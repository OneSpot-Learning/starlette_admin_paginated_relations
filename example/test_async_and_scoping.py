"""Additional checks beyond test_pagination.py:

1. Async engine (AsyncSession) path works end-to-end (sqlakeyset.asyncio).
2. A view that overrides get_list_query() for tenant scoping keeps that
   scoping applied inside the paginated relation query too.
3. A PaginatedHasMany pointed at a nonexistent relationship name fails
   soft (an {"error": ...} payload), not a 500.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import ForeignKey, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette_admin.contrib.sqla import Admin
from starlette_admin.fields import IntegerField, StringField

sys.path.insert(0, "/tmp/work/deliverable/example")
from deliverable.starlette_admin_paginated_relations import (
    PaginatedHasMany,
    PaginatedRelationsModelView,
    PaginatedRelationsPlugin,
)


# --- 1. Async engine ---------------------------------------------------


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    books: Mapped[list["Book"]] = relationship(back_populates="author")


class Book(Base):
    __tablename__ = "books"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"))
    author: Mapped["Author"] = relationship(back_populates="books")


class AuthorView(PaginatedRelationsModelView):
    fields = [IntegerField("id"), StringField("name"), PaginatedHasMany("books", key="book", page_size=5)]


class BookView(PaginatedRelationsModelView):
    fields = [IntegerField("id"), StringField("title")]


def test_async_engine() -> None:
    engine = create_async_engine("sqlite+aiosqlite:////tmp/work/deliverable/example/test_async.db")

    async def seed_async():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as session:
            a = Author(name="Async Author")
            session.add(a)
            await session.flush()
            session.add_all(Book(title=f"B{i}", author_id=a.id) for i in range(23))
            await session.commit()

    asyncio.run(seed_async())

    admin = Admin(engine, secret_key="k" * 16, plugins=[PaginatedRelationsPlugin()])
    admin.add_view(AuthorView(Author, key="author"))
    admin.add_view(BookView(Book, key="book"))
    app = Starlette()
    admin.mount_to(app)
    client = TestClient(app)

    resp = client.get("/admin/author/list")
    assert resp.status_code == 200, resp.text
    assert "data-sa-paginated-relation" in resp.text

    seen = set()
    bookmark = None
    for _ in range(10):
        params = {"view": "author", "field": "books", "pk": "1"}
        if bookmark:
            params["page"] = bookmark
        r = client.get("/admin/plugins/paginated-relations/page", params=params)
        assert r.status_code == 200, r.text
        data = r.json()
        for item in data["items"]:
            seen.add(item["id"])
        if not data["has_next"]:
            break
        bookmark = data["bookmark_next"]
    assert len(seen) == 23, seen
    print("[ok] async engine: paginated through all 23 books via AsyncSession + sqlakeyset.asyncio")


# --- 2. Tenant-scoping is respected -------------------------------------


class TenantBook(Base):
    __tablename__ = "tenant_books"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    tenant_id: Mapped[int]
    author_id: Mapped[int] = mapped_column(ForeignKey("tenant_authors.id"))
    author: Mapped["TenantAuthor"] = relationship(back_populates="books")


class TenantAuthor(Base):
    __tablename__ = "tenant_authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    tenant_id: Mapped[int]
    books: Mapped[list["TenantBook"]] = relationship(back_populates="author")


CURRENT_TENANT = {"id": 1}


class TenantBookView(PaginatedRelationsModelView):
    fields = [IntegerField("id"), StringField("title")]

    def get_list_query(self, request):
        stmt = super().get_list_query(request)
        return stmt.where(TenantBook.tenant_id == CURRENT_TENANT["id"])


class TenantAuthorView(PaginatedRelationsModelView):
    fields = [
        IntegerField("id"),
        StringField("name"),
        PaginatedHasMany("books", key="tenant_book", page_size=5),
    ]

    def get_list_query(self, request):
        stmt = super().get_list_query(request)
        return stmt.where(TenantAuthor.tenant_id == CURRENT_TENANT["id"])


def test_tenant_scoping_applies_to_paginated_relation() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:////tmp/work/deliverable/example/test_tenant.db")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        a1 = TenantAuthor(name="Tenant 1 Author", tenant_id=1)
        session.add(a1)
        session.flush()
        # Same author id space; books for tenant 1 and a decoy set that
        # LOOKS like it belongs to author 1 but is tagged tenant 2 --
        # get_list_query's tenant filter must exclude it from the page.
        session.add_all(
            TenantBook(title=f"T1-{i}", tenant_id=1, author_id=a1.id) for i in range(7)
        )
        session.commit()

        # A second tenant's book pointing at the SAME author row would be a
        # data modeling error in a real app (FK would need tenant scoping
        # too); instead prove scoping via a decoy book smuggled in with a
        # different tenant_id directly on a row that shares the author FK.
        session.add(TenantBook(title="Smuggled", tenant_id=2, author_id=a1.id))
        session.commit()

    admin = Admin(engine, secret_key="k" * 16, plugins=[PaginatedRelationsPlugin()])
    admin.add_view(TenantAuthorView(TenantAuthor, key="tenant_author"))
    admin.add_view(TenantBookView(TenantBook, key="tenant_book"))
    app = Starlette()
    admin.mount_to(app)
    client = TestClient(app)

    resp = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "tenant_author", "field": "books", "pk": "1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    titles = {item["title"] for item in data["items"]}
    assert "Smuggled" not in titles, (
        "tenant scoping from TenantAuthorView.get_list_query was NOT applied "
        f"to the paginated relation query: {titles}"
    )
    assert data["total"] == 7, data["total"]
    print("[ok] tenant scoping on get_list_query is respected by the paginated relation query")


# --- 3. Misconfigured field fails soft ----------------------------------


class BrokenAuthorView(PaginatedRelationsModelView):
    fields = [
        IntegerField("id"),
        StringField("name"),
        PaginatedHasMany("not_a_real_relationship", key="book", page_size=5),
    ]


def test_bad_relationship_name_fails_soft() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:////tmp/work/deliverable/example/test_broken.db")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Author(name="Solo"))
        session.commit()

    admin = Admin(engine, secret_key="k" * 16, plugins=[PaginatedRelationsPlugin()])
    admin.add_view(BrokenAuthorView(Author, key="author"))
    admin.add_view(BookView(Book, key="book"))
    app = Starlette()
    admin.mount_to(app)
    client = TestClient(app)

    resp = client.get("/admin/author/list")
    assert resp.status_code == 200, resp.text  # doesn't 500 the whole page
    assert "no SQLAlchemy relationship named" in resp.text
    print("[ok] a PaginatedHasMany naming a nonexistent relationship fails soft (visible error, no 500)")


if __name__ == "__main__":
    test_async_engine()
    test_tenant_scoping_applies_to_paginated_relation()
    test_bad_relationship_name_fails_soft()
    print("\nAll additional checks passed.")
