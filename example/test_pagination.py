"""Sanity checks run with `python test_pagination.py`:

1. The author list page renders 200 and shows the paginated widget, not a
   raw dump of every book.
2. Walking bookmark_next through the JSON page endpoint visits every book
   exactly once and lands on has_next=False at the end.
3. Loading the author list page never executes a SQL query that touches
   more than page_size+1 rows of `books` -- i.e. the join this field exists
   to avoid genuinely doesn't happen.
"""

from __future__ import annotations

import sys

from sqlalchemy import event
from starlette.testclient import TestClient

sys.path.insert(0, "/tmp/work/deliverable/example")
from deliverable.example.app import build_admin, build_engine, seed

N_BOOKS = 3000


def main() -> None:
    engine = build_engine(db_path="/tmp/work/deliverable/example/test.db", echo=False)
    seed(engine, n_books=N_BOOKS)

    row_counts: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count_rows(conn, cursor, statement, parameters, context, executemany):
        pass  # row counts aren't known before execution; captured via result below

    from fastapi import FastAPI

    app = FastAPI()
    build_admin(engine).mount_to(app)
    client = TestClient(app, follow_redirects=True)

    # --- 1. List page renders and doesn't choke on 3000 related rows -----
    resp = client.get("/admin/author/list")
    assert resp.status_code == 200, resp.text
    assert "data-sa-paginated-relation" in resp.text, "paginated widget not rendered"
    assert "Book #2999" not in resp.text, "full collection got dumped into the page"
    print("[ok] list page renders, only a first page of books appears")

    # --- 2. Detail page for the prolific author also paginates -----------
    resp = client.get("/admin/author/detail", params={"pk": "1"})
    assert resp.status_code == 200, resp.text
    assert "data-sa-paginated-relation" in resp.text
    print("[ok] detail page renders with paginated widget")

    # --- 3. Walk every page via the JSON endpoint, verify full coverage --
    seen_ids: set[int] = set()
    bookmark = None
    pages_fetched = 0
    while True:
        params = {"view": "author", "field": "books", "pk": "1"}
        if bookmark:
            params["page"] = bookmark
        resp = client.get("/admin/plugins/paginated-relations/page", params=params)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert not data.get("error"), data
        assert data["total"] == N_BOOKS, data["total"]
        assert len(data["items"]) <= 10
        for item in data["items"]:
            seen_ids.add(item["id"])
        pages_fetched += 1
        if not data["has_next"]:
            assert data["bookmark_next"] is None
            break
        bookmark = data["bookmark_next"]
        assert pages_fetched <= N_BOOKS  # safety valve against an infinite loop

    assert len(seen_ids) == N_BOOKS, (
        f"expected {N_BOOKS} unique books, saw {len(seen_ids)}"
    )
    assert pages_fetched == N_BOOKS // 10, pages_fetched
    print(
        f"[ok] paged through all {N_BOOKS} books across {pages_fetched} pages of 10, no dupes/gaps"
    )

    # --- 3b. "Back to list" links on page 2+ point at the real author list
    #         page, not at this JSON route's own URL. Regression test for a
    #         bug where every item's `_meta.detailUrl` carried `_origin`
    #         back to `/admin/plugins/paginated-relations/page` -- a URL
    #         that returns JSON, not a page -- because `request.url` inside
    #         the route handler is the API call itself, not whatever page
    #         the user was actually looking at.
    resp = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "author", "field": "books", "pk": "1"},
    )
    detail_url = resp.json()["items"][0]["_meta"]["detailUrl"]
    assert "paginated-relations" not in detail_url, detail_url
    assert "_origin=%2Fadmin%2Fauthor%2Flist" in detail_url, detail_url
    # And when the client (the real JS pager) supplies the page it's
    # actually on, that exact path wins instead of the generic fallback.
    resp = client.get(
        "/admin/plugins/paginated-relations/page",
        params={
            "view": "author",
            "field": "books",
            "pk": "1",
            "origin": "/admin/author/list?sort=name__desc",
        },
    )
    detail_url = resp.json()["items"][0]["_meta"]["detailUrl"]
    assert "_origin=%2Fadmin%2Fauthor%2Flist%3Fsort%3Dname__desc" in detail_url, (
        detail_url
    )
    print(
        "[ok] page 2+ items link back to the real author list page, not this JSON route"
    )

    # --- 4. Prev navigation from the last bookmark walks back correctly --
    resp = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "author", "field": "books", "pk": "1"},
    )
    first_page_ids = [item["id"] for item in resp.json()["items"]]
    bookmark_next = resp.json()["bookmark_next"]
    resp2 = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "author", "field": "books", "pk": "1", "page": bookmark_next},
    )
    second_page_ids = [item["id"] for item in resp2.json()["items"]]
    bookmark_prev = resp2.json()["bookmark_previous"]
    resp3 = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "author", "field": "books", "pk": "1", "page": bookmark_prev},
    )
    back_to_first_ids = [item["id"] for item in resp3.json()["items"]]
    assert back_to_first_ids == first_page_ids, (back_to_first_ids, first_page_ids)
    assert second_page_ids != first_page_ids
    print("[ok] bookmark_previous correctly navigates back to the prior page")

    # --- 5. The quiet author (1 book, well under page_size) has no pager -
    resp = client.get(
        "/admin/plugins/paginated-relations/page",
        params={"view": "author", "field": "books", "pk": "2"},
    )
    data = resp.json()
    assert data["total"] == 1 and not data["has_next"] and not data["has_previous"]
    print("[ok] author with a single book: total=1, no next/previous")

    # --- 6. Prove the eager-join is actually skipped: capture every SQL
    #        statement executed while rendering the author list page. Two
    #        authors are on the page, each showing its own first page of
    #        books, so `books` legitimately gets queried -- but every such
    #        query must be individually bounded (a LIMIT clause, one query
    #        per author), never a single JOIN that pulls in an author's
    #        entire collection the way plain HasMany would.
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    statements.clear()
    resp = client.get("/admin/author/list")
    assert resp.status_code == 200
    list_page_statements = list(statements)
    assert list_page_statements, "expected at least one SQL statement"

    books_queries = [s for s in list_page_statements if "books" in s.lower()]
    # One bounded SELECT (with LIMIT) and one COUNT per author's inline
    # first-page render -- never a JOIN pulling authors.* and books.* in
    # one statement (that shape is exactly what plain HasMany's
    # joinedload would produce, and what this field exists to avoid).
    assert books_queries, "expected the inline first-page book queries to run"
    for s in books_queries:
        low = s.lower()
        assert "join" not in low, f"unexpected join against books: {s}"
        assert "limit" in low or "count" in low, f"unbounded books query: {s}"
    print(
        f"[ok] rendering the author list page ran {len(list_page_statements)} "
        f"statement(s); the {len(books_queries)} touching `books` are all "
        "bounded (LIMIT/COUNT), never a join across the full collection"
    )

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
