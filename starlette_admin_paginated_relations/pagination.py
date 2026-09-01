"""SQLAlchemy keyset pagination for a single HasMany relationship.

This module knows nothing about starlette-admin's rendering pipeline; it
answers one question: "given a parent object's pk, a specific SQLAlchemy
relationship, and an optional sqlakeyset bookmark, return one page of
children plus a total count" -- without ever loading the full collection.

Uses `sqlakeyset` (https://github.com/djrobstep/sqlakeyset) for the actual
seek/keyset pagination, so large relationships page in O(page_size) instead
of degrading with OFFSET the way naive limit/offset pagination would. This
mirrors the pagination style already used elsewhere in this codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import sqlakeyset
import sqlakeyset.asyncio as sqlakeyset_asyncio
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from starlette.requests import Request


class PaginationConfigError(ValueError):
    """Raised when a `PaginatedHasMany` field can't be mapped onto a real,
    single-column-keyed SQLAlchemy relationship (direct one-to-many, or
    many-to-many through a `secondary=` table with a single-column key on
    each side). Raised at request time (not at field-declaration time) so
    the error message can reference the request's view/field, and surfaces
    as a 400 rather than crashing the process."""


@dataclass(frozen=True)
class RelationshipMapping:
    """How `parent_model.<relationship_name>` is actually wired at the
    database level -- everything `build_child_query` (read) and
    `view_mixin.py`'s `_add_or_remove_children` (write) need to scope a
    query, or a write, to one specific parent, without either needing to
    special-case which kind of relationship it's looking at beyond checking
    `is_many_to_many` once.

    Direct one-to-many: `child_fk_column` is the child table's own FK column
    pointing at the parent; `secondary`/`secondary_join`/
    `secondary_parent_column` are all `None`.

    Many-to-many (a `secondary=` table): `child_fk_column` is `None` --
    there's no such column, the relationship lives entirely in the
    association table. `secondary` is that table; `secondary_join` is the
    ready-made join condition between it and the child table (straight from
    `RelationshipProperty.secondaryjoin`, so `build_child_query` never has
    to reconstruct it); `secondary_parent_column`/`secondary_child_column`
    are the association table's own columns that must equal the parent's
    and a given child's pk, respectively -- what
    `view_mixin.py`'s add/remove write needs to insert/delete association
    rows directly, without going through `secondary_join`.
    """

    is_many_to_many: bool
    child_fk_column: Any | None
    secondary: Any | None
    secondary_join: Any | None
    secondary_parent_column: Any | None
    secondary_child_column: Any | None


def resolve_relationship(
    parent_model: type, relationship_name: str, child_model: type
) -> RelationshipMapping:
    """Resolve `parent_model.<relationship_name>` into a `RelationshipMapping`.

    Unlike a generic "find some relationship pointing at this model" scan,
    this looks up the *exact* relationship the field already names via
    `relationship_name` (a `PaginatedHasMany`/`PaginatedHasManyRemove`
    field's `.relationship_name`), so there's no ambiguity even when two
    relationships connect the same pair of models. Shared by the read side
    (this module) and the write side (`view_mixin.py`'s add/remove helper),
    so both agree on exactly how the relationship is wired.
    """
    mapper = sa_inspect(parent_model)
    try:
        rel = mapper.relationships[relationship_name]
    except KeyError as exc:
        raise PaginationConfigError(
            f"{parent_model.__name__!r} has no SQLAlchemy relationship named "
            f"{relationship_name!r}. PaginatedHasMany's `name` must match an "
            "actual relationship attribute on the parent model."
        ) from exc
    if rel.mapper.class_ is not child_model:
        raise PaginationConfigError(
            f"{parent_model.__name__}.{relationship_name} points to "
            f"{rel.mapper.class_.__name__!r}, not {child_model.__name__!r} "
            "(the model backing the view named by this field's `key`)."
        )

    if rel.secondary is not None:
        parent_pairs = list(rel.synchronize_pairs)
        child_pairs = list(rel.secondary_synchronize_pairs)
        if len(parent_pairs) != 1 or len(child_pairs) != 1:
            raise PaginationConfigError(
                f"{parent_model.__name__}.{relationship_name} is a "
                "many-to-many relationship with a composite key on one "
                "side of its association table. PaginatedHasMany currently "
                "supports only a single-column key on each side; fall back "
                "to the plain HasMany field for this relationship."
            )
        _parent_col, secondary_parent_col = parent_pairs[0]
        _child_col, secondary_child_col = child_pairs[0]
        return RelationshipMapping(
            is_many_to_many=True,
            child_fk_column=None,
            secondary=rel.secondary,
            secondary_join=rel.secondaryjoin,
            secondary_parent_column=secondary_parent_col,
            secondary_child_column=secondary_child_col,
        )

    pairs = list(rel.synchronize_pairs)
    if len(pairs) != 1:
        raise PaginationConfigError(
            f"{parent_model.__name__}.{relationship_name} has a composite "
            "foreign key. PaginatedHasMany currently supports only a single-"
            "column foreign key; fall back to the plain HasMany field for "
            "this relationship."
        )
    _parent_col, child_col = pairs[0]
    return RelationshipMapping(
        is_many_to_many=False,
        child_fk_column=child_col,
        secondary=None,
        secondary_join=None,
        secondary_parent_column=None,
        secondary_child_column=None,
    )


def _order_columns(foreign_view: object) -> tuple:
    """Return the child model's PK column(s) as a tuple, for a stable
    ORDER BY -- sqlakeyset requires the paged query to be ordered by
    (effectively) a unique key."""
    pk_column = foreign_view._pk_column
    return pk_column if isinstance(pk_column, tuple) else (pk_column,)


def build_child_query(
    request: Request,
    parent_model: type,
    relationship_name: str,
    foreign_view: object,
    parent_pk_value: object,
) -> Select:
    """The base, ordered, relationship-filtered `Select` for one page of
    children -- a direct FK filter, or a join through the association table
    for a many-to-many relationship.

    Built on top of `foreign_view.get_list_query(request)` rather than a
    bare `select(child_model)`, so any scoping a view already applies there
    (tenant filters, soft-delete filters, access control, ...) still applies
    here -- pagination must never become a side door around it.
    """
    mapping = resolve_relationship(parent_model, relationship_name, foreign_view.model)
    base = foreign_view.get_list_query(request)
    if mapping.is_many_to_many:
        # Without this join, `.where(secondary_parent_column == ...)` would
        # reference a table absent from the FROM clause -- SQLAlchemy would
        # silently add it as an implicit cross join instead of raising,
        # matching *every* child row against *any* row in the association
        # table for this parent. That's the exact "every child shows up for
        # every parent" bug this join exists to prevent.
        stmt = (
            base.join(mapping.secondary, mapping.secondary_join)
            .where(mapping.secondary_parent_column == parent_pk_value)
            .distinct()
        )
    else:
        stmt = base.where(mapping.child_fk_column == parent_pk_value).distinct()
    return stmt.order_by(*_order_columns(foreign_view))


async def fetch_page(
    request: Request,
    parent_model: type,
    relationship_name: str,
    foreign_view: object,
    parent_pk_value: object,
    page_size: int,
    bookmark: str | None,
) -> tuple[sqlakeyset.Page, int]:
    """Fetch one page of children plus the total row count.

    Works with both sync `Session` and async `AsyncSession` -- the same
    duality `contrib/sqla/view.py` handles throughout this codebase --
    dispatching to the matching sqlakeyset entry point for each.

    Returns `(page, total)`. `page` is a `sqlakeyset.Page`: a list of Row
    objects (index 0 is the ORM entity, since the query selects only the
    child model) with `.paging` metadata (`has_next`, `has_previous`,
    `bookmark_next`, `bookmark_previous`).
    """
    session: Session | AsyncSession = request.state.session
    stmt = build_child_query(
        request, parent_model, relationship_name, foreign_view, parent_pk_value
    )
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())

    if isinstance(session, AsyncSession):
        page = await sqlakeyset_asyncio.select_page(
            session, stmt, per_page=page_size, page=bookmark
        )
        total = (await session.execute(count_stmt)).scalar_one()
    else:
        page = await anyio.to_thread.run_sync(
            lambda: sqlakeyset.select_page(
                session, stmt, per_page=page_size, page=bookmark
            )
        )
        total = (
            await anyio.to_thread.run_sync(session.execute, count_stmt)
        ).scalar_one()
    return page, total
