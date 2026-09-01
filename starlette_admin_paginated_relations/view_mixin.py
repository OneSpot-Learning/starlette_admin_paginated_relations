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
those three methods -- otherwise unchanged -- with one added condition.

## Coupling / maintenance note

This intentionally duplicates internal logic from
`starlette_admin.contrib.sqla.view.ModelView`, copied from the checkout at
commit `c715eb8dcc38d769b4f0fea4ea318da868cfb896` of jowilf/starlette-admin.
If you upgrade `starlette-admin` and these three methods have changed
upstream (a new parameter, a new eager-load strategy, batched loading,
etc.), this mixin will silently fall out of sync -- it won't error, it'll
just quietly stop reflecting whatever changed. Diff this file's three
methods against the installed library's after any starlette-admin upgrade.
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request
from starlette_admin.contrib.sqla.view import ModelView
from starlette_admin.fields import RelationField
from starlette_admin.filters import FilterGroup
from starlette_admin.tools import iterdecode

from .fields import PaginatedHasMany


def _eager_loadable(field: object) -> bool:
    """`True` for a `RelationField` that should still be joinedload'd
    eagerly -- i.e. every `RelationField` except `PaginatedHasMany`, which
    fetches its own data through `pagination.fetch_page` instead."""
    return isinstance(field, RelationField) and not isinstance(field, PaginatedHasMany)


class PaginatedRelationsModelView(ModelView):
    """Drop-in replacement for `starlette_admin.contrib.sqla.ModelView` that
    skips the automatic eager-join for `PaginatedHasMany` fields specifically,
    while leaving it in place for every other relation field. See this
    module's docstring for why this override is necessary at all.
    """

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
