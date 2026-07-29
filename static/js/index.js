(function () {
  "use strict";

  const root = document.documentElement;
  const viewControl = document.getElementById("viewModeToggle");

  function setViewMode(mode) {
    const next = mode === "list" ? "list" : "grid";
    root.setAttribute("data-view-mode", next);
    try { localStorage.setItem("viewMode", next); } catch (error) {}
    if (viewControl) {
      viewControl.querySelectorAll("[data-view-mode-target]").forEach((button) => {
        button.setAttribute("aria-pressed", button.dataset.viewModeTarget === next ? "true" : "false");
      });
    }
  }

  if (viewControl) {
    setViewMode(root.getAttribute("data-view-mode"));
    viewControl.addEventListener("click", (event) => {
      const button = event.target.closest("[data-view-mode-target]");
      if (button) setViewMode(button.dataset.viewModeTarget);
    });
  }

  const openButton = document.getElementById("sidebarToggle");
  const closeButton = document.getElementById("drawerClose");
  const scrim = document.getElementById("drawerScrim");
  const panel = document.getElementById("sidebarPanel");
  const mobileQuery = window.matchMedia("(max-width: 900px)");

  function setSidebar(open, restoreFocus) {
    const mobile = mobileQuery.matches;
    const next = mobile && Boolean(open);
    root.setAttribute("data-sidebar-open", next ? "true" : "false");
    if (scrim) scrim.hidden = !next;
    if (openButton) openButton.setAttribute("aria-expanded", next ? "true" : "false");
    document.body.classList.toggle("drawer-open", next);
    if (panel) {
      panel.toggleAttribute("inert", mobile && !next);
      panel.setAttribute("aria-hidden", mobile && !next ? "true" : "false");
    }
    if (next && closeButton) closeButton.focus();
    if (!next && restoreFocus && openButton) openButton.focus({ preventScroll: true });
  }

  setSidebar(false, false);
  if (openButton) openButton.addEventListener("click", () => setSidebar(true, false));
  if (closeButton) closeButton.addEventListener("click", () => setSidebar(false, true));
  if (scrim) scrim.addEventListener("click", () => setSidebar(false, true));
  mobileQuery.addEventListener?.("change", () => setSidebar(false, false));

  document.addEventListener("keydown", (event) => {
    const open = root.getAttribute("data-sidebar-open") === "true";
    if (event.key === "Escape" && open) {
      setSidebar(false, true);
      return;
    }
    if (event.key !== "Tab" || !open || !panel) return;
    const focusable = Array.from(panel.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((element) => !element.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  function applyFilters() {
    const params = new URLSearchParams();
    const current = new URLSearchParams(location.search);
    ["q", "sort", "view"].forEach((key) => {
      const value = current.get(key);
      if (value) params.set(key, value);
    });
    document.querySelectorAll('input[name="ftag"]:checked').forEach((input) => params.append("tag", input.value));
    document.querySelectorAll('input[name="fsrc"]:checked').forEach((input) => params.append("source", input.value));
    location.assign(location.pathname + (params.toString() ? "?" + params.toString() : ""));
  }

  document.querySelectorAll('input[name="ftag"], input[name="fsrc"]').forEach((input) => input.addEventListener("change", applyFilters));
  document.querySelectorAll(".reset-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const selector = button.dataset.reset === "tags" ? 'input[name="ftag"]' : 'input[name="fsrc"]';
      document.querySelectorAll(selector).forEach((input) => { input.checked = false; });
      applyFilters();
    });
  });

  const sortSelect = document.getElementById("sortSelect");
  if (sortSelect) sortSelect.addEventListener("change", () => sortSelect.form?.requestSubmit());

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
          method: "POST",
          headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
        });
        if (!response.ok) throw new Error("toggle failed");
        const data = await response.json();
        const enabled = Boolean(data.enabled);
        button.classList.toggle("is-on", enabled);
        button.setAttribute("aria-pressed", enabled ? "true" : "false");
        const icon = button.querySelector("svg");
        if (icon) {
          icon.setAttribute("fill", enabled ? "currentColor" : "none");
          icon.setAttribute("stroke", enabled ? "none" : "currentColor");
        }
      } catch (error) {
        button.classList.toggle("is-on", wasOn);
        window.toast?.(window.APP_LANG === "en" ? "Action failed. Please try again." : "操作失败，请重试", "error");
      } finally {
        button.disabled = false;
      }
    });
  });
})();
