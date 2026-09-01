# starlette-admin-paginated-relations

A `PaginatedHasMany` field for [starlette-admin](https://github.com/jowilf/starlette-admin)
(SQLAlchemy backend) that pages a has-many relationship's list/detail
rendering instead of loading the full related collection.

## The problem

`HasMany`'s list/detail rendering (`starlette_admin/fields.py`) serializes
every related row, and on the SQLAlchemy backend,
`ModelView.find_all` / `find_by_pk` / `find_by_pks`
(`starlette_admin/contrib/sqla/view.py`) `joinedload`s the *entire*
relationship for every `RelationField`, unconditionally, before the query
even runs. An author with 50,000 books turns one list-page load into a
multi-hundred-thousand-row join, every time. See `starlette_admin_paginated_relations/fields.py`'s
module docstring for the full breakdown, and
`starlette_admin_paginated_relations/view_mixin.py`'s for exactly which
internals this had to work around.

## The fix

`PaginatedHasMany` never touches the ORM-loaded relationship for list/detail
rendering. It fetches one page of children directly — filtered by foreign
key, scoped through the child view's own `get_list_query` (so tenant /
soft-delete / access-control scoping you've already written still applies),
and paged with [`sqlakeyset`](https://github.com/djrobstep/sqlakeyset) so
paging cost stays flat regardless of collection size. The first page renders
inline; Prev/Next clicks fetch further pages from one small JSON route.

## Install

```bash
pip install -e /path/to/starlette_admin_paginated_relations  # or copy the package into your project
```

Requires `starlette-admin`, `sqlalchemy>=2.0`, `sqlakeyset>=2.0`, `anyio`.

**Version note:** built and tested against the jowilf/starlette-admin
checkout at commit `c715eb8dcc38d769b4f0fea4ea318da868cfb896` (dataclass-based
fields, the plugin system, `RequestAction`). If your installed
`starlette-admin` predates that architecture, check that `HasMany`/`BaseField`
are dataclasses and that `starlette_admin.plugins.BasePlugin` exists before
relying on this as-is.

## Usage

```python
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.fields import IntegerField, StringField, HasOne
from starlette_admin_paginated_relations import (
    PaginatedHasMany,
    PaginatedRelationsModelView,
    PaginatedRelationsPlugin,
)

class AuthorView(PaginatedRelationsModelView):   # <- not plain ModelView
    fields = [
        IntegerField("id"),
        StringField("name"),
        PaginatedHasMany("books", key="book", page_size=10),
    ]

class BookView(ModelView):
    fields = [IntegerField("id"), StringField("title"), HasOne("author", key="author")]

admin = Admin(engine, plugins=[PaginatedRelationsPlugin()])  # <- register the plugin once
admin.add_view(AuthorView(Author, key="author"))
admin.add_view(BookView(Book, key="book"))
```

Two things are load-bearing and easy to forget:

* **`AuthorView` must inherit `PaginatedRelationsModelView`**, not plain
  `ModelView`. Without it, the field still works, but the automatic
  `joinedload` this field exists to avoid still happens underneath it — see
  "Scope and caveats" below.
* **`PaginatedRelationsPlugin()` must be passed to `Admin(plugins=[...])`.**
  Without it, the field's templates and JS 404 (they only get wired into the
  Jinja loader / static mount for plugins the admin was actually given).

## Scope and caveats

* **SQLAlchemy only** (`starlette_admin.contrib.sqla`). `pagination.py` uses
  `sqlalchemy.inspect(...).relationships[...]` and the SQLA `ModelView`'s
  `get_list_query`/`_pk_column`.
* **Single-column foreign keys only.** A composite FK raises a visible (not
  a 500) `PaginationConfigError` at request time; use plain `HasMany` for
  those relationships.
* **Create/edit forms are unaffected by design.** `HasMany`'s multi-select
  widget already paginates and searches via the built-in `relation-lookup`
  endpoint — that was never the bottleneck. Pre-selecting the *current*
  value in an edit form, though, still means loading every currently-related
  pk (the same unbounded-load problem, just for the form), so
  `PaginatedHasMany` defaults `exclude_from_create` / `exclude_from_edit` /
  `exclude_from_export` to `True`. Pass `exclude_from_edit=False` etc.
  explicitly per-field if you've decided that trade-off is fine for a given
  relationship — it then falls back to ordinary `HasMany` behavior for that
  one action.
* **Prev/Next, not jump-to-page.** Keyset (seek) pagination is what lets
  this stay fast on a huge relationship, but it fundamentally doesn't
  support random-access "jump to page 47" the way OFFSET pagination does.
  The UI shows a running "N of TOTAL" count with Prev/Next buttons rather
  than clickable page numbers. If you need arbitrary page jumps for a
  bounded relation size, plain `HasMany` (or a bespoke OFFSET-based field)
  is a better fit than this one.
* **No search-within-relation** in this version — only Prev/Next. Add a `q`
  param to `pagination.build_child_query` if you need it.
* **`view_mixin.py` duplicates internal logic** from
  `starlette_admin.contrib.sqla.view.ModelView` (there's no hook to exclude
  one field from the library's automatic eager-load loop without overriding
  the method it lives in). Pin your `starlette-admin` version, and diff
  `view_mixin.py`'s three methods against the installed library after any
  upgrade — see that file's docstring for specifics.

## Package layout

```
starlette_admin_paginated_relations/
  fields.py        PaginatedHasMany
  pagination.py    FK detection + sqlakeyset paging (sync + async sessions)
  view_mixin.py     PaginatedRelationsModelView (skips the eager-load for this field)
  plugin.py        PaginatedRelationsPlugin (the /page JSON route)
  templates/plugins/paginated-relations/fields/...
  static/plugins/paginated-relations/js/paginated_relation.js
example/
  app.py                       runnable demo: one author with 3,000 books
  test_pagination.py           coverage/no-dup paging, join-avoidance proof
  test_async_and_scoping.py    async session, tenant-scoping, soft-fail checks
```

## Verifying it yourself

```bash
pip install -e .
python example/app.py                 # seeds a demo DB, serves http://127.0.0.1:8000/admin
python example/test_pagination.py             # sync engine: coverage + join-avoidance proof
python example/test_async_and_scoping.py      # async engine, tenant scoping, soft-fail
```

All three ran clean against this checkout, including a SQL-statement-level
check (via `sqlalchemy.event`) that rendering a list page of authors never
runs a query joining the full `books` table — only bounded, per-author
`LIMIT`/`COUNT` queries — and a check that a tenant-scoped view's
`get_list_query()` override is still honored inside the paginated relation
query.
