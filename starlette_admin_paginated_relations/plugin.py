"""`PaginatedRelationsPlugin`: the one JSON route `PaginatedHasMany` needs for
its Prev/Next controls, wired up the way starlette-admin plugins are meant to
be (see `starlette_admin.plugins.BasePlugin` and the "Plugins" doc page in
the reviewed checkout).

Registering the plugin is what makes `PaginatedHasMany`'s templates and
static JS resolve at all (they live under this package's
`templates/plugins/paginated-relations/` and
`static/plugins/paginated-relations/`, which the admin only wires into its
Jinja loader / static mount for plugins it was actually given).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.status import HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND
from starlette_admin.plugins import BasePlugin
from starlette_admin.types import RequestAction

from .fields import PaginatedHasMany

if TYPE_CHECKING:
    from starlette_admin.base import BaseAdmin


class PaginatedRelationsPlugin(BasePlugin):
    """Register once, alongside your other `Admin(plugins=[...])` entries.

    ```python
    admin = Admin(engine, plugins=[PaginatedRelationsPlugin()])
    ```

    Contributes exactly one thing beyond the bundled templates/static/JS
    that `BasePlugin`'s folder convention picks up automatically: the JSON
    route the JS pager calls for page 2 onward. Page 1 is always rendered
    inline by `PaginatedHasMany.serialize_value` as part of the normal
    list/detail response, so this route is never hit on first paint.
    """

    name = "paginated-relations"

    def setup(self, admin: "BaseAdmin") -> None:
        # Stashed so the route handler (a plain function, not a BaseAdmin
        # method) can reach `_find_view_by_key` the same way core's own
        # `_render_relation_lookup` does.
        self._admin = admin

    def routes(self) -> list[Route]:
        return [Route("/page", self._page, methods=["GET"], name="page")]

    async def _page(self, request: Request) -> Response:
        """`GET /plugins/paginated-relations/page?view=<key>&field=<name>&pk=<pk>[&page=<bookmark>]`

        Returns the same `{"items": [...], "total": ..., ...}` shape
        `PaginatedHasMany.serialize_value` produces for page 1, for whichever
        page `page` (a serialized sqlakeyset bookmark, from a previous
        response's `bookmark_next`/`bookmark_previous`) points to. Omit
        `page` to re-fetch the first page.
        """
        params = request.query_params
        view_key = params.get("view")
        field_name = params.get("field")
        pk = params.get("pk")
        bookmark = params.get("page") or None
        if not view_key or not field_name or pk is None:
            return JSONResponse(
                {"error": "view, field, and pk are all required"}, status_code=400
            )

        view = self._admin._find_view_by_key(view_key)  # noqa: SLF001
        if not view.is_accessible(request):
            return JSONResponse(None, status_code=HTTP_403_FORBIDDEN)

        # This endpoint's own URL isn't a page a user can return to -- left
        # alone, each child's `_meta.detailUrl` would carry `_origin` back
        # to *this* JSON route (see `_carry_origin` in starlette-admin's
        # helpers.py). `origin` is the page the pager button was actually
        # clicked from, forwarded by the JS as
        # `location.pathname + location.search`; accept it only as a
        # same-origin relative path (never `//host/...`, which browsers
        # treat as protocol-relative to another host) to avoid turning this
        # into an open redirect, and fall back to the parent view's bare
        # list page otherwise (no sort/filter state to preserve here, since
        # this endpoint never had it in the first place).
        origin = params.get("origin")
        if origin and origin.startswith("/") and not origin.startswith("//"):
            request.state.origin_override = origin
        else:
            route_name = request.app.state.ROUTE_NAME
            request.state.origin_override = request.url_for(
                f"{route_name}:list", key=view_key
            ).path

        # A PaginatedHasMany field commonly sets exclude_from_list=True (full
        # paginated relation cells belong on the detail page, not crammed
        # into a list row) or exclude_from_detail=True. Resolve it under
        # whichever action it's actually visible for, using get_fields_list's
        # own `action` override so this lookup doesn't depend on -- or need
        # to mutate -- request.state.action. Checking both keeps Prev/Next
        # working regardless of which page the pager button was clicked from.
        field = next(
            (
                f
                for f in view.get_fields_list(request, action=RequestAction.LIST)
                if f.name == field_name and isinstance(f, PaginatedHasMany)
            ),
            None,
        ) or next(
            (
                f
                for f in view.get_fields_list(request, action=RequestAction.DETAIL)
                if f.name == field_name and isinstance(f, PaginatedHasMany)
            ),
            None,
        )
        if field is None:
            return JSONResponse(
                {"error": f"no accessible PaginatedHasMany field {field_name!r}"},
                status_code=HTTP_404_NOT_FOUND,
            )

        foreign_view = view._find_foreign_view(field.key)  # noqa: SLF001
        if not foreign_view.is_accessible(request):
            return JSONResponse(None, status_code=HTTP_403_FORBIDDEN)

        # Child items are rendered the same way the list page's own
        # `RelationField.serialize_value` renders them, so field-level
        # visibility on the *child* side is governed by exclude_from_list.
        request.state.action = RequestAction.LIST

        if isinstance(view._pk_column, tuple):  # noqa: SLF001
            # Matches pagination.py's single-column-FK restriction: a
            # composite *parent* PK would need a composite FK on the child
            # side to reference it, which PaginatedHasMany doesn't support.
            return JSONResponse(
                {"error": "PaginatedHasMany does not support composite parent keys"},
                status_code=400,
            )
        try:
            parent_pk = view._pk_coerce(pk)  # noqa: SLF001
        except (TypeError, ValueError):
            return JSONResponse({"error": f"invalid pk {pk!r}"}, status_code=400)

        result = await field._serialize_page(  # noqa: SLF001
            request, parent_pk, foreign_view, bookmark
        )
        return JSONResponse(result)
