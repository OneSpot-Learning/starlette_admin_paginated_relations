/* Prev/Next pager for PaginatedHasMany fields.
 *
 * Follows the plugin frontend contract from starlette-admin's "Plugins" doc:
 * query only inside the `container` passed to the initializer (never the
 * global `document`), be idempotent (core reruns initializers after every
 * inline-row / fragment insert), and read configuration from data-*
 * attributes rendered by fields/_macro.html.
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

  function initPaginatedRelation(container) {
    if (container.dataset.saBound) return;
    container.dataset.saBound = "1";
    container.addEventListener("click", function (evt) {
      var prevBtn = evt.target.closest("[data-sa-relation-prev]");
      var nextBtn = evt.target.closest("[data-sa-relation-next]");
      if (prevBtn && !prevBtn.disabled) {
        fetchPage(container, container.dataset.bookmarkPrevious);
      } else if (nextBtn && !nextBtn.disabled) {
        fetchPage(container, container.dataset.bookmarkNext);
      }
    });
  }

  window.StarletteAdmin.registerFieldInitializer(function (element) {
    element
      .querySelectorAll("[data-sa-paginated-relation]")
      .forEach(initPaginatedRelation);
  });
})();
