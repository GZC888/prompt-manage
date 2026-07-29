/* main.js — global helpers shared by every page.
   Intentionally minimal: toasts, csrfFetch, copy, theme toggle, command
   palette, color picker, confirm-forms. No JS-driven hover transforms,
   ripples, scroll observers, or blanket submit-loading. */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const LANG = window.APP_LANG || "zh";
  const tr = (zh, en) => (LANG === "en" ? en : zh);

  function csrfToken() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  // --- Toasts --------------------------------------------------------------
  function ensureToastRoot() {
    let root = $("#toastRoot");
    if (!root) {
      root = document.createElement("div");
      root.id = "toastRoot";
      root.className = "toast-root";
      document.body.appendChild(root);
    }
    return root;
  }

  function dismissToast(el) {
    el.classList.add("hide");
    setTimeout(() => el.remove(), 220);
  }

  function toast(message, type = "info", timeout = 4000) {
    const root = ensureToastRoot();
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
    const span = document.createElement("span");
    span.className = "toast-msg";
    span.textContent = message; // textContent: never inject HTML
    const close = document.createElement("button");
    close.className = "toast-close";
    close.type = "button";
    close.setAttribute("aria-label", tr("关闭", "Close"));
    close.textContent = "×";
    close.addEventListener("click", () => dismissToast(el));
    el.appendChild(span);
    el.appendChild(close);
    root.appendChild(el);
    if (timeout) setTimeout(() => dismissToast(el), timeout);
    return el;
  }
  window.toast = toast;

  // Wire up server-rendered toasts (auto-dismiss + close button).
  $$("#toastRoot .toast").forEach((el, i) => {
    const close = el.querySelector(".toast-close");
    if (close) close.addEventListener("click", () => dismissToast(el));
    setTimeout(() => dismissToast(el), 4500 + i * 400);
  });

  // --- csrfFetch -----------------------------------------------------------
  function csrfFetch(url, options = {}) {
    const opts = Object.assign({ method: "POST", credentials: "same-origin" }, options);
    opts.headers = Object.assign({ "X-CSRF-Token": csrfToken() }, options.headers || {});
    return fetch(url, opts);
  }
  window.csrfFetch = csrfFetch;

  // --- Clipboard -----------------------------------------------------------
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }
  window.copyText = copyText;

  // Delegate copy buttons (cards, version list, ...).
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".js-copy");
    if (!btn) return;
    let text = btn.getAttribute("data-content") || "";
    const copyUrl = btn.getAttribute("data-copy-url");
    if (!text && copyUrl) {
      btn.disabled = true;
      try {
        const response = await fetch(copyUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } });
        if (!response.ok) throw new Error("copy fetch failed");
        const data = await response.json();
        text = typeof data.content === "string" ? data.content : "";
      } catch (error) {
        toast(tr("读取内容失败", "Failed to load content"), "error");
        btn.disabled = false;
        return;
      }
      btn.disabled = false;
    }
    if (!text) { toast(tr("没有内容可复制", "No content to copy"), "error"); return; }
    const ok = await copyText(text);
    toast(ok ? tr("已复制", "Copied") : tr("复制失败", "Copy failed"), ok ? "success" : "error");
    const pid = btn.getAttribute("data-prompt-id");
    if (ok && pid) {
      csrfFetch(window.APP_URLS.detail + pid + "/copied").catch(() => {});
    }
  });

  // --- Theme toggle --------------------------------------------------------
  function effectiveTheme() {
    const m = (() => { try { return localStorage.getItem("theme"); } catch (e) { return null; } })();
    if (m === "light" || m === "dark") return m;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme(theme) {
    const d = document.documentElement;
    if (theme === "light" || theme === "dark") {
      try { localStorage.setItem("theme", theme); } catch (e) {}
      d.setAttribute("data-theme", theme);
    } else {
      try { localStorage.removeItem("theme"); } catch (e) {}
      d.removeAttribute("data-theme");
    }
  }
  const themeButtons = $$("#themeToggle, #themeToggleAuth");
  if (themeButtons.length) {
    const syncThemeState = () => themeButtons.forEach((button) => button.setAttribute("aria-pressed", effectiveTheme() === "dark" ? "true" : "false"));
    syncThemeState();
    themeButtons.forEach((button) => button.addEventListener("click", () => {
      applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
      syncThemeState();
    }));
  }

  // --- Product confirmation dialog ----------------------------------------
  const confirmRoot = $("#confirmDialog");
  const confirmTitle = $("#confirmTitle");
  const confirmMessage = $("#confirmMessage");
  const confirmAccept = $("#confirmAccept");
  let confirmResolve = null;
  let confirmLastFocus = null;

  function closeConfirm(result) {
    if (!confirmRoot || confirmRoot.hidden) return;
    confirmRoot.hidden = true;
    confirmRoot.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    const resolve = confirmResolve;
    confirmResolve = null;
    if (confirmLastFocus?.isConnected) confirmLastFocus.focus({ preventScroll: true });
    if (resolve) resolve(Boolean(result));
  }

  function confirmAction(message, options = {}) {
    if (!confirmRoot || !confirmAccept || !confirmMessage || !confirmTitle) {
      return Promise.resolve(window.confirm(message));
    }
    if (confirmResolve) closeConfirm(false);
    confirmLastFocus = document.activeElement;
    confirmTitle.textContent = options.title || tr("确认操作", "Confirm action");
    confirmMessage.textContent = message || "";
    confirmAccept.textContent = options.acceptLabel || tr("确认", "Confirm");
    confirmAccept.classList.toggle("danger", options.danger !== false);
    confirmRoot.hidden = false;
    confirmRoot.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    setTimeout(() => confirmAccept.focus(), 0);
    return new Promise((resolve) => { confirmResolve = resolve; });
  }
  window.confirmAction = confirmAction;

  if (confirmRoot && confirmAccept) {
    confirmAccept.addEventListener("click", () => closeConfirm(true));
    $$('[data-confirm-cancel]', confirmRoot).forEach((button) => button.addEventListener("click", () => closeConfirm(false)));
    confirmRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); closeConfirm(false); }
      if (event.key !== "Tab") return;
      const focusable = $$('button:not([disabled])', confirmRoot);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form) return;
    event.preventDefault();
    const accepted = await confirmAction(form.getAttribute("data-confirm"), {
      title: form.getAttribute("data-confirm-title") || undefined,
    });
    if (accepted) form.submit();
  }, true);

  // --- Color picker (detail page) -----------------------------------------
  (function colorPicker() {
    const text = $("#color");
    const picker = $("#colorPicker");
    const swatch = $("#colorSwatch");
    const clearBtn = $("#colorClearBtn");
    const presets = $$(".color-preset");
    if (!text || !picker || !swatch) return;
    if (text.readOnly || picker.disabled) return;
    const valid = (s) => /^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/.test((s || "").trim());
    const expand = (s) => {
      s = (s || "").trim();
      if (!valid(s)) return "";
      return s.length === 4 ? "#" + s.slice(1).split("").map((c) => c + c).join("").toLowerCase() : s.toLowerCase();
    };
    const paint = (v) => {
      const color = expand(v);
      swatch.style.background = color || "var(--surface)";
      presets.forEach((button) => button.classList.toggle("active", expand(button.dataset.color) === color));
    };
    paint(text.value);
    picker.addEventListener("input", () => { text.value = picker.value.toLowerCase(); text.classList.remove("color-invalid"); paint(text.value); });
    text.addEventListener("input", () => {
      if (!text.value) { text.classList.remove("color-invalid"); paint(""); return; }
      if (valid(text.value)) { text.classList.remove("color-invalid"); try { picker.value = expand(text.value); } catch (e) {} paint(text.value); }
      else { text.classList.add("color-invalid"); }
    });
    const open = () => { try { picker.showPicker ? picker.showPicker() : picker.click(); } catch (e) { picker.click(); } };
    swatch.addEventListener("click", open);
    swatch.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    if (clearBtn) clearBtn.addEventListener("click", () => {
      text.value = "";
      text.classList.remove("color-invalid");
      paint("");
      text.dispatchEvent(new Event("input", { bubbles: true }));
    });
    presets.forEach((button) => button.addEventListener("click", () => {
      text.value = expand(button.dataset.color);
      picker.value = text.value;
      text.classList.remove("color-invalid");
      paint(text.value);
      text.dispatchEvent(new Event("input", { bubbles: true }));
    }));
  })();

  // --- Command palette -----------------------------------------------------
  (function commandPalette() {
    const root = $("#cmdk");
    const input = $("#cmdkInput");
    const list = $("#cmdkList");
    const openBtn = $("#cmdkBtn");
    const panel = root ? $(".cmdk-panel", root) : null;
    const status = $("#cmdkStatus");
    if (!root || !input || !list || !panel || !status) return;

    const globallyLocked = window.APP_AUTH_MODE === "global" && !window.APP_AUTHENTICATED;
    const navigationActions = [
      { label: tr("收藏", "Favorites"), hint: "", url: window.APP_URLS.favorites },
      { label: tr("首页", "Home"), hint: "", url: window.APP_URLS.home },
    ];
    const utilityActions = [
      { label: tr("切换主题", "Toggle theme"), hint: "", run: () => applyTheme(effectiveTheme() === "dark" ? "light" : "dark") },
    ];
    const protectedActions = [
      { label: tr("新建提示词", "New prompt"), hint: "", url: window.APP_URLS.newPrompt },
      { label: tr("设置", "Settings"), hint: "", url: window.APP_URLS.settings },
    ];
    const actions = (window.APP_CAN_MANAGE ? protectedActions : [])
      .concat(globallyLocked ? [] : navigationActions)
      .concat(utilityActions);
    let items = [];
    let active = -1;
    let searchTimer = null;
    let searchSeq = 0;
    let searchController = null;
    let lastFocus = null;

    function setStatus(message, state = "") {
      status.textContent = message || "";
      status.hidden = !message;
      if (state) status.dataset.state = state;
      else delete status.dataset.state;
    }

    function setBusy(busy) {
      list.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function render(q) {
      const query = q.trim();
      const ql = query.toLowerCase();
      const acts = actions.filter((a) => a.label.toLowerCase().includes(ql));
      items = acts.slice();
      list.innerHTML = "";
      items.forEach((it, i) => list.appendChild(makeItem(it, i)));
      clearTimeout(searchTimer);
      if (searchController) searchController.abort();
      const seq = ++searchSeq;
      if (query && !globallyLocked) {
        setBusy(true);
        setStatus(tr("正在搜索…", "Searching…"), "loading");
        searchTimer = setTimeout(() => fetchResults(query, seq), 180);
      } else {
        setBusy(false);
        setStatus(
          items.length ? "" : (query ? tr("未找到匹配的命令", "No matching commands") : tr("没有可用命令", "No commands available")),
          items.length ? "" : "empty"
        );
      }
      active = items.length ? 0 : -1;
      highlight();
    }

    function makeItem(it, i) {
      const li = document.createElement("li");
      li.className = "cmdk-item";
      li.dataset.index = i;
      li.id = "cmdkOption" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      const span = document.createElement("span");
      span.textContent = it.label; // safe
      li.appendChild(span);
      if (it.hint) {
        const h = document.createElement("span");
        h.className = "muted";
        h.textContent = it.hint;
        li.appendChild(h);
      }
      li.addEventListener("click", () => activate(it));
      li.addEventListener("mousemove", () => { active = i; highlight(false); });
      return li;
    }

    function fetchResults(q, seq) {
      searchController = typeof AbortController === "function" ? new AbortController() : null;
      const body = new URLSearchParams();
      body.set("q", q.slice(0, 256));
      const options = {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-CSRF-Token": csrfToken(),
        },
        body: body.toString(),
      };
      if (searchController) options.signal = searchController.signal;
      fetch(window.APP_URLS.search, options)
        .then((r) => {
          if (!r.ok) throw new Error("search failed");
          return r.json();
        })
        .then((results) => {
          if (seq !== searchSeq) return;
          (Array.isArray(results) ? results : []).forEach((r) => {
            const it = r.locked
              ? { label: r.name, hint: tr("已锁定", "Locked"), url: window.APP_URLS.detail + r.id + "/unlock" }
              : { label: r.name, hint: "", url: window.APP_URLS.detail + r.id };
            items.push(it);
            list.appendChild(makeItem(it, items.length - 1));
          });
          setBusy(false);
          setStatus(items.length ? "" : tr("未找到匹配的命令或提示词", "No matching commands or prompts"), items.length ? "" : "empty");
          if (active < 0 && items.length) active = 0;
          highlight();
        })
        .catch((error) => {
          if (seq !== searchSeq || (error && error.name === "AbortError")) return;
          setBusy(false);
          setStatus(tr("搜索失败，请重试", "Search failed. Please try again."), "error");
        });
    }

    function highlight(scroll = true) {
      if (!items.length) active = -1;
      else active = Math.max(0, Math.min(active, items.length - 1));
      $$(".cmdk-item", list).forEach((el, i) => {
        const selected = i === active;
        el.classList.toggle("active", selected);
        el.setAttribute("aria-selected", selected ? "true" : "false");
      });
      const el = list.children[active];
      if (el) {
        input.setAttribute("aria-activedescendant", el.id);
        if (scroll) el.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function activate(it) {
      if (!it) return;
      if (it.run) { close(); it.run(); return; }
      if (it.url) window.location.href = it.url;
    }

    function open() {
      if (!root.hidden) return;
      lastFocus = document.activeElement;
      root.hidden = false;
      root.setAttribute("aria-hidden", "false");
      input.setAttribute("aria-expanded", "true");
      if (openBtn) openBtn.setAttribute("aria-expanded", "true");
      document.body.classList.add("modal-open");
      input.value = "";
      render("");
      setTimeout(() => input.focus(), 0);
    }
    function close() {
      if (root.hidden) return;
      clearTimeout(searchTimer);
      ++searchSeq;
      if (searchController) searchController.abort();
      root.hidden = true;
      root.setAttribute("aria-hidden", "true");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      if (openBtn) openBtn.setAttribute("aria-expanded", "false");
      document.body.classList.remove("modal-open");
      setBusy(false);
      setStatus("");
      const target = lastFocus && lastFocus.isConnected ? lastFocus : openBtn;
      if (target && target.focus) target.focus({ preventScroll: true });
    }

    function trapFocus(e) {
      if (e.key !== "Tab" || root.hidden) return;
      const focusable = $$(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        panel
      ).filter((el) => !el.hidden && el.getAttribute("aria-hidden") !== "true");
      if (!focusable.length) { e.preventDefault(); panel.focus(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }

    if (openBtn) openBtn.addEventListener("click", open);
    root.addEventListener("keydown", trapFocus);
    input.addEventListener("input", () => render(input.value));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" && items.length) { e.preventDefault(); active = (active + 1) % items.length; highlight(); }
      else if (e.key === "ArrowUp" && items.length) { e.preventDefault(); active = (active - 1 + items.length) % items.length; highlight(); }
      else if (e.key === "Enter") { e.preventDefault(); activate(items[active]); }
      else if (e.key === "Escape") { close(); }
    });
    $$("[data-cmdk-close]", root).forEach((el) => el.addEventListener("click", close));
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); root.hidden ? open() : close(); }
      else if (e.key === "Escape" && !root.hidden) { close(); }  // 焦点不在输入框时也能用 Esc 关闭
    });
  })();

  // Enable transitions only after first paint (prevents drawer slide flash).
  requestAnimationFrame(() => document.documentElement.classList.add("js-ready"));
})();
