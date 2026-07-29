/* settings.js — import file status display (safe DOM, no innerHTML). */
(function () {
  "use strict";
  const LANG = window.APP_LANG || "zh";
  const tr = (zh, en) => (LANG === "en" ? en : zh);
  const input = document.getElementById("importFileInput");
  const status = document.getElementById("importFileStatus");
  const submit = document.getElementById("importSubmit");
  const restoreAuth = document.getElementById("restoreAuthCheckbox");
  const restorePasswordField = document.getElementById("restoreAuthPasswordField");
  const restorePassword = document.getElementById("restoreCurrentPassword");
  const restoreBackupPasswordField = document.getElementById("restoreBackupPasswordField");
  const restoreBackupPassword = document.getElementById("restoreBackupPassword");
  if (!input || !status) return;

  function syncRestoreAuth() {
    const enabled = Boolean(restoreAuth && restoreAuth.checked);
    if (restorePasswordField && restorePassword) {
      restorePasswordField.hidden = !enabled;
      restorePassword.disabled = !enabled;
      restorePassword.required = enabled;
      if (!enabled) restorePassword.value = "";
    }
    if (restoreBackupPasswordField && restoreBackupPassword) {
      restoreBackupPasswordField.hidden = !enabled;
      restoreBackupPassword.disabled = !enabled;
      if (!enabled) restoreBackupPassword.value = "";
    }
  }
  if (restoreAuth) {
    restoreAuth.addEventListener("change", syncRestoreAuth);
    syncRestoreAuth();
  }

  function fmtSize(bytes) {
    if (!isFinite(bytes) || bytes < 0) return "--";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }
  function setText(node, cls) {
    status.className = "file-status" + (cls ? " " + cls : "");
    status.replaceChildren(node);
  }

  function reset() {
    const s = document.createElement("span");
    s.className = "muted";
    s.textContent = tr("未选择文件", "No file selected");
    setText(s, "");
    if (submit) submit.disabled = true;
  }

  input.addEventListener("change", () => {
    const f = input.files && input.files[0];
    if (!f) { reset(); return; }
    const ok = /\.(json|csv)$/i.test(f.name || "");
    if (!ok) {
      input.value = "";  // 清空，避免不支持的文件被一并提交
      const s = document.createElement("span");
      s.textContent = tr("不支持的文件类型：仅支持 JSON 或 CSV", "Unsupported file type: only JSON or CSV");
      setText(s, "invalid");
      if (submit) submit.disabled = true;
      return;
    }
    const maxMb = Number(window.APP_MAX_IMPORT_MB) || 10;
    if (f.size > maxMb * 1024 * 1024) {
      input.value = "";
      const s = document.createElement("span");
      s.textContent = tr(
        "文件过大：最大允许 " + maxMb + " MB",
        "File is too large. Maximum: " + maxMb + " MB"
      );
      setText(s, "invalid");
      if (submit) submit.disabled = true;
      return;
    }
    const frag = document.createDocumentFragment();
    const label = document.createElement("span");
    label.textContent = tr("已选择文件：", "Selected file: ");
    const strong = document.createElement("strong");
    strong.textContent = f.name;
    const size = document.createElement("span");
    size.className = "file-size";
    size.textContent = tr("文件大小：", "File size: ") + fmtSize(f.size);
    frag.appendChild(label);
    frag.appendChild(strong);
    frag.appendChild(size);
    setText(frag, "valid");
    if (submit) submit.disabled = false;
  });

  const form = input.closest("form");
  if (form) {
    let confirmed = false;
    form.addEventListener("submit", async (e) => {
      if (confirmed) return;
      if (input.files && input.files.length > 0) {
        e.preventDefault();
        const restoreAuthentication = form.querySelector('input[name="restore_auth"]')?.checked;
        const msg = tr(
          restoreAuthentication
            ? "导入将覆盖所有数据并替换认证设置（导入前会自动备份）。确定要继续吗？"
            : "导入将覆盖所有现有数据（导入前会自动备份）。确定要继续吗？",
          restoreAuthentication
            ? "Import will overwrite all data and replace authentication settings (a backup is taken first). Continue?"
            : "Import will overwrite ALL existing data (a backup is taken first). Continue?"
        );
        const accepted = await window.confirmAction(msg, {
          title: tr("导入并覆盖", "Import and overwrite"),
          acceptLabel: tr("继续导入", "Continue import"),
        });
        if (!accepted) return;
        if (submit) {
          submit.disabled = true;
          submit.setAttribute("aria-busy", "true");
        }
        confirmed = true;
        form.submit();
      }
    });
  }

  reset();
})();
