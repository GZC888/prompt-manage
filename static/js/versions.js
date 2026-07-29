/* versions.js — diff selector dialog. */
(function () {
  "use strict";
  const LANG = window.APP_LANG || "zh";
  const tr = (zh, en) => (LANG === "en" ? en : zh);

  const modal = document.getElementById("diffModal");
  const openBtn = document.getElementById("openDiffSelector");
  if (!modal || !openBtn) return;

  const left = document.getElementById("leftVersion");
  const right = document.getElementById("rightVersion");
  const doDiff = document.getElementById("doDiff");
  const panel = modal.querySelector(".versions-modal-panel");
  let lastFocus = null;

  function open() {
    if (!modal.hidden) return;
    lastFocus = document.activeElement;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    openBtn.setAttribute("aria-expanded", "true");
    document.body.classList.add("modal-open");
    // Smart default: left = previous (2nd newest), right = newest.
    if (right) right.selectedIndex = 0;
    if (left && left.options.length > 1) left.selectedIndex = 1;
    if (left) left.focus();
  }
  function close() {
    if (modal.hidden) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    openBtn.setAttribute("aria-expanded", "false");
    document.body.classList.remove("modal-open");
    const target = lastFocus && lastFocus.isConnected ? lastFocus : openBtn;
    if (target && target.focus) target.focus({ preventScroll: true });
  }

  function trapFocus(e) {
    if (e.key !== "Tab" || modal.hidden || !panel) return;
    const focusable = Array.from(panel.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((el) => !el.hidden && el.getAttribute("aria-hidden") !== "true");
    if (!focusable.length) { e.preventDefault(); panel.focus(); return; }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  openBtn.addEventListener("click", open);
  modal.addEventListener("keydown", trapFocus);
  modal.querySelectorAll("[data-modal-close]").forEach((el) => el.addEventListener("click", close));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) close(); });

  if (doDiff) {
    doDiff.addEventListener("click", () => {
      const l = left && left.value, r = right && right.value;
      if (!l || !r) { window.toast(tr("请选择要对比的版本", "Please select versions to compare"), "error"); return; }
      if (l === r) { window.toast(tr("请选择两个不同的版本进行对比", "Please select two different versions"), "error"); return; }
      window.location.href = window.DIFF_BASE + "?left=" + encodeURIComponent(l) + "&right=" + encodeURIComponent(r);
    });
  }
})();
