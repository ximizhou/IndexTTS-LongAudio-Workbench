(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    tab: "create",
    voices: [],
    selectedVoice: null,
    voiceFilter: "all",
    emotionMode: "voice",
    jobs: [],
    selectedJob: null,
    pollTimer: null,
    refreshBusy: false,
  };

  const statusLabels = {
    created: "待开始",
    queued: "排队中",
    running: "生成中",
    paused: "已暂停",
    completed: "已完成",
    failed: "有失败",
    cancelled: "已取消",
  };
  const activeStatuses = new Set(["queued", "running"]);

  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  }

  function toast(message, error = false) {
    const node = $("toast");
    node.textContent = message || "";
    node.classList.toggle("error", error);
    node.classList.add("show");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => node.classList.remove("show"), 3600);
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* Empty responses are valid for DELETE. */ }
    if (!response.ok) {
      const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg || String(item)).join("；") : payload.detail;
      throw new Error(detail || `请求失败 (${response.status})`);
    }
    return payload;
  }

  function showTab(name) {
    if (!["create", "voices", "jobs"].includes(name)) name = "create";
    state.tab = name;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".workspace-panel").forEach((panel) => {
      const active = panel.id === `panel-${name}`;
      panel.classList.toggle("active", active);
      panel.hidden = !active;
    });
    if (name === "voices") loadVoices();
    if (name === "jobs") refreshJobs();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function selectedSettings() {
    const value = (id, fallback) => {
      const number = Number($(id).value);
      return Number.isFinite(number) ? number : fallback;
    };
    const lang = $("lang").value;
    const durationFactor = value("durationFactor", 1);
    const emotionWeight = value("emotionWeight", 0.65);
    const maxChars = Math.round(value("maxChars", 140));
    const maxTokens = Math.round(value("segmentTokens", 120));
    const seed = Math.max(1, Math.min(4294967295, Math.round(value("seed", 42))));
    const emotionMethod = state.emotionMode === "text" ? "prompt" : "reference";
    return {
      // Current FastAPI adapter names.
      voice_id: state.selectedVoice?.id || null, lang, max_chars: maxChars, pause_ms: Math.round(value("pauseMs", 260)),
      duration_factor: durationFactor, use_emo_text: state.emotionMode === "text", emotion_text: $("emotionText").value.trim(),
      emotion_random: $("emotionRandom").checked, max_text_tokens_per_segment: maxTokens,
      temperature: value("temperature", 0.65), top_p: value("topP", 0.72), top_k: Math.round(value("topK", 25)),
      max_mel_tokens: Math.round(value("maxMelTokens", 1500)), repetition_penalty: value("repetitionPenalty", 10),
      seed, emo_alpha: emotionWeight, emotion_vector: [0, 0, 0, 0, 0, 0, 0, 0], num_beams: 3, do_sample: true,
      sampling: { seed, temperature: value("temperature", 0.65), top_p: value("topP", 0.72), top_k: Math.round(value("topK", 25)) },
      // Canonical IndexTTS names remain in the request for newer adapters.
      language: lang, emotion_method: emotionMethod, speech_rate: durationFactor, emotion_weight: emotionWeight,
      max_segment_tokens: maxTokens, reference_voice_id: state.selectedVoice?.id || null, reference_audio_id: state.selectedVoice?.id || null,
    };
  }

  function updateControlReadouts() {
    $("maxCharsValue").textContent = $("maxChars").value;
    $("tokenValue").textContent = $("segmentTokens").value;
    $("durationValue").textContent = `${Number($("durationFactor").value).toFixed(2)}×`;
    $("emotionWeightValue").textContent = Number($("emotionWeight").value).toFixed(2);
    $("barVoiceName").textContent = state.selectedVoice?.name || "未选择";
    $("barSettings").textContent = `${$("maxChars").value} 字/段 · ${$("segmentTokens").value} token · ${Number($("durationFactor").value).toFixed(2)}×`;
    const text = $("scriptInput").value;
    $("charCount").textContent = `${text.length.toLocaleString()} 字`;
    $("textNotice").textContent = text.trim() ? "文稿已就绪" : "等待输入";
  }

  function normalizeVoice(raw, index) {
    return {
      ...raw,
      id: raw.id || `voice-${index}`,
      name: raw.name || `参考音色 ${index + 1}`,
      description: raw.description || raw.style || "参考音频",
      kind: raw.kind || "saved",
      gender: raw.gender || "",
      language: raw.language || "中文",
    };
  }

  function voiceMeta(voice) {
    return [voice.gender, voice.language, voice.kind === "preset" ? "内置参考" : "我的音色"].filter(Boolean).join(" · ");
  }

  function selectVoice(voice) {
    state.selectedVoice = voice || null;
    $("selectedVoiceName").textContent = voice?.name || "选择一个音色";
    $("selectedVoiceMeta").textContent = voice ? voiceMeta(voice) : "打开音色库试听参考音频";
    $("selectedVoiceAvatar").textContent = (voice?.name || "I").slice(0, 1).toUpperCase();
    $("barVoiceName").textContent = voice?.name || "未选择";
    renderVoices();
  }

  function renderVoices() {
    const target = $("voiceGrid");
    if (!target) return;
    const filter = state.voiceFilter;
    const voices = state.voices.filter((voice) => {
      if (filter === "saved") return voice.kind !== "preset";
      if (filter === "female") return /女|female/i.test(voice.gender || voice.description || "");
      if (filter === "male") return /男|male/i.test(voice.gender || voice.description || "");
      return true;
    });
    $("voiceCount").textContent = `${voices.length} 个参考音色`;
    target.replaceChildren();
    if (!voices.length) {
      target.innerHTML = '<div class="empty-state">没有匹配的参考音色</div>';
      return;
    }
    voices.forEach((voice) => {
      const card = document.createElement("article");
      card.className = `voice-card${state.selectedVoice?.id === voice.id ? " selected" : ""}`;
      card.innerHTML = `<div class="voice-card-top"><span class="voice-avatar">${escapeHtml((voice.name || "I").slice(0, 1))}</span><span class="voice-card-copy"><strong>${escapeHtml(voice.name)}</strong><small>${escapeHtml(voice.description || "参考音频")}</small></span></div><div class="voice-card-meta">${escapeHtml(voiceMeta(voice))}</div><div class="voice-card-actions"><button class="outline-button audition" type="button">试听</button><button class="primary-button use" type="button">使用</button>${voice.kind !== "preset" ? '<button class="icon-button delete" type="button" title="删除音色" aria-label="删除音色">×</button>' : ""}</div>`;
      card.querySelector(".audition").addEventListener("click", (event) => { event.stopPropagation(); previewVoice(voice); });
      card.querySelector(".use").addEventListener("click", (event) => { event.stopPropagation(); selectVoice(voice); showTab("create"); toast(`已选择“${voice.name}”`); });
      const deleteButton = card.querySelector(".delete");
      if (deleteButton) deleteButton.addEventListener("click", (event) => { event.stopPropagation(); deleteVoice(voice); });
      card.addEventListener("click", () => selectVoice(voice));
      target.appendChild(card);
    });
  }

  async function loadVoices(preferredId = null) {
    try {
      const payload = await api("/api/voices");
      const presets = Array.isArray(payload.presets) ? payload.presets : [];
      const saved = Array.isArray(payload.saved) ? payload.saved : [];
      state.voices = [...presets, ...saved].map(normalizeVoice);
      const wanted = preferredId || state.selectedVoice?.id;
      const selected = state.voices.find((voice) => voice.id === wanted) || state.voices[0] || null;
      selectVoice(selected);
      $("serviceStatus").innerHTML = "<i></i>服务在线";
      $("serviceStatus").classList.add("online");
      renderVoices();
    } catch (error) {
      $("serviceStatus").innerHTML = "<i></i>服务未连接";
      $("serviceStatus").classList.remove("online");
      toast(error.message, true);
    }
  }

  function renderSegments(data) {
    const list = $("segmentList");
    list.replaceChildren();
    (data.segments || []).forEach((segment) => {
      const row = document.createElement("div");
      row.className = "segment-line";
      row.innerHTML = `<span class="segment-index">${String((segment.index ?? 0) + 1).padStart(2, "0")}</span><span>${escapeHtml(segment.text)}</span><span class="segment-count">${segment.char_count || String(segment.text || "").length}字</span>`;
      list.appendChild(row);
    });
    $("segmentSummary").textContent = `${(data.segments || []).length} 段 · ${(data.normalized_text || "").length.toLocaleString()} 字`;
    $("segmentPreview").hidden = false;
  }

  async function previewSplit() {
    const text = $("scriptInput").value.trim();
    if (!text) { toast("请先输入文稿", true); return; }
    try {
      const payload = await api("/api/preview", { method: "POST", body: JSON.stringify({ text, ...selectedSettings() }) });
      renderSegments(payload);
      $("textNotice").textContent = `已切分为 ${payload.segments?.length || 0} 段`;
    } catch (error) { toast(error.message, true); }
  }

  async function previewVoice(voice = state.selectedVoice) {
    if (!voice?.id) { toast("请先选择音色", true); showTab("voices"); return; }
    const button = $("previewVoice");
    button.disabled = true;
    try {
      const payload = await api("/api/voices/preview", { method: "POST", body: JSON.stringify({ text: "欢迎使用 IndexTTS 长音频工作台，这是一段参考音色试听。", ...selectedSettings(), voice_id: voice.id }) });
      $("voicePreviewAudio").src = payload.audio_url;
      $("voicePreviewBox").hidden = false;
      $("voicePreviewAudio").play().catch(() => {});
      toast(`“${voice.name}”试听已生成`);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }

  async function createJob() {
    const text = $("scriptInput").value.trim();
    if (!text) { toast("请先输入文稿", true); return; }
    if (!state.selectedVoice?.id) { toast("请先选择参考音色", true); showTab("voices"); return; }
    const button = $("createJob");
    button.disabled = true;
    try {
      const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ text, ...selectedSettings() }) });
      await api(`/api/jobs/${encodeURIComponent(job.job_id)}/start`, { method: "POST" });
      await refreshJobs();
      await selectJob(job.job_id);
      showTab("jobs");
      toast(`任务已开始，共 ${job.segments?.length || job.summary?.segment_count || 0} 段`);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }

  function jobStatus(status) { return statusLabels[status] || status || "未知"; }

  function renderJobList() {
    const list = $("jobList");
    list.replaceChildren();
    if (!state.jobs.length) { list.innerHTML = '<div class="empty-state">暂无任务</div>'; return; }
    state.jobs.forEach((job) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `job-row${state.selectedJob?.job_id === job.job_id ? " selected" : ""}`;
      const progress = Math.round((job.progress || 0) * 100);
      row.innerHTML = `<span class="job-row-top"><strong>${escapeHtml(job.job_id)}</strong><em class="status ${escapeHtml(job.status)}">${jobStatus(job.status)}</em></span><span class="job-progress"><i style="width:${progress}%"></i></span><span class="job-meta"><span>${job.completed_count || 0}/${job.segment_count || 0} 段</span><span>${progress}%</span></span>`;
      row.addEventListener("click", () => selectJob(job.job_id));
      list.appendChild(row);
    });
  }

  function renderJobDetail(job) {
    const detail = $("jobDetail");
    if (!job) {
      detail.innerHTML = '<div class="empty-state"><span>03</span><strong>选择一个任务</strong><p>查看片段进度、试听和最终文件。</p></div>';
      return;
    }
    const summary = job.summary || {};
    const progress = Math.round((summary.progress ?? job.progress ?? 0) * 100);
    const segments = job.segments || [];
    const output = job.outputs || summary.outputs || {};
    const actionButton = (action, label, visible) => visible ? `<button class="outline-button job-action" data-action="${action}" type="button">${label}</button>` : "";
    detail.innerHTML = `<div class="job-detail-head"><div><span class="eyebrow">TASK</span><h3>${escapeHtml(job.job_id)}</h3></div><span class="status ${escapeHtml(job.status)}">${jobStatus(job.status)}</span></div><div class="job-detail-progress"><div><span>${summary.completed_count || 0}/${summary.segment_count || segments.length} 段完成</span><strong>${progress}%</strong></div><span><i style="width:${progress}%"></i></span></div><div class="job-actions">${actionButton("start", "开始", ["created"].includes(job.status))}${actionButton("pause", "暂停", job.status === "running")}${actionButton("resume", "继续", job.status === "paused")}${actionButton("retry", "重试失败片段", job.status === "failed")}${actionButton("cancel", "取消", ["created", "queued", "running", "paused"].includes(job.status))}</div><div class="job-segments"><div class="mini-heading"><span>片段</span><strong>${segments.length} 段</strong></div><div class="segment-list">${segments.map((segment) => { const ok = ["success", "succeeded"].includes(segment.status); const url = `/api/jobs/${encodeURIComponent(job.job_id)}/segments/${segment.index}/audio`; return `<div class="segment-line"><span class="segment-index">${String((segment.index ?? 0) + 1).padStart(2, "0")}</span><span class="segment-copy"><span>${escapeHtml(segment.text)}</span><small class="segment-status ${escapeHtml(segment.status || "")}">${jobStatus(segment.status)}${segment.error ? ` · ${escapeHtml(segment.error)}` : ""}</small></span><span class="segment-media">${ok ? `<audio controls preload="none" src="${url}"></audio><a href="${url}" download title="下载片段">下载</a>` : ""}</span></div>`; }).join("") || '<span class="empty-state">没有片段</span>'}</div></div>${output.wav || output.mp3 ? `<div class="output-links"><span>最终文件</span>${output.wav ? `<a href="/api/jobs/${encodeURIComponent(job.job_id)}/download/wav" download>下载 WAV</a>` : ""}${output.mp3 ? `<a href="/api/jobs/${encodeURIComponent(job.job_id)}/download/mp3" download>下载 MP3</a>` : ""}</div>` : ""}`;
    detail.querySelectorAll(".job-action").forEach((button) => button.addEventListener("click", () => jobAction(button.dataset.action)));
  }

  async function refreshJobs() {
    if (state.refreshBusy) return;
    state.refreshBusy = true;
    try {
      state.jobs = await api("/api/jobs");
      renderJobList();
      const active = state.jobs.some((job) => activeStatuses.has(job.status));
      if (active && !state.pollTimer) state.pollTimer = window.setInterval(refreshJobs, 1800);
      if (!active && state.pollTimer) { window.clearInterval(state.pollTimer); state.pollTimer = null; }
      if (state.selectedJob?.job_id) {
        const current = state.jobs.find((job) => job.job_id === state.selectedJob.job_id);
        if (current) await selectJob(current.job_id, false);
      }
    } catch (error) {
      if (state.tab === "jobs") toast(error.message, true);
    } finally { state.refreshBusy = false; }
  }

  async function selectJob(jobId, announce = true) {
    try {
      state.selectedJob = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderJobList();
      renderJobDetail(state.selectedJob);
      if (announce) showTab("jobs");
    } catch (error) { toast(error.message, true); }
  }

  async function jobAction(action) {
    if (!state.selectedJob?.job_id) return;
    try {
      state.selectedJob = await api(`/api/jobs/${encodeURIComponent(state.selectedJob.job_id)}/${action}`, { method: "POST" });
      renderJobDetail(state.selectedJob);
      await refreshJobs();
      toast({ start: "任务已开始", pause: "任务已暂停", resume: "任务已继续", retry: "失败片段已重新排队", cancel: "任务已取消" }[action] || "操作完成");
    } catch (error) { toast(error.message, true); }
  }

  async function uploadVoice() {
    const file = $("voiceFile").files[0];
    if (!file) { toast("请先选择参考音频", true); return; }
    const extension = file.name.toLowerCase().split(".").pop();
    if (!["wav", "mp3", "flac"].includes(extension)) { toast("参考音频只支持 WAV、MP3 或 FLAC", true); return; }
    if (file.size > 50 * 1024 * 1024) { toast("参考音频不能超过 50 MB", true); return; }
    const name = $("voiceNameInput").value.trim() || file.name.replace(/\.[^.]+$/, "");
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    form.append("description", $("voiceDescriptionInput").value.trim());
    const button = $("uploadVoice");
    button.disabled = true;
    $("uploadStatus").textContent = "正在保存……";
    try {
      const voice = await api("/api/voices/upload", { method: "POST", body: form });
      $("voiceFile").value = "";
      $("voiceFileLabel").textContent = "选择音频文件";
      $("voiceNameInput").value = "";
      $("voiceDescriptionInput").value = "";
      $("uploadStatus").textContent = "已保存到音色库";
      await loadVoices(voice.id);
      toast("参考音色已保存");
    } catch (error) { $("uploadStatus").textContent = error.message; toast(error.message, true); }
    finally { button.disabled = false; }
  }

  async function deleteVoice(voice) {
    if (!window.confirm(`删除“${voice.name}”？已生成的任务不会受影响。`)) return;
    try {
      await api(`/api/voices/${encodeURIComponent(voice.id)}`, { method: "DELETE" });
      if (state.selectedVoice?.id === voice.id) state.selectedVoice = null;
      await loadVoices();
      toast("参考音色已删除");
    } catch (error) { toast(error.message, true); }
  }

  function bind() {
    document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => showTab(button.dataset.tab)));
    $("openVoices").addEventListener("click", () => showTab("voices"));
    $("selectedVoice").addEventListener("click", () => showTab("voices"));
    $("backToCreate").addEventListener("click", () => showTab("create"));
    $("refreshAll").addEventListener("click", () => Promise.all([loadVoices(), refreshJobs()]));
    $("refreshJobs").addEventListener("click", refreshJobs);
    $("previewSegments").addEventListener("click", previewSplit);
    $("previewVoice").addEventListener("click", () => previewVoice());
    $("createJob").addEventListener("click", createJob);
    $("clearText").addEventListener("click", () => { $("scriptInput").value = ""; $("segmentPreview").hidden = true; updateControlReadouts(); });
    $("scriptInput").addEventListener("input", updateControlReadouts);
    ["maxChars", "segmentTokens", "durationFactor", "emotionWeight", "pauseMs", "seed", "temperature", "topP", "topK", "maxMelTokens", "repetitionPenalty"].forEach((id) => $(id).addEventListener("input", updateControlReadouts));
    $("textFile").addEventListener("change", async (event) => { const file = event.target.files[0]; if (!file) return; try { $("scriptInput").value = await file.text(); updateControlReadouts(); toast(`已导入 ${file.name}`); } catch (error) { toast(error.message, true); } event.target.value = ""; });
    document.querySelectorAll("[data-emotion]").forEach((button) => button.addEventListener("click", () => { state.emotionMode = button.dataset.emotion; document.querySelectorAll("[data-emotion]").forEach((item) => item.classList.toggle("active", item === button)); $("emotionText").disabled = state.emotionMode !== "text"; }));
    document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => { state.voiceFilter = button.dataset.filter; document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button)); renderVoices(); }));
    $("voiceFile").addEventListener("change", () => { const file = $("voiceFile").files[0]; if (file) { $("voiceFileLabel").textContent = file.name; if (!$("voiceNameInput").value) $("voiceNameInput").value = file.name.replace(/\.[^.]+$/, ""); } });
    $("uploadVoice").addEventListener("click", uploadVoice);
  }

  bind();
  $("emotionText").disabled = true;
  updateControlReadouts();
  showTab("create");
  Promise.allSettled([loadVoices(), refreshJobs()]);
})();
