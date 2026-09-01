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
* **Single-column keys only.** Both a direct one-to-many (a composite FK)
  and a many-to-many through a `secondary=` table (a composite key on
  either side of the association table) raise `PaginationConfigError` at
  request time; use plain `HasMany` for those.
* **Export.** A CSV/Excel/JSON row still needs the full related collection
  to represent it, which is exactly the cost this field avoids elsewhere, so
  `exclude_from_export` stays `True`. Pass `exclude_from_export=False`
  explicitly if that trade-off is fine for a given field -- it then falls
  back to `HasMany`'s normal (unbounded) behavior for that action only.

## Create/edit: add-only and remove-only, never a full-collection replace

`HasMany`'s own multi-select widget already searches and paginates its
*options* via the built-in `relation-lookup` endpoint (see
`starlette_admin/base.py`) -- that was never the bottleneck. The bottleneck
was pre-selecting the *current* value: showing what's already related
requires loading it, the same unbounded read this field exists to avoid,
just on the form instead of the list.

So `PaginatedHasMany`'s create/edit widget (`parse_obj`, below) simply never
pre-loads or shows the current selection -- it always starts empty. Picking
children in it and submitting only **adds** those children to the
relationship; anything already related, shown or not, is left alone. This
is also why a plain `HasMany`-style submit (which replaces the *entire*
collection with whatever the form posts) would be actively wrong here: with
no current selection loaded, a "replace" write would silently orphan every
child not re-picked in this one visit. `PaginatedRelationsModelView`
(`view_mixin.py`) intercepts the write instead, applying it as a direct,
bounded child-FK update -- see that module for the mechanics.

`PaginatedHasManyRemove` is the mirror image for the same relationship:
also a search-as-you-type select that starts empty, but **subtractive**
only -- picking a child unrelates it (nulls its FK) if it's currently
related, and does nothing otherwise. It only makes sense on an edit form
(there's nothing to remove from a not-yet-created parent), so it's excluded
from create/list/detail/export unconditionally. Because it targets the same
relationship as a `PaginatedHasMany` field but needs its own distinct form
field id, its `.name` is just that id -- point `relationship_name` at the
real SQLAlchemy relationship name explicitly if it differs (it defaults to
`.name`, so a lone `PaginatedHasManyRemove` with no sibling add-field can
skip it, same as `PaginatedHasMany`). It searches only children *currently*
related to this parent, through `PaginatedRelationsPlugin`'s `/search`
route -- never the whole foreign table -- since removing something not
already related is meaningless.

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
    # Create/edit render as a search-as-you-type select (core HasMany's own
    # relation-lookup widget, unmodified) that never pre-loads the current
    # selection -- see `parse_obj` below -- and whose submission is handled
    # by `PaginatedRelationsModelView._populate_obj` as an *add-only* write:
    # picked children get related to this parent; nothing already related
    # is ever touched, so this never has to pay for the unbounded load a
    # normal HasMany's replace-the-whole-collection write would need to
    # avoid clobbering what it can't see. Export still would need the full
    # collection, so that stays excluded by default.
    exclude_from_create: bool | None = False
    exclude_from_edit: bool | None = False
    exclude_from_export: bool | None = True

    @property
    def relationship_name(self) -> str:
        """The SQLAlchemy relationship this field writes to. Always `.name`
        for `PaginatedHasMany` itself; exists so write-path code in
        `view_mixin.py` can treat this and `PaginatedHasManyRemove` (whose
        `.name` is its own form field id, independent of the relationship it
        targets) identically."""
        return self.name

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
        if request.state.action in (RequestAction.CREATE, RequestAction.EDIT):
            # Same reasoning as above, for the same reason: the create/edit
            # widget starts empty rather than paying to pre-load and show
            # every currently-related child. `[]`, not `None` -- the base
            # RelationField.serialize_value this flows into iterates the
            # value directly for form rendering.
            return []
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


@dataclass
class PaginatedHasManyRemove(HasMany):
    """A search-as-you-type select, for an edit form only, that *unrelates*
    already-related children -- the subtractive mirror of `PaginatedHasMany`.

    Parameters:
        relationship_name: The actual SQLAlchemy relationship name on the
            parent model. Defaults to `.name`, same as `PaginatedHasMany` --
            set this explicitly when a sibling `PaginatedHasMany` field
            already owns that name, since two fields on one view can't share
            a `.name` (it's also the HTML form field id).

    See this module's docstring ("Create/edit: add-only and remove-only...")
    for the full rationale. In short: picking a child here and submitting
    the form nulls that child's FK if it's currently related to this parent,
    and does nothing if it isn't -- it never adds a relation, and it never
    touches any child not explicitly picked. The write itself is handled by
    `PaginatedRelationsModelView._populate_obj` (`view_mixin.py`), the same
    place that handles `PaginatedHasMany`'s add-only write.
    """

    relationship_name: str | None = None
    form_template: str = (
        "plugins/paginated-relations/fields/form/paginated_relation_remove.html"
    )
    # Only ever meaningful on an edit form: there's no relationship yet on
    # a not-created parent, and list/detail/export all read existing state
    # rather than offer to change it.
    exclude_from_list: bool = True
    exclude_from_detail: bool = True
    exclude_from_create: bool = True
    exclude_from_edit: bool = False
    exclude_from_export: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.relationship_name is None:
            self.relationship_name = self.name

    async def parse_obj(self, request: Request, obj: Any) -> Any:
        if request.state.action == RequestAction.EDIT:
            # Same reasoning as PaginatedHasMany.parse_obj: never load the
            # current collection just to render this field. What's returned
            # here becomes `data` in the form template, which only needs
            # this parent's own pk to scope its search endpoint -- not the
            # list of currently-related children.
            assert self._view is not None, (
                f"PaginatedHasManyRemove {self.name!r} has no _view; it "
                "must be used inside a BaseModelView"
            )
            return await self._view.get_pk_value(request, obj)
        return []

    async def serialize_value(self, request: Request, value: Any) -> Any:
        if request.state.action == RequestAction.EDIT:
            return value  # the parent pk from parse_obj, passed through as-is
        return await super().serialize_value(request, value)
