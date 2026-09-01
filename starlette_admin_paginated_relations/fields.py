"""`PaginatedHasMany`: a HasMany field that never loads the full related
collection to render the list or detail page.

## The problem this solves

`starlette_admin.fields.HasMany.serialize_value` (as of the reviewed
starlette-admin checkout) iterates the *entire* loaded collection to build
the list/detail payload:

    result = [
        await foreign_view.serialize(v, request, include_relationships=False)
        for v in value
    ]

and `value` itself comes from plain attribute access in `BaseField.parse_obj`
(`getattr(obj, self.name, None)`). On the SQLAlchemy backend specifically,
`ModelView.find_all` / `find_by_pk` / `find_by_pks`
(`starlette_admin/contrib/sqla/view.py`) additionally attach
`.options(joinedload(getattr(self.model, field.name)))` for *every*
`RelationField`, unconditionally, before the query even runs. Put together:
showing a paginated list of 50 authors, where each author has a `HasMany`
field to their books, runs one query that joins in *every* book for *every*
author on the page, then serializes every one of those rows -- even though
the list page only ever displays a truncated handful of links per cell. An
author with 50,000 books turns one admin page load into a multi-hundred-
thousand-row join, every time that page is opened.

## What this field does instead

`PaginatedHasMany` never touches the ORM-loaded relationship for LIST/DETAIL
rendering. `parse_obj` returns just the parent's own pk (essentially free);
`serialize_value` then runs a small, explicitly paginated query -- filtered
by the child's foreign key, scoped through the foreign view's own
`get_list_query` (so tenant/soft-delete/access scoping still applies), and
paged with `sqlakeyset` (see `pagination.py`) so paging cost stays flat
regardless of collection size. The *first* page is fetched inline so the
list/detail cell renders with data immediately; further pages are fetched
client-side from `PaginatedRelationsPlugin`'s JSON route as the user clicks
Prev/Next (see `static/plugins/paginated-relations/js/paginated_relation.js`).

## What's out of scope (by design)

* **SQLAlchemy only.** `pagination.py` uses `sqlalchemy.inspect(...)
  .relationships[...]` and `foreign_view.get_list_query`/`._pk_column`,
  which are specific to `starlette_admin.contrib.sqla`.
* **Single-column foreign keys only.** A composite FK raises
  `PaginationConfigError` at request time; use plain `HasMany` for those.
* **Create/edit forms are unaffected.** `HasMany`'s existing multi-select
  widget already paginates and searches via the built-in
  `relation-lookup` endpoint (see `starlette_admin/base.py`), which is not
  the bottleneck this field addresses. Because pre-selecting the *current*
  value in an edit form still requires knowing every currently-related pk
  (the same unbounded-load problem, just for the form instead of the list),
  `PaginatedHasMany` defaults `exclude_from_create` / `exclude_from_edit` /
  `exclude_from_export` to `True`. Pass `exclude_from_edit=False` etc.
  explicitly if you've decided that trade-off is fine for a given field --
  it then falls back to `HasMany`'s normal (unbounded) behavior for that
  action only.
* **No search-within-relation.** Only Prev/Next paging; add a `q` param to
  `pagination.build_child_query` if you need it later.

## Usage

    from starlette_admin_paginated_relations import PaginatedHasMany, PaginatedRelationsPlugin
    from starlette_admin_paginated_relations.view_mixin import PaginatedRelationsModelView

    class AuthorView(PaginatedRelationsModelView):
        fields = [
            IntegerField("id"),
            StringField("name"),
            PaginatedHasMany("books", key="book", page_size=10),
        ]

    admin = Admin(engine, plugins=[PaginatedRelationsPlugin()])
    admin.add_view(AuthorView(Author, key="author"))
    admin.add_view(BookView(Book, key="book"))

`AuthorView` must inherit `PaginatedRelationsModelView` (see
`view_mixin.py`) for the join-avoidance to take effect -- adding the field
to a plain `ModelView` still works (it degrades to running one extra join
you don't need), but you lose the actual point of this field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette_admin.fields import HasMany
from starlette_admin.helpers import not_none, static_url
from starlette_admin.types import RequestAction

from .pagination import PaginationConfigError, fetch_page

#: Query-string key the JS pager uses to carry a serialized sqlakeyset
#: bookmark. Encodes both "which row" and "which direction" (see
#: `sqlakeyset.serialize_bookmark`), so a single param is enough for both
#: Prev and Next.
BOOKMARK_PARAM = "page"


@dataclass
class PaginatedHasMany(HasMany):
    """A `HasMany` field whose list/detail rendering is paginated
    server-side instead of loading the full related collection.

    Parameters:
        page_size: Number of related rows fetched and shown per page, both
            for the inline first page and for each subsequent Prev/Next
            request against `PaginatedRelationsPlugin`'s JSON route.

    See this module's docstring for the full rationale and scope.
    """

    page_size: int = 10
    list_template: str = (
        "plugins/paginated-relations/fields/list/paginated_relation.html"
    )
    detail_template: str = (
        "plugins/paginated-relations/fields/detail/paginated_relation.html"
    )
    # See "What's out of scope" above: these three actions would otherwise
    # still force a full, unbounded load of the relationship (to pre-select
    # current values, or to export every related row). Override explicitly
    # per-field if that trade-off is acceptable for a given relationship.
    exclude_from_create: bool | None = True
    exclude_from_edit: bool | None = True
    exclude_from_export: bool | None = True

    def additional_js_links(self, request: Request) -> list[str]:
        if request.state.action in (RequestAction.LIST, RequestAction.DETAIL):
            return [
                static_url(
                    request,
                    "plugins/paginated-relations/js/paginated_relation.js",
                    v="1.0.0",
                )
            ]
        return super().additional_js_links(request)

    async def parse_obj(self, request: Request, obj: Any) -> Any:
        if request.state.action in (RequestAction.LIST, RequestAction.DETAIL):
            # Deliberately does NOT do `getattr(obj, self.name)`: on the
            # SQLAlchemy backend that line alone would lazy-load (or, if
            # already joinedload'd, materialize) every related row.
            assert self._view is not None, (
                f"PaginatedHasMany {self.name!r} has no _view; it must be "
                "used inside a BaseModelView"
            )
            return await self._view.get_pk_value(request, obj)
        return await super().parse_obj(request, obj)

    async def serialize_value(self, request: Request, value: Any) -> Any:
        action = request.state.action
        if action not in (RequestAction.LIST, RequestAction.DETAIL):
            return await super().serialize_value(request, value)

        assert self._view is not None
        parent_pk = value  # what parse_obj returned above
        foreign_view = self._view._find_foreign_view(not_none(self.key))
        bookmark = None  # first page, always, for inline list/detail rendering
        return await self._serialize_page(request, parent_pk, foreign_view, bookmark)

    async def _serialize_page(
        self,
        request: Request,
        parent_pk: Any,
        foreign_view: Any,
        bookmark: str | None,
    ) -> dict[str, Any]:
        """Fetch and serialize one page of related items for `parent_pk`.

        Shared by `serialize_value` (always page 1, for the initial render)
        and `PaginatedRelationsPlugin`'s JSON route (any page, for Prev/Next
        clicks) -- see `plugin.py`.
        """
        try:
            page, total = await fetch_page(
                request=request,
                parent_model=not_none(self._view).model,  # type: ignore[union-attr]
                relationship_name=self.name,
                foreign_view=foreign_view,
                parent_pk_value=parent_pk,
                page_size=self.page_size,
                bookmark=bookmark,
            )
        except PaginationConfigError as exc:
            return {"error": str(exc), "items": [], "total": 0}
        items = [
            await foreign_view.serialize(row[0], request, include_relationships=False)
            for row in page
        ]
        return {
            "items": items,
            "total": total,
            "page_size": self.page_size,
            "has_next": page.paging.has_next,
            "has_previous": page.paging.has_previous,
            "bookmark_next": page.paging.bookmark_next
            if page.paging.has_next
            else None,
            "bookmark_previous": (
                page.paging.bookmark_previous if page.paging.has_previous else None
            ),
        }
