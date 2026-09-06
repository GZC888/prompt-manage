/* detail.js — the reader view and the editor. */
(function () {
  "use strict";

  const tr = window.tr || ((zh) => zh);

  // --- Reader ---------------------------------------------------------------
  (function reader() {
    const rendered = document.getElementById("readerRendered_body");
    const raw = document.getElementById("readerRaw_body");
    if (!rendered || !raw) return;

    const source = raw.textContent || "";
    rendered.innerHTML = window.renderMarkdown(source);
    const counter = document.getElementById("readerCharCount");
    if (counter) counter.textContent = source.length.toLocaleString();

    function setMode(mode) {
      const showRaw = mode === "raw";
      raw.hidden = !showRaw;
      rendered.hidden = showRaw;
      document.querySelectorAll("[data-reader-mode]").forEach((button) => {
        button.setAttribute("aria-pressed", button.dataset.readerMode === mode ? "true" : "false");
      });
      try { localStorage.setItem("readerMode", mode); } catch (error) { /* not persisted */ }
    }

    document.querySelectorAll("[data-reader-mode]").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.readerMode));
    });
    let stored = null;
    try { stored = localStorage.getItem("readerMode"); } catch (error) { /* ignore */ }
    setMode(stored === "raw" ? "raw" : "rendered");
  })();

  // --- Editor ---------------------------------------------------------------
  const form = document.getElementById("promptForm");
  if (!form) return;

  const nameInput = document.getElementById("name");
  const contentInput = document.getElementById("content");
  const counter = document.getElementById("charCounter");
  const saveButton = document.getElementById("saveBtn");
  const dirtyFlag = document.getElementById("dirtyFlag");
  let submitting = false;

  const saveVersionToggle = document.getElementById("saveVersionToggle");
  const bumpSelect = document.getElementById("bump_kind");
  if (saveVersionToggle && bumpSelect) {
    const sync = () => { bumpSelect.disabled = !saveVersionToggle.checked; };
    saveVersionToggle.addEventListener("change", sync);
    sync();
  }

  function updateCounter() {
    if (counter && contentInput) {
      counter.textContent = contentInput.value.length.toLocaleString() + " " + tr("字符", "chars");
    }
  }
  updateCounter();
  contentInput?.addEventListener("input", updateCounter);

  function snapshot() {
    return Array.from(new FormData(form).entries())
      .filter(([key]) => key !== "_csrf_token")
      .map(([key, value]) => key + "=" + (typeof value === "string" ? value : ""))
      .join("&");
  }
  let initialState = snapshot();
  function checkDirty() {
    const dirty = snapshot() !== initialState;
    if (dirtyFlag) dirtyFlag.hidden = !dirty;
    return dirty;
  }
  form.addEventListener("input", checkDirty);
  form.addEventListener("change", checkDirty);
  window.addEventListener("beforeunload", (event) => {
    if (!submitting && checkDirty()) { event.preventDefault(); event.returnValue = ""; }
  });

  document.getElementById("copyBtn")?.addEventListener("click", async () => {
    const content = contentInput?.value || "";
    if (!content.trim()) { window.toast(tr("没有内容可复制", "No content to copy"), "error"); return; }
    const copied = await window.copyText(content);
    window.toast(copied ? tr("已复制", "Copied") : tr("复制失败", "Copy failed"), copied ? "success" : "error");
  });

  const previewButton = document.getElementById("previewToggle");
  const preview = document.getElementById("contentPreview");
  previewButton?.addEventListener("click", () => {
    const showing = !preview.hidden;
    if (showing) {
      preview.hidden = true;
      contentInput.hidden = false;
      contentInput.focus();
    } else {
      preview.innerHTML = window.renderMarkdown(contentInput.value || "");
      preview.hidden = false;
      contentInput.hidden = true;
    }
    previewButton.setAttribute("aria-pressed", showing ? "false" : "true");
    previewButton.setAttribute("aria-label", showing ? tr("预览", "Preview") : tr("返回编辑", "Back to editing"));
  });

  const deleteForm = document.getElementById("deleteForm");
  document.getElementById("deleteBtn")?.addEventListener("click", async () => {
    const accepted = await window.confirmAction(
      tr("确定要删除该提示词及其所有版本吗？此操作不可恢复。",
         "Delete this prompt and all its versions? This cannot be undone."),
      { title: tr("删除提示词", "Delete prompt"), acceptLabel: tr("删除", "Delete") }
    );
    if (!accepted) return;
    submitting = true;
    deleteForm.submit();
  });

  form.addEventListener("submit", (event) => {
    if (submitting) { event.preventDefault(); return; }
    if (!nameInput.value.trim() || !contentInput.value.trim()) {
      event.preventDefault();
      const missingName = !nameInput.value.trim();
      window.toast(
        missingName ? tr("请输入提示词名称", "Please enter a prompt name")
                    : tr("请输入提示词内容", "Please enter prompt content"),
        "error"
      );
      (missingName ? nameInput : contentInput).focus();
      return;
    }
    submitting = true;
    if (saveButton) {
      saveButton.disabled = true;
      saveButton.setAttribute("aria-busy", "true");
      // Re-enable if the navigation never happens (e.g. the browser blocks it).
      setTimeout(() => {
        submitting = false;
        saveButton.disabled = false;
        saveButton.removeAttribute("aria-busy");
      }, 8000);
    }
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.submit();
    }
  });

  // --- Colour ---------------------------------------------------------------
  (function colorField() {
    const text = document.getElementById("color");
    if (!text) return;
    const presets = Array.from(document.querySelectorAll(".color-preset"));
    const expand = (value) => {
      const raw = (value || "").trim();
      if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(raw)) return "";
      return (raw.length === 4
        ? "#" + raw.slice(1).split("").map((c) => c + c).join("")
        : raw).toLowerCase();
    };
    const paint = () => {
      const color = expand(text.value);
      text.classList.toggle("color-invalid", Boolean(text.value.trim()) && !color);
      presets.forEach((button) => button.classList.toggle("active", expand(button.dataset.color) === color));
    };
    text.addEventListener("input", paint);
    presets.forEach((button) => button.addEventListener("click", () => {
      const next = expand(button.dataset.color);
      text.value = text.value.trim().toLowerCase() === next ? "" : next;
      paint();
      checkDirty();
    }));
    document.getElementById("colorClearBtn")?.addEventListener("click", () => {
      text.value = "";
      paint();
      checkDirty();
    });
    paint();
  })();

  // --- Tag editor -----------------------------------------------------------
  (function tagEditor() {
    const hidden = document.getElementById("tags");
    const entry = document.getElementById("tagEntry");
    const chips = document.getElementById("tagChips");
    const editor = document.getElementById("tagEditor");
    if (!hidden || !entry || !chips || !editor) return;

    let tags = hidden.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean);
    let suggestions = [];
    let dropdown = null;
    let activeIndex = -1;

    function sync(notify) {
      hidden.value = tags.join(", ");
      chips.replaceChildren(...tags.map((tag, index) => {
        const chip = document.createElement("span");
        chip.className = "editable-tag";
        const label = document.createElement("span");
        label.textContent = tag;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", tr("删除标签 ", "Remove tag ") + tag);
        remove.addEventListener("click", () => {
          tags.splice(index, 1);
          sync(true);
          entry.focus();
        });
        chip.append(label, remove);
        return chip;
      }));
      if (notify) { hidden.dispatchEvent(new Event("input", { bubbles: true })); checkDirty(); }
    }

    function closeSuggestions() {
      dropdown?.remove();
      dropdown = null;
      activeIndex = -1;
      entry.setAttribute("aria-expanded", "false");
      entry.removeAttribute("aria-activedescendant");
    }

    function addTag(value) {
      const tag = value.trim().replace(/^[,，]+|[,，]+$/g, "");
      if (!tag || tags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) {
        entry.value = "";
        closeSuggestions();
        return;
      }
      tags.push(tag);
      entry.value = "";
      closeSuggestions();
      sync(true);
    }

    function renderSuggestions() {
      const needle = entry.value.trim().toLowerCase();
      closeSuggestions();
      if (!needle) return;
      const matches = suggestions
        .filter((tag) => !tags.includes(tag) && tag.toLowerCase().includes(needle))
        .slice(0, 6);
      if (!matches.length) return;
      dropdown = document.createElement("div");
      dropdown.id = "tagSuggestions";
      dropdown.className = "tag-suggestions";
      dropdown.setAttribute("role", "listbox");
      matches.forEach((tag, index) => {
        const option = document.createElement("div");
        option.id = "tagSuggestion" + index;
        option.className = "tag-suggestion";
        option.setAttribute("role", "option");
        option.textContent = tag;
        option.addEventListener("mousedown", (event) => { event.preventDefault(); addTag(tag); entry.focus(); });
        dropdown.appendChild(option);
      });
      editor.appendChild(dropdown);
      entry.setAttribute("aria-expanded", "true");
    }

    function move(direction) {
      if (!dropdown?.children.length) return;
      activeIndex = (activeIndex + direction + dropdown.children.length) % dropdown.children.length;
      Array.from(dropdown.children).forEach((option, index) => option.classList.toggle("active", index === activeIndex));
      entry.setAttribute("aria-activedescendant", dropdown.children[activeIndex].id);
    }

    entry.addEventListener("input", () => {
      if (/[,，]$/.test(entry.value)) addTag(entry.value);
      else renderSuggestions();
    });
    entry.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); move(1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); move(-1); }
      else if (event.key === "Enter") {
        event.preventDefault();
        if (activeIndex >= 0 && dropdown) addTag(dropdown.children[activeIndex].textContent);
        else addTag(entry.value);
      } else if (event.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        sync(true);
      } else if (event.key === "Escape") {
        closeSuggestions();
      }
    });
    entry.addEventListener("blur", () => setTimeout(() => { addTag(entry.value); closeSuggestions(); }, 120));

    fetch(window.APP.urls.tags, { credentials: "same-origin" })
      .then((response) => (response.ok ? response.json() : []))
      .then((items) => { suggestions = Array.isArray(items) ? items : []; })
      .catch(() => { /* suggestions are optional */ });

    sync(false);
    initialState = snapshot();
  })();
})();
