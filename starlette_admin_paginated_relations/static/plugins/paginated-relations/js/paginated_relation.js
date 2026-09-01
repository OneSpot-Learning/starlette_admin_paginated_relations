/* Prev/Next pager for PaginatedHasMany fields.
 *
 * NOTE: this deliberately does NOT use starlette-admin's
 * `window.StarletteAdmin.registerFieldInitializer` plugin hook, even though
 * that's the pattern the "Plugins" doc describes. In practice
 * `registerFieldInitializer` only exists once `static/js/form.js` has
 * loaded, and core only loads `form.js` on the list page when the view has
 * inline-editable fields enabled (`list.html`'s `{% if inline_edit_enabled %}`
 * guard) -- and never on the plain detail page at all. PaginatedHasMany is
 * neither inline-editable nor a form field, so on a typical list/detail
 * page `window.StarletteAdmin.registerFieldInitializer` simply doesn't
 * exist, and calling it throws before any click handler is ever attached
 * (the "Next button doesn't work" bug this file used to have).
 *
 * Instead: one delegated click listener on `document`, bound once. This
 * needs no per-element re-initialization at all -- delegation means a
 * `[data-sa-paginated-relation]` container inserted later (e.g. core
 * replacing a whole `<tr>` after an inline edit of some *other* field on
 * the same row) is handled automatically, with no re-scan step to forget.
 */
(function () {
  "use strict";

  function renderItems(container, items) {
    var itemsEl = container.querySelector("[data-sa-relation-items]");
    if (!itemsEl) return;
    itemsEl.innerHTML = "";
    if (!items || !items.length) {
      var none = document.createElement("span");
      none.className = "text-muted";
      none.textContent = "None";
      itemsEl.appendChild(none);
      return;
    }
    items.forEach(function (item, i) {
      var meta = (item && item._meta) || {};
      var a = document.createElement("a");
      a.href = meta.detailUrl || "#";
      a.textContent = meta.repr != null ? meta.repr : String(item);
      itemsEl.appendChild(a);
      if (i < items.length - 1) {
        itemsEl.appendChild(document.createTextNode(", "));
      }
    });
  }

  function applyPage(container, data) {
    if (!data || data.error) {
      var itemsEl = container.querySelector("[data-sa-relation-items]");
      if (itemsEl) {
        itemsEl.innerHTML = "";
        var err = document.createElement("span");
        err.className = "text-danger";
        err.textContent = (data && data.error) || "Failed to load";
        itemsEl.appendChild(err);
      }
      return;
    }

    renderItems(container, data.items);
    container.dataset.bookmarkNext = data.bookmark_next || "";
    container.dataset.bookmarkPrevious = data.bookmark_previous || "";

    var prevBtn = container.querySelector("[data-sa-relation-prev]");
    var nextBtn = container.querySelector("[data-sa-relation-next]");
    var pager = container.querySelector("[data-sa-relation-pager]");
    var range = container.querySelector("[data-sa-relation-range]");
    var total = data.total || 0;
    var showPager = total > (data.items || []).length || data.has_previous;

    if (pager) {
      pager.style.display = showPager ? "" : "none";
    }
    if (prevBtn) prevBtn.disabled = !data.has_previous;
    if (nextBtn) nextBtn.disabled = !data.has_next;
    if (range) {
      range.textContent = (data.items || []).length + " of " + total;
    }
  }

  function fetchPage(container, bookmark) {
    var base = container.dataset.pageUrl;
    if (!base) return;
    var url = new URL(base, window.location.href);
    url.searchParams.set("view", container.dataset.view);
    url.searchParams.set("field", container.dataset.field);
    url.searchParams.set("pk", container.dataset.pk);
    // So links rendered on this page ("back to list") point at the real
    // page the user is looking at, not at this JSON endpoint's own URL --
    // see plugin.py's `origin` handling.
    url.searchParams.set("origin", window.location.pathname + window.location.search);
    if (bookmark) {
      url.searchParams.set("page", bookmark);
    } else {
      url.searchParams.delete("page");
    }

    var prevBtn = container.querySelector("[data-sa-relation-prev]");
    var nextBtn = container.querySelector("[data-sa-relation-next]");
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;

    fetch(url.toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (res) {
        return res.json().catch(function () {
          return { error: "Unexpected response (" + res.status + ")" };
        });
      })
      .then(function (data) {
        applyPage(container, data);
      })
      .catch(function () {
        applyPage(container, { error: "Network error" });
      });
  }

  // Guard against this script somehow being included twice on one page
  // (e.g. once for a list field, once for a detail field) -- a delegated
  // listener doesn't need re-binding per element, but it does need to not
  // be attached twice over.
  if (window.__saPaginatedRelationBound) return;
  window.__saPaginatedRelationBound = true;

  document.addEventListener("click", function (evt) {
    var prevBtn = evt.target.closest("[data-sa-relation-prev]");
    var nextBtn = evt.target.closest("[data-sa-relation-next]");
    var btn = prevBtn || nextBtn;
    if (!btn || btn.disabled) return;

    var container = btn.closest("[data-sa-paginated-relation]");
    if (!container) return;

    var bookmark = prevBtn
      ? container.dataset.bookmarkPrevious
      : container.dataset.bookmarkNext;
    fetchPage(container, bookmark);
  });
})();
