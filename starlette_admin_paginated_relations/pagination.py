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

import anyio
import sqlakeyset
import sqlakeyset.asyncio as sqlakeyset_asyncio
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from starlette.requests import Request


class PaginationConfigError(ValueError):
    """Raised when a `PaginatedHasMany` field can't be mapped onto a real,
    single-column SQLAlchemy relationship. Raised at request time (not at
    field-declaration time) so the error message can reference the request's
    view/field, and surfaces as a 400 rather than crashing the process."""


def _child_fk_column(parent_model: type, relationship_name: str, child_model: type):
    """Return the child-side FK `Column` for `parent_model.<relationship_name>`.

    Unlike a generic "find some relationship pointing at this model" scan,
    this looks up the *exact* relationship the field already names via
    `relationship_name` (the field's own `.name`), so there's no ambiguity
    even when two relationships connect the same pair of models.
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
    pairs = list(rel.synchronize_pairs)
    if len(pairs) != 1:
        raise PaginationConfigError(
            f"{parent_model.__name__}.{relationship_name} has a composite "
            "foreign key. PaginatedHasMany currently supports only a single-"
            "column foreign key; fall back to the plain HasMany field for "
            "this relationship."
        )
    _parent_col, child_col = pairs[0]
    return child_col


def _order_columns(foreign_view: object) -> tuple:
    """Return the child model's PK column(s) as a tuple, for a stable
    ORDER BY -- sqlakeyset requires the paged query to be ordered by
    (effectively) a unique key."""
    pk_column = foreign_view._pk_column  # noqa: SLF001 -- same attribute contrib/sqla/view.py uses internally
    return pk_column if isinstance(pk_column, tuple) else (pk_column,)


def build_child_query(
    request: Request,
    parent_model: type,
    relationship_name: str,
    foreign_view: object,
    parent_pk_value: object,
) -> Select:
    """The base, ordered, FK-filtered `Select` for one page of children.

    Built on top of `foreign_view.get_list_query(request)` rather than a
    bare `select(child_model)`, so any scoping a view already applies there
    (tenant filters, soft-delete filters, access control, ...) still applies
    here -- pagination must never become a side door around it.
    """
    child_col = _child_fk_column(parent_model, relationship_name, foreign_view.model)
    stmt = foreign_view.get_list_query(request).where(child_col == parent_pk_value).distinct()
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
        total = (await anyio.to_thread.run_sync(session.execute, count_stmt)).scalar_one()
    return page, total
