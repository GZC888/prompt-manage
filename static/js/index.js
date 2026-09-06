/* index.js — library page: layout toggle, mobile filter drawer, instant search.
   Instant search is progressive enhancement: the server can render the library
   on its own (X-Partial: library), and without JS the form still submits. */
(function () {
  "use strict";

  const root = document.documentElement;
  const tr = window.tr || ((zh) => zh);
  const mobileQuery = window.matchMedia("(max-width: 900px)");
  let searchTimer = null;
  let fetchSeq = 0;

  // --- Layout (grid / list) -------------------------------------------------
  function setViewMode(mode) {
    const next = mode === "list" ? "list" : "grid";
    root.setAttribute("data-view-mode", next);
    try { localStorage.setItem("viewMode", next); } catch (error) { /* not persisted */ }
    document.querySelectorAll("[data-view-mode-target]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.viewModeTarget === next ? "true" : "false");
    });
  }

  // --- Mobile filter drawer -------------------------------------------------
  function setDrawer(open, restoreFocus) {
    const next = mobileQuery.matches && Boolean(open);
    const toggle = document.getElementById("sidebarToggle");
    const panel = document.getElementById("sidebarPanel");
    const scrim = document.getElementById("drawerScrim");
    root.setAttribute("data-sidebar-open", next ? "true" : "false");
    document.body.classList.toggle("drawer-open", next);
    if (scrim) scrim.hidden = !next;
    toggle?.setAttribute("aria-expanded", next ? "true" : "false");
    if (panel) {
      const hide = mobileQuery.matches && !next;
      panel.toggleAttribute("inert", hide);
      panel.setAttribute("aria-hidden", hide ? "true" : "false");
    }
    if (next) document.getElementById("drawerClose")?.focus();
    else if (restoreFocus) toggle?.focus({ preventScroll: true });
  }

  document.addEventListener("keydown", (event) => {
    const open = root.getAttribute("data-sidebar-open") === "true";
    if (!open) return;
    if (event.key === "Escape") { setDrawer(false, true); return; }
    const panel = document.getElementById("sidebarPanel");
    if (panel) window.trapFocus?.(event, panel);
  });
  mobileQuery.addEventListener?.("change", () => setDrawer(false, false));

  // --- Facet filters --------------------------------------------------------
  function applyFacets() {
    const params = new URLSearchParams();
    const current = new URLSearchParams(location.search);
    ["view", "sort", "q"].forEach((key) => {
      const value = current.get(key);
      if (value) params.set(key, value);
    });
    document.querySelectorAll('input[name="ftag"]:checked').forEach((input) => params.append("tag", input.value));
    document.querySelectorAll('input[name="fsrc"]:checked').forEach((input) => params.append("source", input.value));
    navigate(params);
  }

  // --- Instant search -------------------------------------------------------
  function formParams() {
    const form = document.querySelector("form[data-live-search]");
    const params = new URLSearchParams();
    if (!form) return params;
    new FormData(form).forEach((value, key) => {
      if (typeof value === "string" && value !== "") params.append(key, value);
    });
    // "all" and "updated" are the defaults; leaving them out keeps URLs short.
    if (params.get("view") === "all") params.delete("view");
    if (params.get("sort") === "updated") params.delete("sort");
    return params;
  }

  function navigate(params) {
    const url = location.pathname + (params.toString() ? "?" + params.toString() : "");
    const shell = document.getElementById("libraryShell");
    if (!shell || !window.fetch || !window.DOMParser) { location.assign(url); return; }

    const seq = ++fetchSeq;
    const input = document.getElementById("promptSearch");
    const hadFocus = input && document.activeElement === input;
    const caret = input ? input.selectionStart : null;
    shell.setAttribute("aria-busy", "true");

    fetch(url, { credentials: "same-origin", headers: { "X-Partial": "library" } })
      .then((response) => { if (!response.ok) throw new Error("bad status"); return response.text(); })
      .then((html) => {
        if (seq !== fetchSeq) return;
        const fresh = new DOMParser().parseFromString(html, "text/html").getElementById("libraryShell");
        if (!fresh) throw new Error("no library in response");
        shell.replaceWith(fresh);
        history.replaceState(null, "", url);
        bind();
        const nextInput = document.getElementById("promptSearch");
        if (hadFocus && nextInput) {
          nextInput.focus({ preventScroll: true });
          try { if (caret !== null) nextInput.setSelectionRange(caret, caret); } catch (error) { /* ignore */ }
        }
      })
      .catch(() => {
        if (seq !== fetchSeq) return;
        // Fall back to a normal navigation rather than leaving a stale list.
        location.assign(url);
      });
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => navigate(formParams()), 220);
  }

  // --- Optimistic pin toggle ------------------------------------------------
  function bindToggles() {
    document.querySelectorAll(".js-toggle-form").forEach((form) => {
      const button = form.querySelector("button[type='submit']");
      if (!button) return;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (button.disabled) return;
        const wasOn = button.classList.contains("is-on");
        button.disabled = true;
        try {
          const response = await window.csrfFetch(form.action, {
            headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
          });
          if (!response.ok) throw new Error("toggle failed");
          const enabled = Boolean((await response.json()).enabled);
          button.classList.toggle("is-on", enabled);
          button.setAttribute("aria-pressed", enabled ? "true" : "false");
          const svg = button.querySelector("svg");
          if (svg) {
            svg.setAttribute("fill", enabled ? "currentColor" : "none");
            svg.setAttribute("stroke", enabled ? "none" : "currentColor");
          }
        } catch (error) {
          button.classList.toggle("is-on", wasOn);
          window.toast?.(tr("操作失败，请重试", "Action failed. Please try again."), "error");
        } finally {
          button.disabled = false;
        }
      });
    });
  }

  // --- Wiring (re-run after every instant-search swap) ----------------------
  function bind() {
    setViewMode(root.getAttribute("data-view-mode") || "grid");
    setDrawer(false, false);

    document.getElementById("viewModeToggle")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-view-mode-target]");
      if (button) setViewMode(button.dataset.viewModeTarget);
    });
    document.getElementById("sidebarToggle")?.addEventListener("click", () => setDrawer(true, false));
    document.getElementById("drawerClose")?.addEventListener("click", () => setDrawer(false, true));
    document.getElementById("drawerScrim")?.addEventListener("click", () => setDrawer(false, true));

    document.querySelectorAll('input[name="ftag"], input[name="fsrc"]')
      .forEach((input) => input.addEventListener("change", applyFacets));
    document.querySelectorAll(".reset-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const selector = button.dataset.reset === "tags" ? 'input[name="ftag"]' : 'input[name="fsrc"]';
        document.querySelectorAll(selector).forEach((input) => { input.checked = false; });
        applyFacets();
      });
    });

    document.getElementById("sortSelect")?.addEventListener("change", () => navigate(formParams()));
    const search = document.getElementById("promptSearch");
    if (search) {
      search.addEventListener("input", () => {
        const clear = document.querySelector("[data-search-clear]");
        if (clear) clear.hidden = !search.value;
        scheduleSearch();
      });
    }
    document.querySelector("[data-search-clear]")?.addEventListener("click", () => {
      const input = document.getElementById("promptSearch");
      if (!input) return;
      input.value = "";
      input.focus();
      navigate(formParams());
    });
    document.querySelector("form[data-live-search]")?.addEventListener("submit", (event) => {
      event.preventDefault();
      navigate(formParams());
    });

    bindToggles();
  }

  bind();

  // "/" focuses search, the way most content tools behave.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const input = document.getElementById("promptSearch");
    if (!input) return;
    event.preventDefault();
    input.focus();
    input.select();
  });
})();
