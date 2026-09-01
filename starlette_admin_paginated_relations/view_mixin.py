"""`PaginatedRelationsModelView`: the SQLAlchemy `ModelView` subclass a
`PaginatedHasMany` field actually needs to be useful.

## Why this exists

`starlette_admin.contrib.sqla.view.ModelView.find_all` / `find_by_pk` /
`find_by_pks` each unconditionally attach

    stmt = stmt.options(joinedload(getattr(self.model, field.name)))

for *every* field that `isinstance(field, RelationField)` -- and
`PaginatedHasMany` is one (it inherits `HasMany`, so the rest of
starlette-admin keeps treating it as an ordinary relation field for
everything else: form wiring, write-path syncing, recursion-safety checks,
and so on). Left alone, that means a `PaginatedHasMany` field would *still*
force the full related collection to be joined into every list/detail query
-- `PaginatedHasMany.serialize_value` would then just be doing a second,
separately-paginated query on top of a join starlette-admin already paid
for. The whole point of this field is to not pay for that join.

There is no hook in the base class to exclude one field from that loop
without overriding the method it lives in, so this subclass reimplements
`find_all`/`find_by_pk`/`find_by_pks` -- otherwise unchanged -- with one
added condition each.

The same problem, mirrored, shows up on the write side once
`PaginatedHasMany`/`PaginatedHasManyRemove` are allowed on create/edit
forms (see `fields.py`'s "Create/edit: add-only and remove-only" section):
`ModelView._arrange_data`/`_populate_obj` generically `setattr` every
relation field's submitted value onto the object, which for a `HasMany`
*replaces* the whole collection -- exactly wrong for a field that
deliberately never loaded the current collection to know what "replace"
would even mean. `ModelView.edit`'s `old_data` capture has the same failure
mode for a different reason: a bare `getattr(obj, field.name)` over every
field, unconditionally. This subclass reimplements `_arrange_data`,
`_populate_obj`, `edit`, and `after_create` (the last one lightly -- see its
docstring) with one added branch each, so these fields write via a direct,
bounded child-FK update (`_add_or_remove_children`) instead. `can_access_field`
gets a small, non-copied override on top, to keep these fields out of the
list page's separate inline-edit write path, which none of the above
intercepts.

## Coupling / maintenance note

This intentionally duplicates internal logic from
`starlette_admin.contrib.sqla.view.ModelView`, copied from the checkout at
commit `c715eb8dcc38d769b4f0fea4ea318da868cfb896` of jowilf/starlette-admin.
If you upgrade `starlette-admin` and these methods have changed upstream (a
new parameter, a new eager-load strategy, batched loading, etc.), this
mixin will silently fall out of sync -- it won't error, it'll just quietly
stop reflecting whatever changed. Diff this file's copied methods
(`find_all`, `find_by_pk`, `find_by_pks`, `_arrange_data`, `_populate_obj`,
`edit`) against the installed library's after any starlette-admin upgrade.
Pin your `starlette-admin` version accordingly.

## Usage

    class AuthorView(PaginatedRelationsModelView):
        fields = [IntegerField("id"), StringField("name"), PaginatedHasMany("books", key="book")]

If a view has no `PaginatedHasMany` fields, inheriting this mixin instead of
plain `ModelView` is a harmless no-op (the added condition never fires).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anyio.to_thread
from sqlalchemy import and_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request
from starlette_admin.contrib.sqla.view import ModelView
from starlette_admin.exceptions import FormValidationError
from starlette_admin.fields import FileField, HasMany, RelationField
from starlette_admin.filters import FilterGroup
from starlette_admin.helpers import not_none, on_commit
from starlette_admin.logging import get_logger
from starlette_admin.tools import iterdecode
from starlette_admin.types import RequestAction

from .fields import PaginatedHasMany, PaginatedHasManyRemove
from .pagination import PaginationConfigError, child_fk_column

_log = get_logger(__name__)

#: A `PaginatedHasMany`/`PaginatedHasManyRemove` field never goes through
#: the generic relation-field write path (see `_populate_obj` below) -- both
#: write directly to the child-side FK column for a bounded set of children
#: instead of replacing the whole ORM-managed collection.
_DirectWriteRelation = (PaginatedHasMany, PaginatedHasManyRemove)


def _eager_loadable(field: object) -> bool:
    """`True` for a `RelationField` that should still be joinedload'd
    eagerly -- i.e. every `RelationField` except `PaginatedHasMany` (which
    fetches its own data through `pagination.fetch_page` instead) and
    `PaginatedHasManyRemove` (whose `.name` isn't even a real attribute on
    the model -- it's a standalone form field id -- so
    `getattr(self.model, field.name)` would raise for it besides)."""
    return isinstance(field, RelationField) and not isinstance(
        field, _DirectWriteRelation
    )


async def _add_or_remove_children(
    request: Request,
    foreign_view: Any,
    child_col: Any,
    parent_pk_value: Any,
    child_pks: Sequence[Any],
    *,
    relate: bool,
) -> None:
    """Add or remove a bounded set of children to/from one parent's HasMany
    relationship by writing the child-side FK column directly.

    Deliberately never touches the ORM-managed collection attribute (`obj.books
    = [...]`), so children not named in `child_pks` are never affected --
    the same guarantee `PaginatedHasMany`'s read side makes, extended to the
    write side. `foreign_view.find_by_pks` bounds the load to exactly the
    submitted pks, never the full relationship.

    `relate=True` (`PaginatedHasMany`): set each child's FK to
    `parent_pk_value`, skipping any child that's already set to it.
    `relate=False` (`PaginatedHasManyRemove`): null each child's FK, but
    only where it currently equals `parent_pk_value` -- a child related to
    some *other* parent, or not related at all, is left untouched.
    """
    if not child_pks:
        return
    if not relate and not child_col.nullable:
        raise PaginationConfigError(
            f"{child_col.table.name}.{child_col.name} is NOT NULL -- "
            "PaginatedHasManyRemove can't unrelate a child without a "
            "nullable foreign key column."
        )
    children = await foreign_view.find_by_pks(request, list(child_pks))
    session: Session | AsyncSession = request.state.session
    dirty = False
    for child in children:
        current = getattr(child, child_col.key)
        if relate and current != parent_pk_value:
            setattr(child, child_col.key, parent_pk_value)
            dirty = True
        elif not relate and current == parent_pk_value:
            setattr(child, child_col.key, None)
            dirty = True
    if not dirty:
        return
    if isinstance(session, AsyncSession):
        await session.flush()
    else:
        await anyio.to_thread.run_sync(session.flush)


class PaginatedRelationsModelView(ModelView):
    """Drop-in replacement for `starlette_admin.contrib.sqla.ModelView`
    that: skips the automatic eager-join for `PaginatedHasMany` fields on
    read (leaving it in place for every other relation field), and routes
    `PaginatedHasMany`/`PaginatedHasManyRemove` writes through a direct,
    bounded child-FK update instead of the generic whole-collection-replace
    write path. See this module's docstring for why each override exists.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._validate_direct_write_relations()

    def _validate_direct_write_relations(self) -> None:
        """Catch a `relationship_name` that doesn't name a real
        relationship on this view's model at view-construction time (app
        startup), instead of the mistake surfacing only much later --
        silently, as an empty result set -- from `_search`, or mid-request
        from `_populate_obj`/`after_create`. `PaginatedHasManyRemove` is the
        field this most commonly bites: its `.name` is just its own form
        field id (has to be, to coexist with a same-relationship
        `PaginatedHasMany` sibling), completely independent of the actual
        relationship it targets, so a forgotten `relationship_name` fails
        this check immediately rather than looking like a working field
        that just never finds anything to remove.

        Deliberately only checks that the relationship *exists* -- that's
        all that's knowable this early, before the foreign view (needed to
        confirm it points at the right child model, and that the FK is
        single-column) is resolvable from `field.key` via the admin's view
        registry. `pagination.child_fk_column` still performs that fuller
        check lazily, at first request.
        """
        relationships = sa_inspect(self.model).relationships.keys()
        for field in self.fields:
            if (
                isinstance(field, _DirectWriteRelation)
                and field.relationship_name not in relationships
            ):
                hint = (
                    " If this field's .name is meant to differ from the "
                    "relationship it targets (e.g. a PaginatedHasManyRemove "
                    "alongside a same-relationship PaginatedHasMany), pass "
                    "relationship_name explicitly."
                    if field.relationship_name == field.name
                    else ""
                )
                raise PaginationConfigError(
                    f"{type(field).__name__} {field.name!r} on "
                    f"{type(self).__name__} has relationship_name="
                    f"{field.relationship_name!r}, which is not a "
                    f"SQLAlchemy relationship on {self.model.__name__}." + hint
                )

    async def find_all(
        self,
        request: Request,
        skip: int = 0,
        limit: int = 100,
        q: str | None = None,
        sorts: Sequence[tuple[str, str]] | None = None,
        filters: FilterGroup | None = None,
    ) -> Sequence[Any]:
        session: Session | AsyncSession = request.state.session
        stmt = self.get_list_query(request).offset(skip)
        if limit > 0:
            stmt = stmt.limit(limit)
        stmt = await self._apply_search_and_filters(request, stmt, q, filters)
        stmt = self.build_order_clauses(request, sorts or [], stmt)
        for field in self.get_fields_list(request):
            if _eager_loadable(field):
                stmt = stmt.options(joinedload(getattr(self.model, field.name)))
        if isinstance(session, AsyncSession):
            rows = (await session.execute(stmt)).scalars().unique().all()
        else:
            rows = (
                (await anyio.to_thread.run_sync(session.execute, stmt))
                .scalars()
                .unique()
                .all()
            )
        return rows

    async def find_by_pk(self, request: Request, pk: Any) -> Any:
        session: Session | AsyncSession = request.state.session
        if isinstance(self._pk_column, tuple):
            clause = self._composite_pk_clause(pk)
        else:
            assert isinstance(self._pk_coerce, type)
            clause = self._pk_column == self._pk_coerce(pk)
        stmt = self.get_detail_query(request).where(clause)
        for field in self.get_fields_list(request):
            if _eager_loadable(field):
                stmt = stmt.options(joinedload(getattr(self.model, field.name)))
        if isinstance(session, AsyncSession):
            obj = (await session.execute(stmt)).scalars().unique().one_or_none()
        else:
            obj = (
                (await anyio.to_thread.run_sync(session.execute, stmt))
                .scalars()
                .unique()
                .one_or_none()
            )
        return obj

    def _composite_pk_clause(self, pk: Any) -> Any:
        """Mirrors `ModelView.find_by_pk`'s composite-PK branch verbatim
        (not otherwise reachable as a standalone method on the base class)."""
        assert isinstance(self._pk_coerce, tuple)
        return and_(
            *(
                (
                    _pk_col == _coerce(_pk)
                    if _coerce is not bool
                    else _pk_col == (_pk == "True")
                )
                for _pk_col, _coerce, _pk in zip(
                    self._pk_column,
                    self._pk_coerce,
                    iterdecode(pk),
                )
            )
        )

    async def find_by_pks(self, request: Request, pks: list[Any]) -> Sequence[Any]:
        has_multiple_pks = isinstance(self._pk_column, tuple)
        try:
            return await self._exec_find_by_pks(request, pks)
        except DBAPIError:  # pragma: no cover
            if has_multiple_pks:
                return await self._exec_find_by_pks(request, pks, False)
            raise

    async def _exec_find_by_pks(
        self, request: Request, pks: list[Any], use_composite_in: bool = True
    ) -> Sequence[Any]:
        session: Session | AsyncSession = request.state.session
        if isinstance(self._pk_column, tuple):
            clause = await self._get_multiple_pks_in_clause(pks, use_composite_in)
        else:
            assert isinstance(self._pk_coerce, type)
            clause = self._pk_column.in_(map(self._pk_coerce, pks))
        stmt = self.get_detail_query(request).where(clause)
        for field in self.get_fields_list(request):
            if _eager_loadable(field):
                stmt = stmt.options(joinedload(getattr(self.model, field.name)))
        if isinstance(session, AsyncSession):
            return (await session.execute(stmt)).scalars().unique().all()
        return (
            (await anyio.to_thread.run_sync(session.execute, stmt))
            .scalars()
            .unique()
            .all()
        )

    def can_access_field(
        self, request: Request, field: Any, action: Any = None
    ) -> bool:
        """Blocks the list page's single-field inline-edit popover for
        `PaginatedHasMany`/`PaginatedHasManyRemove` specifically, even
        though `exclude_from_edit=False` (needed for the full edit *page*
        to show them). Inline-edit is a separate write path this mixin
        doesn't intercept, so letting it through would reintroduce a plain
        `HasMany`-style whole-collection replace for exactly the field this
        library exists to make safe."""
        action = action if action is not None else request.state.action
        if action == RequestAction.INLINE_EDIT and isinstance(
            field, _DirectWriteRelation
        ):
            return False
        return super().can_access_field(request, field, action)

    async def _arrange_data(
        self,
        request: Request,
        data: dict[str, Any],
        is_edit: bool = False,
    ) -> dict[str, Any]:
        """Copy of `ModelView._arrange_data` with one branch added: a
        `PaginatedHasMany`/`PaginatedHasManyRemove` field's submitted pks
        pass through unchanged, rather than being resolved into loaded ORM
        objects and wrapped for assignment onto the relationship attribute
        -- `_populate_obj` below writes them directly instead. See this
        module's docstring for the upstream-coupling/version-pinning note
        that applies to every method copied here.
        """
        arranged_data: dict[str, Any] = {}
        for field in self.get_fields_list(request):
            if field.read_only:
                continue
            if isinstance(field, _DirectWriteRelation):
                arranged_data[field.name] = data[field.name]
            elif isinstance(field, RelationField) and data[field.name] is not None:
                foreign_view = self._find_foreign_view(field.key)  # type: ignore[attr-defined]
                if isinstance(field, HasMany):
                    arranged_data[field.name] = field.collection_class(
                        await foreign_view.find_by_pks(request, data[field.name])
                    )
                else:
                    arranged_data[field.name] = await foreign_view.find_by_pk(
                        request, data[field.name]
                    )
            else:
                arranged_data[field.name] = data[field.name]
        return arranged_data

    async def _populate_obj(
        self,
        request: Request,
        obj: Any,
        data: dict[str, Any],
        is_edit: bool = False,
    ) -> Any:
        """Copy of `ModelView._populate_obj` with one branch added: a
        `PaginatedHasMany`/`PaginatedHasManyRemove` field is never
        `setattr`'d onto `obj` at all (that would mean assigning a plain
        list to a SQLAlchemy relationship, which *replaces* the whole
        collection). On edit, the parent's pk is already known, so the
        add/remove happens immediately via direct child-FK writes. On
        create, `obj` has no pk yet (not flushed) -- the pending writes are
        stashed on `request.state` for `after_create` to apply once it does.
        """
        pending: list[tuple[Any, list[Any]]] = []
        for field in self.get_fields_list(request):
            if field.read_only:
                continue
            if isinstance(field, _DirectWriteRelation):
                child_pks = data.get(field.name) or []
                if not is_edit:
                    pending.append((field, child_pks))
                    continue
                foreign_view = self._find_foreign_view(field.key)  # type: ignore[attr-defined]
                child_col = child_fk_column(
                    self.model, field.relationship_name, foreign_view.model
                )
                parent_pk = await self.get_pk_value(request, obj)
                await _add_or_remove_children(
                    request,
                    foreign_view,
                    child_col,
                    parent_pk,
                    child_pks,
                    relate=isinstance(field, PaginatedHasMany),
                )
                continue
            name, value = field.name, data.get(field.name)
            if isinstance(field, FileField) and field.storage is None:
                value, should_be_deleted = not_none(value)
                if should_be_deleted:
                    setattr(obj, name, None)
                elif (not field.multiple and value is not None) or (
                    field.multiple and isinstance(value, list) and len(value) > 0
                ):
                    setattr(obj, name, value)
            else:
                setattr(obj, name, value)
        if pending:
            request.state._paginated_relations_pending = pending
        return obj

    async def after_create(self, request: Request, obj: Any) -> None:
        """Applies the add/remove writes `_populate_obj` deferred for
        create, now that `obj` has a pk (this runs after `create()`'s own
        flush, inside the same still-open transaction -- see
        `starlette_admin.contrib.sqla.view.ModelView.create`)."""
        await super().after_create(request, obj)
        pending: list[tuple[Any, list[Any]]] | None = getattr(
            request.state, "_paginated_relations_pending", None
        )
        if not pending:
            return
        parent_pk = await self.get_pk_value(request, obj)
        for field, child_pks in pending:
            foreign_view = self._find_foreign_view(field.key)  # type: ignore[attr-defined]
            child_col = child_fk_column(
                self.model, field.relationship_name, foreign_view.model
            )
            await _add_or_remove_children(
                request,
                foreign_view,
                child_col,
                parent_pk,
                child_pks,
                relate=isinstance(field, PaginatedHasMany),
            )
        request.state._paginated_relations_pending = None

    async def edit(self, request: Request, pk: Any, data: dict[str, Any]) -> Any:
        """Copy of `ModelView.edit` with one change: `old_data` (used only
        for `before_edit`/`after_edit` event payloads) reports `None` for a
        `PaginatedHasMany`/`PaginatedHasManyRemove` field instead of
        `getattr(obj, field.name)`, which -- with these fields now allowed
        on the edit form -- would otherwise be exactly the unbounded
        full-collection load this library exists to avoid, just moved from
        the read side to the write side. Event consumers won't see this
        field's old/new state; see this module's docstring for the
        upstream-coupling/version-pinning note that applies to every method
        copied here.
        """
        _log.debug(
            "edit %s pk=%r: arranging and validating data", self.model.__name__, pk
        )
        session: Session | AsyncSession = request.state.session
        try:
            data = await self._arrange_data(request, data, True)
            await self.validate(request, data)
            obj = await self.find_by_pk(request, pk)
            old_data = {
                f.name: (
                    None
                    if isinstance(f, _DirectWriteRelation)
                    else getattr(obj, f.name, None)
                )
                for f in self.get_fields_list(request)
            }
            if isinstance(session, AsyncSession):
                async with session.begin_nested():
                    await self._populate_obj(request, obj, data, True)
                    await self._emit_before_edit(
                        request, data, obj, pk=pk, old_data=old_data
                    )
                    session.add(obj)
                    await session.flush()
                await session.refresh(obj, self._refresh_attr_names(request))
            else:
                with session.begin_nested():
                    await self._populate_obj(request, obj, data, True)
                    await self._emit_before_edit(
                        request, data, obj, pk=pk, old_data=old_data
                    )
                    session.add(obj)
                    await anyio.to_thread.run_sync(session.flush)
                await anyio.to_thread.run_sync(
                    session.refresh, obj, self._refresh_attr_names(request)
                )
            await self._emit_after_edit(request, obj, pk=pk, old_data=old_data)
            on_commit(
                request,
                lambda: self._emit_after_edit_committed(
                    request, obj, old_data=old_data
                ),
            )
            _log.info("Edited %s pk=%r", self.model.__name__, pk)
            return obj
        except Exception as e:  # noqa: BLE001 -- matches upstream ModelView.edit/create exactly: any exception (validation, DB, or a PaginationConfigError from _add_or_remove_children) must route through handle_exception for consistent logging/HTTP mapping.
            if isinstance(e, FormValidationError):
                _log.warning(
                    "edit %s pk=%r: validation failed: %s", self.model.__name__, pk, e
                )
            else:
                _log.error(
                    "edit %s pk=%r: unexpected error: %s",
                    self.model.__name__,
                    pk,
                    e,
                    exc_info=True,
                )
            await self.handle_exception(request, e)
