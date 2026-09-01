"""Runnable demo: one Author with 3,000 Books, rendered through
PaginatedHasMany. Run directly to seed a fresh sqlite DB and print the admin
URL; also imported by test_pagination.py for an in-process TestClient check.
"""

from __future__ import annotations

import os

from sqlalchemy import ForeignKey, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.fields import HasOne, IntegerField, StringField

from deliverable.starlette_admin_paginated_relations import (
    PaginatedHasMany,
    PaginatedRelationsModelView,
    PaginatedRelationsPlugin,
)

DB_PATH = os.environ.get("DEMO_DB_PATH", "/tmp/work/deliverable/example/demo.db")


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
    fields = [
        IntegerField("id"),
        StringField("name"),
        PaginatedHasMany("books", key="book", page_size=10),
    ]


class BookView(ModelView):
    fields = [
        IntegerField("id"),
        StringField("title"),
        HasOne("author", key="author"),
    ]


def build_engine(db_path: str = DB_PATH, echo: bool = False):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, echo=echo)
    return engine


def seed(engine, n_books: int = 3000) -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        prolific = Author(name="Prolific Author")
        quiet = Author(name="Quiet Author")
        session.add_all([prolific, quiet])
        session.flush()
        session.add_all(
            Book(title=f"Book #{i}", author_id=prolific.id) for i in range(n_books)
        )
        session.add(Book(title="Only Book", author_id=quiet.id))
        session.commit()


def build_admin(engine) -> Admin:
    admin = Admin(
        engine,
        title="Paginated Relations Demo",
        secret_key="demo-only-not-for-production",
        plugins=[PaginatedRelationsPlugin()],
    )
    admin.add_view(AuthorView(Author, key="author", icon="fa fa-user"))
    admin.add_view(BookView(Book, key="book", icon="fa fa-book"))
    return admin


def build_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    engine = build_engine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(lifespan=lifespan)
    build_admin(engine).mount_to(app)
    return app, engine


if __name__ == "__main__":
    import uvicorn

    engine = build_engine()
    seed(engine)
    app, _ = build_app()
    print(f"Seeded {DB_PATH} with 3,000 books for one author.")
    print("Open http://127.0.0.1:8000/admin")
    uvicorn.run(app, host="127.0.0.1", port=8000)
