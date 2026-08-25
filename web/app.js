(() => {
  "use strict";

  const state = {
    config: null,
    selectedFile: null,
    jobId: null,
    activeView: null,
    pollTimer: null,
    pollVersion: 0,
  };
  const $ = (id) => document.getElementById(id);
  const apiKeyInput = $("api-key");

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  function apiHeaders() {
    const key = apiKeyInput.value.trim();
    return key ? { "X-API-Key": key } : {};
  }

  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    Object.entries(apiHeaders()).forEach(([key, value]) => headers.set(key, value));
    const response = await fetch(url, { ...options, headers });
    if (!response.ok) {
      let detail = `请求失败（${response.status}）`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* plain response */ }
      throw new Error(detail);
    }
    return response;
  }

  // Fetch does not expose upload progress in browsers. XHR keeps the same
  // multipart request while letting the UI report bytes sent for large files.
  function uploadJob(form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      Object.entries(apiHeaders()).forEach(([key, value]) => xhr.setRequestHeader(key, value));
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded, event.total);
      });
      xhr.addEventListener("load", () => {
        let payload = null;
        try { payload = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* plain response */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
          return;
        }
        reject(new Error(payload?.detail || `请求失败（${xhr.status}）`));
      });
      xhr.addEventListener("error", () => reject(new Error("上传连接中断，请检查网络后重试。")));
      xhr.addEventListener("abort", () => reject(new Error("上传已取消。")));
      xhr.send(form);
    });
  }

  function setServiceState(kind, text) {
    const element = $("service-state");
    element.classList.remove("is-ready", "is-error");
    if (kind) element.classList.add(`is-${kind}`);
    $("service-state-text").textContent = text;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${Math.max(0, Math.round(bytes))} B`;
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderModes(modes) {
    $("mode-list").innerHTML = modes.map((mode, index) => `
      <div class="mode-option">
        <input id="mode-${escapeHtml(mode.id)}" name="mode" type="radio" value="${escapeHtml(mode.id)}" ${index === 0 ? "checked" : ""}>
        <label for="mode-${escapeHtml(mode.id)}">
          <span class="mode-name">${escapeHtml(mode.label)}</span>
          <span class="mode-description">${escapeHtml(mode.description)}</span>
        </label>
      </div>`).join("");
    document.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener("change", updateModeState));
    updateModeState();
  }

  function selectedMode() {
    return document.querySelector('input[name="mode"]:checked')?.value || "sttn-auto";
  }

  function updateModeState() {
    const mode = selectedMode();
    const areaLabel = document.querySelector('label[for="coords"]');
    const note = document.querySelector(".field-note");
    if (mode === "logo-lama") {
      areaLabel.textContent = "固定区域（必填）";
      note.textContent = "请填写一个或多个固定水印区域，格式为 JSON 数组。";
    } else {
      areaLabel.textContent = "固定区域（可选）";
      note.textContent = "留空时由算法使用全屏区域；检测模式会结合 OCR 定位字幕。";
    }
  }

  function showFile(file) {
    state.selectedFile = file;
    const summary = $("file-summary");
    summary.hidden = !file;
    summary.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "";
    $("dropzone").classList.toggle("has-file", Boolean(file));
    $("submit-button").disabled = !file;
  }

  function showFormError(message = "") {
    const error = $("form-error");
    error.hidden = !message;
    error.textContent = message;
  }

  function setMonitor(job) {
    $("empty-monitor").hidden = true;
    $("task-monitor").hidden = false;
    $("task-mode").textContent = job.mode_label || job.mode;
    $("task-name").textContent = job.filename || "处理任务";
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    $("progress-number").textContent = `${progress}%`;
    $("progress-fill").style.width = `${progress}%`;
    $("task-message").textContent = job.error || job.message || "处理中";
    $("log-box").textContent = (job.logs || []).join("\n");
    $("log-box").scrollTop = $("log-box").scrollHeight;
    const chip = $("status-chip");
    chip.className = "status-chip";
    const labels = { loading: "读取中", uploading: "上传中", queued: "排队中", processing: "处理中", paused: "已暂停", succeeded: "已完成", failed: "失败", "load-error": "读取失败" };
    chip.textContent = labels[job.status] || job.status;
    chip.classList.add({ loading: "status-processing", uploading: "status-processing", queued: "status-processing", processing: "status-processing", paused: "status-paused", succeeded: "status-success", failed: "status-error", "load-error": "status-error" }[job.status] || "status-idle");
    const pause = $("pause-button");
    pause.hidden = !["queued", "processing", "paused"].includes(job.status);
    pause.dataset.action = job.status === "paused" ? "resume" : "pause";
    pause.innerHTML = job.status === "paused" ? '<span aria-hidden="true">▶</span> 继续任务' : '<span aria-hidden="true">Ⅱ</span> 暂停任务';
    pause.disabled = false;
    const download = $("download-button");
    download.hidden = job.status !== "succeeded";
    if (job.download_url) {
      download.href = job.download_url;
      download.setAttribute("download", "");
    }
    $("retry-button").hidden = job.status !== "failed";
  }

  function setUploadMonitor(file, loaded, total) {
    if (state.activeView !== "upload") return;
    const percent = total > 0 ? Math.round((loaded / total) * 100) : 0;
    const amount = `${formatBytes(loaded)} / ${formatBytes(total)}`;
    setMonitor({
      status: "uploading",
      mode: "upload",
      mode_label: "文件上传",
      filename: file.name,
      progress: percent,
      message: `正在上传 ${amount}`,
      logs: [`已上传 ${amount}`],
    });
  }

  function clearPolling() {
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollVersion += 1;
  }

  function setActiveRecentJob(jobId) {
    document.querySelectorAll(".recent-item").forEach((item) => {
      const isActive = item.dataset.jobId === jobId;
      item.classList.toggle("is-active", isActive);
      item.setAttribute("aria-pressed", String(isActive));
    });
  }

  async function pollJob(jobId, pollVersion) {
    if (state.jobId !== jobId || state.pollVersion !== pollVersion) return;
    try {
      const response = await apiFetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (state.jobId !== jobId || state.pollVersion !== pollVersion) return;
      setMonitor(job);
      setActiveRecentJob(jobId);
      setServiceState("ready", "服务已连接");
      if (["queued", "processing", "paused"].includes(job.status)) {
        state.pollTimer = window.setTimeout(() => pollJob(jobId, pollVersion), 900);
      } else {
        state.pollTimer = null;
        $("submit-button").disabled = !state.selectedFile;
        await loadRecentJobs();
      }
    } catch (error) {
      if (state.jobId !== jobId || state.pollVersion !== pollVersion) return;
      state.pollTimer = null;
      setMonitor({
        status: "load-error",
        mode_label: "任务详情",
        filename: document.querySelector(`.recent-item[data-job-id="${jobId}"] .recent-name`)?.textContent || "无法读取任务",
        progress: 0,
        error: error.message,
        logs: ["任务状态读取失败，请稍后重试。"],
      });
      setServiceState("error", "服务请求失败");
    }
  }

  async function selectJob(jobId) {
    if (!jobId) return;
    clearPolling();
    state.jobId = jobId;
    state.activeView = `job:${jobId}`;
    setActiveRecentJob(jobId);
    const filename = document.querySelector(`.recent-item[data-job-id="${jobId}"] .recent-name`)?.textContent || "正在读取任务";
    setMonitor({
      status: "loading",
      mode_label: "任务详情",
      filename,
      progress: 0,
      message: "正在读取任务状态…",
      logs: [],
    });
    await pollJob(jobId, state.pollVersion);
  }

  async function submitJob(event) {
    event.preventDefault();
    showFormError("");
    if (!state.selectedFile) { showFormError("请先选择一个视频文件。"); return; }
    const mode = selectedMode();
    const coords = $("coords").value.trim();
    if (mode === "logo-lama" && !coords) { showFormError("固定水印模式需要填写区域。"); return; }
    const maxUploadBytes = Number(state.config?.max_upload_bytes || 0);
    if (maxUploadBytes > 0 && state.selectedFile.size > maxUploadBytes) {
      showFormError(`文件过大（${formatBytes(state.selectedFile.size)}），服务限制为 ${formatBytes(maxUploadBytes)}。`);
      return;
    }
    const pad = Number($("pad").value);
    if (!Number.isInteger(pad) || pad < 0 || pad > 512) { showFormError("边缘扩展必须是 0 到 512 的整数。"); return; }
    const form = new FormData();
    form.append("file", state.selectedFile);
    form.append("mode", mode);
    form.append("subtitle_area_coords", coords);
    form.append("pad", String(pad));
    form.append("detect_mode", $("detect-mode").value);
    const submit = $("submit-button");
    clearPolling();
    state.jobId = null;
    state.activeView = "upload";
    setActiveRecentJob(null);
    submit.disabled = true;
    submit.querySelector("span").textContent = "上传中 0%";
    setUploadMonitor(state.selectedFile, 0, state.selectedFile.size);
    try {
      const job = await uploadJob(form, (loaded, total) => {
        const percent = total > 0 ? Math.round((loaded / total) * 100) : 0;
        submit.querySelector("span").textContent = `上传中 ${percent}%`;
        setUploadMonitor(state.selectedFile, loaded, total);
      });
      await loadRecentJobs();
      if (state.activeView === "upload") await selectJob(job.id);
    } catch (error) {
      showFormError(error.message);
      setServiceState("error", "服务请求失败");
      submit.disabled = false;
    } finally {
      submit.querySelector("span").textContent = "开始处理";
    }
  }

  async function controlJob() {
    const button = $("pause-button");
    const jobId = state.jobId;
    const action = button.dataset.action;
    if (!jobId || !["pause", "resume"].includes(action)) return;
    button.disabled = true;
    try {
      await apiFetch(`/api/jobs/${jobId}/${action}`, { method: "POST" });
      await selectJob(jobId);
      await loadRecentJobs();
    } catch (error) {
      $("task-message").textContent = error.message;
      button.disabled = false;
    }
  }

  async function loadRecentJobs() {
    try {
      const response = await apiFetch("/api/jobs?limit=8");
      const data = await response.json();
      const list = $("recent-list");
      if (!data.items?.length) { list.innerHTML = '<p class="recent-empty">暂无历史任务</p>'; return; }
      const labels = { queued: "排队中", processing: "处理中", paused: "已暂停", succeeded: "已完成", failed: "失败" };
      list.innerHTML = data.items.map((job) => `
        <button class="recent-item${job.id === state.jobId ? " is-active" : ""}" type="button" data-job-id="${escapeHtml(job.id)}" aria-controls="task-monitor" aria-pressed="${job.id === state.jobId}" aria-label="查看任务 ${escapeHtml(job.filename)}，状态 ${labels[job.status] || escapeHtml(job.status)}">
          <span class="recent-name" title="${escapeHtml(job.filename)}">${escapeHtml(job.filename)}</span>
          <span class="recent-mode">${escapeHtml(job.mode_label)}</span>
          <span class="recent-status ${job.status}">${labels[job.status] || job.status}</span>
          <span class="recent-open" aria-hidden="true">→</span>
        </button>`).join("");
      list.querySelectorAll(".recent-item").forEach((item) => {
        item.addEventListener("click", () => selectJob(item.dataset.jobId));
      });
    } catch (error) {
      $("recent-list").innerHTML = `<p class="recent-empty">无法加载任务记录：${error.message}</p>`;
    }
  }

  async function loadConfig() {
    try {
      const response = await apiFetch("/api/config");
      state.config = await response.json();
      renderModes(state.config.modes);
      $("api-key-wrap").hidden = !state.config.api_key_required;
      if (state.config.max_upload_bytes) {
        $("dropzone").querySelector(".dropzone-subtitle").textContent = `或点击选择文件，限制 ${formatBytes(state.config.max_upload_bytes)}`;
      }
      setServiceState("ready", "服务已连接");
      await loadRecentJobs();
    } catch (error) {
      setServiceState("error", "需要 API Key 或服务不可用");
      $("mode-list").innerHTML = '<p class="field-note">无法读取处理配置，请检查服务或 API Key。</p>';
    }
  }

  async function checkHealth() {
    try {
      const response = await fetch("/api/health");
      if (!response.ok) throw new Error("health check failed");
      setServiceState("ready", "服务已连接");
    } catch (_) {
      setServiceState("error", "服务不可用");
    }
  }

  function bindDropzone() {
    const dropzone = $("dropzone");
    const fileInput = $("video-file");
    fileInput.addEventListener("change", () => showFile(fileInput.files[0] || null));
    ["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.remove("is-dragging"); }));
    dropzone.addEventListener("drop", (event) => showFile(event.dataTransfer.files[0] || null));
    dropzone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });
  }

  $("job-form").addEventListener("submit", submitJob);
  $("refresh-jobs").addEventListener("click", loadRecentJobs);
  $("pause-button").addEventListener("click", controlJob);
  $("retry-button").addEventListener("click", () => $("job-form").requestSubmit());
  bindDropzone();
  checkHealth();
  loadConfig();
})();
