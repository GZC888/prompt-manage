/* main.js — shared behaviour for every page.
   Deliberately small: toasts, csrfFetch, clipboard, theme, a confirm dialog,
   a Markdown renderer and the command palette. No scroll observers, no
   hover animation, no blanket submit-loading. */
(function () {
  "use strict";

  const APP = window.APP || (window.APP = { lang: "zh", urls: {} });
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const tr = (zh, en) => (APP.lang === "en" ? en : zh);
  window.tr = tr;

  const csrfToken = () => $('meta[name="csrf-token"]')?.getAttribute("content") || "";

  // --- Toasts --------------------------------------------------------------
  function dismissToast(el) {
    el.classList.add("hide");
    setTimeout(() => el.remove(), 200);
  }

  function toast(message, type = "info", timeout = 4000) {
    let root = $("#toastRoot");
    if (!root) {
      root = document.createElement("div");
      root.id = "toastRoot";
      root.className = "toast-root";
      document.body.appendChild(root);
    }
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    const text = document.createElement("span");
    text.className = "toast-msg";
    text.textContent = message; // textContent: never inject HTML
    const close = document.createElement("button");
    close.className = "toast-close";
    close.type = "button";
    close.setAttribute("aria-label", tr("关闭", "Close"));
    close.textContent = "×";
    close.addEventListener("click", () => dismissToast(el));
    el.append(text, close);
    root.appendChild(el);
    if (timeout) setTimeout(() => dismissToast(el), timeout);
    return el;
  }
  window.toast = toast;

  $$("#toastRoot .toast").forEach((el, index) => {
    el.querySelector(".toast-close")?.addEventListener("click", () => dismissToast(el));
    setTimeout(() => dismissToast(el), 4500 + index * 400);
  });

  // --- Fetch with CSRF -----------------------------------------------------
  function csrfFetch(url, options = {}) {
    return fetch(url, Object.assign({ method: "POST", credentials: "same-origin" }, options, {
      headers: Object.assign({ "X-CSRF-Token": csrfToken() }, options.headers || {}),
    }));
  }
  window.csrfFetch = csrfFetch;

  // --- Clipboard -----------------------------------------------------------
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (error) { /* fall through */ }
    }
    // execCommand is deprecated but remains the only fallback over plain HTTP.
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.cssText = "position:fixed;top:-1000px";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch (error) { return false; }
  }
  window.copyText = copyText;

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".js-copy");
    if (!button) return;
    let text = button.getAttribute("data-content") || "";
    const url = button.getAttribute("data-copy-url");
    if (!text && url) {
      button.disabled = true;
      try {
        const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("copy fetch failed");
        text = (await response.json()).content || "";
      } catch (error) {
        toast(tr("读取内容失败", "Failed to load content"), "error");
        return;
      } finally {
        button.disabled = false;
      }
    }
    if (!text) { toast(tr("没有内容可复制", "No content to copy"), "error"); return; }
    const ok = await copyText(text);
    toast(ok ? tr("已复制", "Copied") : tr("复制失败", "Copy failed"), ok ? "success" : "error");
  });

  // --- Theme: light -> dark -> follow system --------------------------------
  const readTheme = () => { try { return localStorage.getItem("theme"); } catch (e) { return null; } };
  const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const effectiveTheme = () => {
    const stored = readTheme();
    return stored === "light" || stored === "dark" ? stored : (systemDark() ? "dark" : "light");
  };

  function applyTheme(mode) {
    try {
      if (mode === "light" || mode === "dark") localStorage.setItem("theme", mode);
      else localStorage.removeItem("theme");
    } catch (error) { /* private mode: the choice just will not persist */ }
    document.documentElement.setAttribute("data-theme", mode === "light" || mode === "dark" ? mode : "auto");
    syncThemeButtons();
  }
  window.applyTheme = applyTheme;

  const themeButtons = $$("#themeToggle, #themeToggleAuth");
  function syncThemeButtons() {
    const stored = readTheme() || "auto";
    themeButtons.forEach((button) => {
      button.setAttribute("aria-pressed", effectiveTheme() === "dark" ? "true" : "false");
      button.setAttribute("title", themeLabel(stored));
    });
  }
  const themeLabel = (mode) => mode === "light" ? tr("浅色主题", "Light theme")
    : mode === "dark" ? tr("深色主题", "Dark theme")
    : tr("跟随系统", "Follow system");

  themeButtons.forEach((button) => button.addEventListener("click", () => {
    const stored = readTheme();
    const next = stored === "light" ? "dark" : stored === "dark" ? "auto" : "light";
    applyTheme(next);
    toast(themeLabel(next), "info", 1600);
  }));
  syncThemeButtons();

  // --- Confirmation dialog -------------------------------------------------
  const confirmRoot = $("#confirmDialog");
  const confirmAccept = $("#confirmAccept");
  let confirmResolve = null;
  let confirmLastFocus = null;

  function closeConfirm(result) {
    if (!confirmRoot || confirmRoot.hidden) return;
    confirmRoot.hidden = true;
    document.body.classList.remove("modal-open");
    const resolve = confirmResolve;
    confirmResolve = null;
    if (confirmLastFocus?.isConnected) confirmLastFocus.focus({ preventScroll: true });
    resolve?.(Boolean(result));
  }

  function confirmAction(message, options = {}) {
    if (!confirmRoot || !confirmAccept) return Promise.resolve(window.confirm(message));
    if (confirmResolve) closeConfirm(false);
    confirmLastFocus = document.activeElement;
    $("#confirmTitle").textContent = options.title || tr("确认操作", "Confirm action");
    $("#confirmMessage").textContent = message || "";
    confirmAccept.textContent = options.acceptLabel || tr("确认", "Confirm");
    confirmRoot.hidden = false;
    document.body.classList.add("modal-open");
    setTimeout(() => confirmAccept.focus(), 0);
    return new Promise((resolve) => { confirmResolve = resolve; });
  }
  window.confirmAction = confirmAction;

  if (confirmRoot && confirmAccept) {
    confirmAccept.addEventListener("click", () => closeConfirm(true));
    $$("[data-confirm-cancel]", confirmRoot).forEach((el) => el.addEventListener("click", () => closeConfirm(false)));
    confirmRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); closeConfirm(false); return; }
      trapFocus(event, confirmRoot);
    });
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (!form || form.dataset.confirmed === "1") return;
    event.preventDefault();
    const accepted = await confirmAction(form.getAttribute("data-confirm"), {
      title: form.getAttribute("data-confirm-title") || undefined,
    });
    if (!accepted) return;
    form.dataset.confirmed = "1";
    form.submit();
  }, true);

  function trapFocus(event, container) {
    if (event.key !== "Tab" || container.hidden) return;
    const focusable = $$(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      container
    ).filter((el) => !el.hidden && el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  window.trapFocus = trapFocus;

  // --- Markdown ------------------------------------------------------------
  // A deliberately small subset (headings, lists, emphasis, code, links,
  // quotes). Everything is escaped first, so the output can never carry markup
  // that came from the prompt text itself.
  const escapeHtml = (value) => value
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inlineMarkdown(text) {
    return text
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)"'<>]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function renderMarkdown(source) {
    const lines = escapeHtml(String(source || "")).split("\n");
    const out = [];
    let listType = null;
    let paragraph = [];
    let fence = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      out.push("<p>" + inlineMarkdown(paragraph.join("<br>")) + "</p>");
      paragraph = [];
    };
    const closeList = () => {
      if (listType) { out.push("</" + listType + ">"); listType = null; }
    };
    const openList = (type) => {
      if (listType !== type) { closeList(); out.push("<" + type + ">"); listType = type; }
    };

    for (const line of lines) {
      const fenceMatch = /^\s*```(.*)$/.exec(line);
      if (fenceMatch) {
        if (fence === null) {
          flushParagraph();
          closeList();
          fence = [];
        } else {
          out.push("<pre><code>" + fence.join("\n") + "</code></pre>");
          fence = null;
        }
        continue;
      }
      if (fence !== null) { fence.push(line); continue; }

      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph(); closeList();
        const level = heading[1].length;
        out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        continue;
      }
      const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (bullet) {
        flushParagraph(); openList("ul");
        out.push("<li>" + inlineMarkdown(bullet[1]) + "</li>");
        continue;
      }
      const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (ordered) {
        flushParagraph(); openList("ol");
        out.push("<li>" + inlineMarkdown(ordered[1]) + "</li>");
        continue;
      }
      const quote = /^\s*&gt;\s?(.*)$/.exec(line);
      if (quote) {
        flushParagraph(); closeList();
        out.push("<blockquote>" + inlineMarkdown(quote[1]) + "</blockquote>");
        continue;
      }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        flushParagraph(); closeList();
        out.push("<hr>");
        continue;
      }
      if (!line.trim()) { flushParagraph(); closeList(); continue; }
      if (listType) closeList();
      paragraph.push(line);
    }
    if (fence !== null) out.push("<pre><code>" + fence.join("\n") + "</code></pre>");
    flushParagraph();
    closeList();
    return out.join("\n");
  }
  window.renderMarkdown = renderMarkdown;

  // --- Import file pre-check (settings page) --------------------------------
  const importInput = $('input[name="import_file"]');
  if (importInput) {
    importInput.addEventListener("change", () => {
      const file = importInput.files?.[0];
      if (!file) return;
      const maxMb = Number(APP.maxImportMb) || 10;
      if (!/\.json$/i.test(file.name || "")) {
        importInput.value = "";
        toast(tr("仅支持 .json 备份文件", "Only .json backups are supported"), "error");
      } else if (file.size > maxMb * 1024 * 1024) {
        importInput.value = "";
        toast(tr(`文件过大：最大 ${maxMb}MB`, `File too large: maximum ${maxMb}MB`), "error");
      }
    });
  }

  // --- Command palette -----------------------------------------------------
  (function commandPalette() {
    const root = $("#cmdk");
    const input = $("#cmdkInput");
    const list = $("#cmdkList");
    const status = $("#cmdkStatus");
    const openButton = $("#cmdkBtn");
    if (!root || !input || !list || !status) return;

    const actions = [
      { label: tr("新建提示词", "New prompt"), url: APP.urls.newPrompt },
      { label: tr("提示词库", "Library"), url: APP.urls.home },
      { label: tr("设置", "Settings"), url: APP.urls.settings },
      { label: tr("切换主题", "Toggle theme"), run: () => applyTheme(effectiveTheme() === "dark" ? "light" : "dark") },
    ];
    let items = [];
    let active = -1;
    let timer = null;
    let seq = 0;
    let controller = null;
    let lastFocus = null;

    function setStatus(message) {
      status.textContent = message || "";
      status.hidden = !message;
    }

    function makeItem(item, index) {
      const li = document.createElement("li");
      li.className = "cmdk-item";
      li.id = "cmdkOption" + index;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      const label = document.createElement("span");
      label.textContent = item.label; // textContent: prompt names are user data
      li.appendChild(label);
      if (item.hint) {
        const hint = document.createElement("span");
        hint.className = "muted";
        hint.textContent = item.hint;
        li.appendChild(hint);
      }
      li.addEventListener("click", () => activate(item));
      li.addEventListener("mousemove", () => { active = index; highlight(false); });
      return li;
    }

    function highlight(scroll = true) {
      active = items.length ? Math.max(0, Math.min(active, items.length - 1)) : -1;
      $$(".cmdk-item", list).forEach((el, index) => {
        const selected = index === active;
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

    function render(query) {
      const needle = query.trim().toLowerCase();
      items = actions.filter((action) => action.label.toLowerCase().includes(needle));
      list.replaceChildren(...items.map(makeItem));
      clearTimeout(timer);
      controller?.abort();
      const current = ++seq;
      if (needle) {
        setStatus(tr("正在搜索…", "Searching…"));
        timer = setTimeout(() => search(query, current), 160);
      } else {
        setStatus("");
      }
      active = items.length ? 0 : -1;
      highlight();
    }

    function search(query, current) {
      controller = typeof AbortController === "function" ? new AbortController() : null;
      const body = new URLSearchParams({ q: query.slice(0, 256) });
      fetch(APP.urls.search, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-CSRF-Token": csrfToken(),
        },
        body: body.toString(),
        signal: controller?.signal,
      })
        .then((response) => { if (!response.ok) throw new Error("search failed"); return response.json(); })
        .then((results) => {
          if (current !== seq) return;
          (Array.isArray(results) ? results : []).forEach((result) => {
            const item = { label: result.name, hint: result.source || "", url: APP.urls.detail + result.id };
            items.push(item);
            list.appendChild(makeItem(item, items.length - 1));
          });
          setStatus(items.length ? "" : tr("没有匹配的提示词", "No matching prompts"));
          if (active < 0 && items.length) active = 0;
          highlight();
        })
        .catch((error) => {
          if (current !== seq || error?.name === "AbortError") return;
          setStatus(tr("搜索失败，请重试", "Search failed. Please try again."));
        });
    }

    function activate(item) {
      if (!item) return;
      if (item.run) { close(); item.run(); return; }
      if (item.url) window.location.href = item.url;
    }

    function open() {
      if (!root.hidden) return;
      lastFocus = document.activeElement;
      root.hidden = false;
      input.setAttribute("aria-expanded", "true");
      openButton?.setAttribute("aria-expanded", "true");
      document.body.classList.add("modal-open");
      input.value = "";
      render("");
      setTimeout(() => input.focus(), 0);
    }

    function close() {
      if (root.hidden) return;
      clearTimeout(timer);
      seq += 1;
      controller?.abort();
      root.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      openButton?.setAttribute("aria-expanded", "false");
      document.body.classList.remove("modal-open");
      setStatus("");
      (lastFocus?.isConnected ? lastFocus : openButton)?.focus?.({ preventScroll: true });
    }

    openButton?.addEventListener("click", open);
    $$("[data-cmdk-close]", root).forEach((el) => el.addEventListener("click", close));
    root.addEventListener("keydown", (event) => trapFocus(event, root));
    input.addEventListener("input", () => render(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" && items.length) { event.preventDefault(); active = (active + 1) % items.length; highlight(); }
      else if (event.key === "ArrowUp" && items.length) { event.preventDefault(); active = (active - 1 + items.length) % items.length; highlight(); }
      else if (event.key === "Enter") { event.preventDefault(); activate(items[active]); }
      else if (event.key === "Escape") close();
    });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        root.hidden ? open() : close();
      } else if (event.key === "Escape" && !root.hidden) {
        close();
      }
    });
  })();

  // Enable transitions only after first paint (prevents a drawer slide flash).
  requestAnimationFrame(() => document.documentElement.classList.add("js-ready"));
})();
