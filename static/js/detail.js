(function () {
  "use strict";

  const language = window.APP_LANG || "zh";
  const translate = (zh, en) => (language === "en" ? en : zh);
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
  function syncBumpSelect() {
    if (!saveVersionToggle || !bumpSelect) return;
    bumpSelect.disabled = !saveVersionToggle.checked;
  }
  if (saveVersionToggle && bumpSelect) {
    syncBumpSelect();
    saveVersionToggle.addEventListener("change", syncBumpSelect);
  }

  function updateCounter() {
    if (counter && contentInput) counter.textContent = contentInput.value.length + " " + translate("字符", "chars");
  }
  updateCounter();
  contentInput?.addEventListener("input", updateCounter);

  function serializeForm() {
    const parts = [];
    for (const [key, value] of new FormData(form).entries()) {
      if (key === "_csrf_token") continue;
      if (key === "image_file") {
        parts.push(key + "=" + (value?.name ? value.name + ":" + value.size : ""));
      } else {
        parts.push(key + "=" + (typeof value === "string" ? value : ""));
      }
    }
    return parts.join("&");
  }
  let initialState = serializeForm();
  function checkDirty() {
    const dirty = serializeForm() !== initialState;
    if (dirtyFlag) dirtyFlag.hidden = !dirty;
    return dirty;
  }
  form.addEventListener("input", checkDirty);
  form.addEventListener("change", checkDirty);
  window.addEventListener("beforeunload", (event) => {
    if (!submitting && checkDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.getElementById("copyBtn")?.addEventListener("click", async () => {
    const content = contentInput?.value || "";
    if (!content.trim()) {
      window.toast(translate("没有内容可复制", "No content to copy"), "error");
      return;
    }
    const copied = await window.copyText(content);
    window.toast(copied ? translate("已复制", "Copied") : translate("复制失败", "Copy failed"), copied ? "success" : "error");
  });

  document.getElementById("clearBtn")?.addEventListener("click", async () => {
    const accepted = await window.confirmAction(
      translate("确定要清空内容吗？此操作不可撤销。", "Clear the content? This cannot be undone."),
      { title: translate("清空内容", "Clear content") }
    );
    if (!accepted) return;
    contentInput.value = "";
    updateCounter();
    checkDirty();
    contentInput.focus();
  });

  function escapeHtml(value) {
    return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function renderMarkdown(source) {
    const codeBlocks = [];
    let output = escapeHtml(source).replace(/```([\s\S]*?)```/g, (match, code) => {
      codeBlocks.push("<pre><code>" + code.replace(/^\n/, "") + "</code></pre>");
      return "__PM_CODE_" + (codeBlocks.length - 1) + "__";
    });
    output = output
      .replace(/^###\s+(.*)$/gm, "<h3>$1</h3>")
      .replace(/^##\s+(.*)$/gm, "<h2>$1</h2>")
      .replace(/^#\s+(.*)$/gm, "<h1>$1</h1>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)"'<>]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\n{2,}/g, "</p><p>")
      .replace(/\n/g, "<br>");
    output = "<p>" + output + "</p>";
    return output.replace(/__PM_CODE_(\d+)__/g, (match, index) => codeBlocks[Number(index)] || "");
  }

  const previewButton = document.getElementById("previewToggle");
  const preview = document.getElementById("contentPreview");
  previewButton?.addEventListener("click", () => {
    const showing = !preview.hidden;
    if (showing) {
      preview.hidden = true;
      contentInput.hidden = false;
      previewButton.setAttribute("aria-pressed", "false");
      previewButton.setAttribute("aria-label", translate("预览", "Preview"));
    } else {
      preview.innerHTML = renderMarkdown(contentInput.value || "");
      preview.hidden = false;
      contentInput.hidden = true;
      previewButton.setAttribute("aria-pressed", "true");
      previewButton.setAttribute("aria-label", translate("返回编辑", "Back to editing"));
    }
  });

  const deleteButton = document.getElementById("deleteBtn");
  const deleteForm = document.getElementById("deleteForm");
  deleteButton?.addEventListener("click", async () => {
    const accepted = await window.confirmAction(
      translate("确定要删除该提示词及其所有版本吗？此操作不可恢复。", "Delete this prompt and all versions? This cannot be undone."),
      { title: translate("删除提示词", "Delete prompt"), acceptLabel: translate("删除", "Delete") }
    );
    if (accepted) {
      submitting = true;
      deleteForm.submit();
    }
  });

  const imageInput = document.getElementById("image_file");
  const dropzone = document.getElementById("coverDropzone");
  const selectedPreview = document.getElementById("selectedImagePreview");
  let previewReadId = 0;

  function validateImage(file) {
    if (!file) return false;
    const supported = ["image/jpeg", "image/png", "image/webp"];
    const maxMegabytes = Number(window.APP_MAX_IMAGE_MB) || 5;
    if (!supported.includes(file.type)) {
      window.toast(translate("仅支持 jpg、png、webp 图片", "Only jpg, png, and webp images are supported"), "error");
      return false;
    }
    if (file.size > maxMegabytes * 1024 * 1024) {
      window.toast(translate("图片大小不能超过 " + maxMegabytes + "MB", "Image must be smaller than " + maxMegabytes + "MB"), "error");
      return false;
    }
    return true;
  }

  function showSelectedImage(file) {
    if (!selectedPreview || !file) return;
    const image = selectedPreview.querySelector("img");
    const readId = ++previewReadId;
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (readId !== previewReadId || typeof reader.result !== "string") return;
      image.src = reader.result;
      selectedPreview.hidden = false;
    });
    reader.addEventListener("error", () => {
      if (readId !== previewReadId) return;
      image.removeAttribute("src");
      selectedPreview.hidden = true;
      window.toast(translate("无法预览所选图片", "Unable to preview the selected image"), "error");
    });
    reader.readAsDataURL(file);
  }

  imageInput?.addEventListener("change", () => {
    const file = imageInput.files?.[0];
    if (!file) return;
    if (!validateImage(file)) {
      previewReadId += 1;
      imageInput.value = "";
      selectedPreview.querySelector("img")?.removeAttribute("src");
      selectedPreview.hidden = true;
      return;
    }
    showSelectedImage(file);
  });

  if (dropzone && imageInput) {
    ["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    }));
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (!validateImage(file)) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      imageInput.files = transfer.files;
      imageInput.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  form.addEventListener("submit", (event) => {
    if (submitting) { event.preventDefault(); return; }
    if (!nameInput.value.trim() || !contentInput.value.trim()) {
      event.preventDefault();
      window.toast(
        !nameInput.value.trim() ? translate("请输入提示词名称", "Please enter a prompt name") : translate("请输入提示词内容", "Please enter prompt content"),
        "error"
      );
      return;
    }
    submitting = true;
    if (saveButton) {
      const originalMarkup = saveButton.innerHTML;
      saveButton.disabled = true;
      saveButton.textContent = translate("保存中...", "Saving...");
      setTimeout(() => {
        submitting = false;
        saveButton.disabled = false;
        saveButton.innerHTML = originalMarkup;
      }, 8000);
    }
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.submit();
    }
  });

  (function initializeTagEditor() {
    const hiddenInput = document.getElementById("tags");
    const entry = document.getElementById("tagEntry");
    const chipList = document.getElementById("tagChips");
    const editor = document.getElementById("tagEditor");
    if (!hiddenInput || !entry || !chipList || !editor) return;

    let tags = hiddenInput.value.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean);
    let suggestions = [];
    let dropdown = null;
    let activeIndex = -1;

    function syncTags(notify) {
      hiddenInput.value = tags.join(", ");
      chipList.replaceChildren();
      tags.forEach((tag, index) => {
        const chip = document.createElement("span");
        chip.className = "editable-tag";
        const label = document.createElement("span");
        label.textContent = tag;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";
        remove.setAttribute("aria-label", translate("删除标签 ", "Remove tag ") + tag);
        remove.addEventListener("click", () => {
          tags.splice(index, 1);
          syncTags(true);
          entry.focus();
        });
        chip.append(label, remove);
        chipList.appendChild(chip);
      });
      if (notify) hiddenInput.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function addTag(value) {
      const tag = value.trim().replace(/^[,，]+|[,，]+$/g, "");
      if (!tag || tags.some((existing) => existing.toLowerCase() === tag.toLowerCase())) return;
      tags.push(tag);
      entry.value = "";
      closeSuggestions();
      syncTags(true);
    }

    function closeSuggestions() {
      dropdown?.remove();
      dropdown = null;
      activeIndex = -1;
      entry.setAttribute("aria-expanded", "false");
      entry.removeAttribute("aria-activedescendant");
    }

    function chooseSuggestion(tag) {
      addTag(tag);
      entry.focus();
    }

    function renderSuggestions() {
      const query = entry.value.trim().toLowerCase();
      closeSuggestions();
      if (!query) return;
      const matches = suggestions.filter((tag) => !tags.includes(tag) && tag.toLowerCase().includes(query)).slice(0, 6);
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
        option.addEventListener("mousedown", (event) => { event.preventDefault(); chooseSuggestion(tag); });
        dropdown.appendChild(option);
      });
      editor.appendChild(dropdown);
      entry.setAttribute("aria-expanded", "true");
    }

    function moveSuggestion(direction) {
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
      if (event.key === "ArrowDown") { event.preventDefault(); moveSuggestion(1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); moveSuggestion(-1); }
      else if (event.key === "Enter") {
        event.preventDefault();
        if (activeIndex >= 0 && dropdown) chooseSuggestion(dropdown.children[activeIndex].textContent);
        else addTag(entry.value);
      } else if (event.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        syncTags(true);
      } else if (event.key === "Escape") closeSuggestions();
    });
    entry.addEventListener("blur", () => setTimeout(() => {
      addTag(entry.value);
      closeSuggestions();
    }, 120));

    fetch(window.APP_URLS.tags, { credentials: "same-origin" })
      .then((response) => response.ok ? response.json() : [])
      .then((items) => { suggestions = Array.isArray(items) ? items : []; })
      .catch(() => {});

    syncTags(false);
    initialState = serializeForm();
  })();
})();
