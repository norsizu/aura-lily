const $ = (id) => document.getElementById(id);

const adminUserInput = $("adminUser");
const adminPasswordInput = $("adminPassword");
adminUserInput.value = localStorage.getItem("aura_lily_admin_user") || "admin";
adminPasswordInput.value = sessionStorage.getItem("aura_lily_admin_password") || "";

let providerPresets = [];
let hermesModelProfiles = [];
let auraModelProfiles = [];
let ttsPresets = [];
let asrPresets = [];
let ttsProfiles = [];
let asrProfiles = [];
let lastSummary = {};
let revealed = {
  hermes: false,
  auraModel: false,
  fastReply: false,
  tts: false,
  asr: false,
};

const headers = () => ({
  "content-type": "application/json",
  "authorization": `Basic ${basicCredential(adminUserInput.value.trim(), adminPasswordInput.value)}`,
});

function basicCredential(user, pass) {
  const bytes = new TextEncoder().encode(`${user}:${pass}`);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function setStatus(id, text, ok = true) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = `status ${ok ? "ok" : "warn"}`;
}

function setBadge(id, text, tone = "muted") {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = `badge ${tone}`;
}

function setTestResult(id, payload, ok = true) {
  const el = $(id);
  if (!el) return;
  const parts = [];
  if (payload?.provider || payload?.model) parts.push([payload.provider, payload.model].filter(Boolean).join(" / "));
  if (payload?.stage) parts.push(`stage: ${payload.stage}`);
  if (payload?.endpoint_host) parts.push(`host: ${payload.endpoint_host}`);
  if (payload?.source_sample_rate || payload?.device_sample_rate) {
    parts.push(`source ${payload.source_sample_rate || "?"}Hz -> device ${payload.device_sample_rate || "?"}Hz`);
  } else if (payload?.requested_sample_rate) {
    parts.push(`sample_rate ${payload.requested_sample_rate}Hz`);
  }
  if (payload?.resampled_for_device) parts.push("已重采样到设备播放率");
  if (Number.isFinite(Number(payload?.audio_bytes)) && Number(payload.audio_bytes) > 0) parts.push(`audio ${payload.audio_bytes} bytes`);
  if (Number.isFinite(Number(payload?.latency_ms)) && Number(payload.latency_ms) > 0) parts.push(`${payload.latency_ms}ms`);
  if (payload?.detail) parts.push(payload.detail);
  el.replaceChildren();
  const summary = document.createElement("div");
  summary.textContent = parts.filter(Boolean).join(" · ") || (ok ? "测试通过。" : "测试失败。");
  el.appendChild(summary);
  if (payload?.audio_data_url) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = payload.audio_data_url;
    audio.className = "test-audio";
    el.appendChild(audio);
    const caption = document.createElement("div");
    caption.className = "mini";
    caption.textContent = "试听为 Lily 发给 ESP32 前的设备播放版 WAV。";
    el.appendChild(caption);
  }
  el.className = `test-result ${ok ? "ok" : "warn"}`;
}

function val(id, value) {
  const el = $(id);
  if (el) el.value = value ?? "";
}

function bool(id, value) {
  const el = $(id);
  if (el) el.checked = Boolean(value);
}

function num(id) {
  const value = Number($(id)?.value || 0);
  return Number.isFinite(value) ? value : 0;
}

function intOr(id, fallback) {
  const value = num(id);
  return value > 0 ? Math.round(value) : fallback;
}

function formatWorldTime(value) {
  const date = new Date(Number(value || 0) * 1000);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function csv(items) {
  return Array.isArray(items) ? items.join(",") : "";
}

function mergeTextValues(...groups) {
  const out = [];
  const seen = new Set();
  for (const group of groups) {
    for (const value of group || []) {
      const text = String(value || "").trim();
      const key = text.toLowerCase();
      if (!text || seen.has(key)) continue;
      seen.add(key);
      out.push(text);
    }
  }
  return out;
}

function saveLoginLocally() {
  localStorage.setItem("aura_lily_admin_user", adminUserInput.value.trim() || "admin");
  sessionStorage.setItem("aura_lily_admin_password", adminPasswordInput.value);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_) {
    payload = { ok: false, error: text || response.statusText };
  }
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

async function testApi(path) {
  const response = await fetch(path, { headers: headers() });
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_) {
    payload = { ok: false, error: text || response.statusText };
  }
  if (!response.ok || payload.ok === false) {
    return {
      ...payload,
      ok: false,
      detail: payload.detail || payload.error || response.statusText,
    };
  }
  return payload;
}

function selectedPreset() {
  return providerPresets.find((item) => item.id === $("providerPreset").value) || null;
}

function selectedAuraPreset() {
  return providerPresets.find((item) => item.id === $("auraProviderPreset").value) || null;
}

function selectedTtsPreset() {
  return ttsPresets.find((item) => item.id === $("ttsPreset").value) || null;
}

function selectedAsrPreset() {
  return asrPresets.find((item) => item.id === $("asrPreset").value) || null;
}

function optionLabel(item) {
  const label = item.label || item.provider || item.id;
  const aliases = Array.isArray(item.aliases) && item.aliases.length ? ` · ${item.aliases.slice(0, 3).join("/")}` : "";
  return `${label}${aliases}`;
}

function populateModelProfiles(selectId, profiles) {
  const select = $(selectId);
  if (!select) return;
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = profiles.length ? "选择已保存的模型配置" : "暂无已保存配置";
  select.appendChild(empty);
  for (const item of profiles) {
    const option = document.createElement("option");
    option.value = `${item.provider || ""}\n${item.model || ""}\n${item.base_url || ""}`;
    option.textContent = `${item.label || `${item.provider || "自定义"} / ${item.model || "未命名"}`} · ${item.api_key_configured ? "Key 已保存" : "未配置 Key"}`;
    option.dataset.profile = JSON.stringify(item);
    select.appendChild(option);
  }
}

function profileFromSelect(id) {
  const option = $(id)?.selectedOptions?.[0];
  if (!option?.dataset.profile) return null;
  try { return JSON.parse(option.dataset.profile); } catch (_) { return null; }
}

function applyHermesModelProfile(profile) {
  if (!profile) return;
  val("provider", profile.provider);
  val("model", profile.model);
  val("baseUrl", profile.base_url);
  val("hermesProfileLabel", profile.label);
  maskInput("apiKey", profile.api_key_configured, "粘贴上游 API Key");
  populateProviderPresetSelect({ provider: profile.provider, base_url: profile.base_url });
  refreshPresetModels(false);
  setStatus("hermesStatus", `已载入：${profile.label || profile.model}`, true);
}

function applyAuraModelProfile(profile) {
  if (!profile) return;
  val("auraModelProvider", profile.provider);
  val("auraModelModel", profile.model);
  val("auraModelBaseUrl", profile.base_url);
  val("auraModelProfileLabel", profile.label);
  maskInput("auraModelApiKey", profile.api_key_configured, "粘贴 Aura LLM API Key");
  populateAuraPresetSelect({ aura_model_provider: profile.provider, aura_model_base_url: profile.base_url });
  refreshAuraPresetModels(false);
  setStatus("auraRuntimeStatus", `已载入：${profile.label || profile.model}`, true);
}

function renderQuotaCard(id, title, item) {
  const el = $(id);
  if (!el) return;
  el.replaceChildren();
  const heading = document.createElement("strong");
  heading.textContent = title;
  const detail = document.createElement("span");
  detail.textContent = item?.ok ? (item.display || "已读取") : (item?.error || "未读取");
  el.append(heading, detail);
  el.className = `quota-card ${item?.ok ? "ok" : "muted"}`;
}

async function refreshQuotas() {
  try {
    const payload = await api("/admin/quota");
    renderQuotaCard("auraQuotaCard", "Aura 对话模型额度", payload.aura);
    renderQuotaCard("hermesQuotaCard", "Hermes 模型额度", payload.hermes);
  } catch (err) {
    renderQuotaCard("auraQuotaCard", "Aura 对话模型额度", { error: err.message });
    renderQuotaCard("hermesQuotaCard", "Hermes 模型额度", { error: err.message });
  }
}

function populateGroupedSelect(select, items, currentConfig, matcher, emptyText) {
  if (!select) return;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = emptyText;
  select.appendChild(empty);

  const groups = [];
  for (const item of items) {
    const groupName = item.group || "其他";
    let group = groups.find((candidate) => candidate.label === groupName);
    if (!group) {
      group = document.createElement("optgroup");
      group.label = groupName;
      groups.push(group);
      select.appendChild(group);
    }
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = optionLabel(item);
    if (matcher(item, currentConfig)) opt.selected = true;
    group.appendChild(opt);
  }
}

function populateProviderPresetSelect(currentConfig = {}) {
  populateGroupedSelect(
    $("providerPreset"),
    providerPresets,
    currentConfig,
    (item, config) => item.provider === config.provider && (!config.base_url || !item.base_url || item.base_url === config.base_url),
    "选择 Hermes 上游厂商",
  );
}

function populateAuraPresetSelect(currentConfig = {}) {
  populateGroupedSelect(
    $("auraProviderPreset"),
    providerPresets,
    currentConfig,
    (item, config) => item.provider === config.aura_model_provider && (!config.aura_model_base_url || !item.base_url || item.base_url === config.aura_model_base_url),
    "选择 Aura 上游厂商",
  );
}

function populateTtsPresetSelect(currentConfig = {}) {
  populateGroupedSelect(
    $("ttsPreset"),
    ttsPresets.map((item) => ({ ...item, group: ttsGroup(item) })),
    currentConfig,
    (item, config) => item.provider === config.tts_provider && (!config.tts_base_url || !item.base_url || item.base_url === config.tts_base_url),
    "选择 TTS 供应商",
  );
}

function populateAsrPresetSelect(currentConfig = {}) {
  populateGroupedSelect(
    $("asrPreset"),
    asrPresets.map((item) => ({ ...item, group: asrGroup(item) })),
    currentConfig,
    (item, config) => item.provider === config.asr_provider && (!config.asr_base_url || !item.base_url || item.base_url === config.asr_base_url),
    "选择 ASR 供应商",
  );
}

function ttsGroup(item) {
  if (item.id === "none") return "关闭";
  if (item.id === "edge" || item.id === "voxcpm-aura") return "本地/免密钥";
  if (item.billing_scope === "step_plan") return "StepFun Step Plan";
  if (item.billing_scope === "open_platform") return "非 Plan 备用";
  if (item.id === "minimax") return "国内主流";
  if (item.id === "custom" || item.id === "custom-http") return "自定义";
  return "国际/自定义";
}

function asrGroup(item) {
  if (item.mode === "local" || item.provider === "local") return "本地模型";
  if (item.billing_scope === "step_plan") return "StepFun Step Plan";
  if (item.billing_scope === "open_platform") return "非 Plan 备用";
  if (item.id === "custom") return "自定义";
  return "API 供应商";
}

function fillModelList(listId, models) {
  const list = $(listId);
  if (!list) return;
  list.innerHTML = "";
  for (const model of models || []) {
    const opt = document.createElement("option");
    opt.value = model;
    list.appendChild(opt);
  }
}

function presetHint(preset) {
  if (!preset) return "";
  const keyText = preset.requires_api_key ? "需要 API Key" : "通常不需要 API Key";
  const urlText = preset.requires_base_url ? "，需要 Base URL" : "";
  const scopeText = preset.billing_scope === "step_plan"
    ? " · Step Plan 订阅内"
    : preset.billing_scope === "open_platform"
    ? " · 非 Step Plan 路由"
    : "";
  const recommendedText = preset.recommended ? " · 推荐" : "";
  const routeText = preset.route ? ` · 路由 ${preset.route}` : "";
  const streamText = preset.streaming === true ? " · 实时流式" : preset.streaming === false ? " · 非实时流式" : "";
  const description = preset.description ? ` ${preset.description}` : "";
  return `${preset.label || preset.id}：${keyText}${urlText}${scopeText}${recommendedText}${routeText}${streamText}。${description}`;
}

function refreshPresetModels(applyDefaults = true) {
  const preset = selectedPreset();
  fillModelList("modelList", preset?.models || []);
  if (!preset) {
    $("providerHint").textContent = "没有匹配 preset 时可以手动填写 provider/base URL/model。";
    return;
  }
  if (applyDefaults) {
    val("provider", preset.provider || "");
    val("baseUrl", preset.base_url || "");
    if (Array.isArray(preset.models) && preset.models.length) val("model", preset.models[0]);
  }
  $("providerHint").textContent = presetHint(preset);
}

function refreshAuraPresetModels(applyDefaults = true) {
  const preset = selectedAuraPreset();
  fillModelList("auraModelList", preset?.models || []);
  if (!preset) {
    $("auraProviderHint").textContent = "没有匹配 preset 时可以手动填写 provider/base URL/model。";
    return;
  }
  if (applyDefaults) {
    val("auraModelProvider", preset.provider || "");
    val("auraModelBaseUrl", preset.base_url || "");
    if (Array.isArray(preset.models) && preset.models.length) val("auraModelModel", preset.models[0]);
  }
  $("auraProviderHint").textContent = presetHint(preset);
}

function refreshTtsPreset(applyDefaults = true) {
  const preset = selectedTtsPreset();
  fillModelList("ttsModelList", mergeTextValues(preset?.models || [], ttsProfiles.map((item) => item.model)));
  fillModelList("ttsVoiceList", mergeTextValues(preset?.voices || [], ttsProfiles.map((item) => item.voice)));
  if (!preset) {
    $("ttsHint").textContent = "";
    return;
  }
  if (applyDefaults) {
    val("ttsProvider", preset.provider || "none");
    val("ttsBaseUrl", preset.base_url || "");
    if (Array.isArray(preset.models) && preset.models.length) val("ttsModel", preset.models[0]);
    if (Array.isArray(preset.voices) && preset.voices.length) val("ttsVoice", preset.voices[0]);
  }
  $("ttsHint").textContent = presetHint(preset);
}

function refreshAsrPreset(applyDefaults = true) {
  const preset = selectedAsrPreset();
  fillModelList("asrModelList", mergeTextValues(preset?.models || [], asrProfiles.map((item) => item.model)));
  if (!preset) {
    $("asrHint").textContent = "";
    return;
  }
  if (applyDefaults) {
    val("asrMode", preset.mode || "api");
    val("asrProvider", preset.provider || "custom");
    val("asrBaseUrl", preset.base_url || "");
    if (Array.isArray(preset.models) && preset.models.length) val("asrModel", preset.models[0]);
  }
  $("asrHint").textContent = presetHint(preset);
  updateAsrModeVisibility();
}

function maskInput(id, configured, emptyText) {
  const el = $(id);
  if (!el) return;
  el.type = "password";
  el.value = "";
  el.placeholder = configured ? "•••••••• 已保存；留空则不修改" : emptyText;
}

function updateAuraModelVisibility() {
  const independent = $("auraModelMode").value === "aura_model";
  $("auraModelFields").classList.toggle("disabled-block", !independent);
  $("clearAuraModelKey").disabled = !independent;
  $("clearAuraModelKeyBottom").disabled = !independent;
}

function updateAsrModeVisibility() {
  const apiMode = $("asrMode").value === "api";
  $("asrBaseUrl").placeholder = apiMode ? "本机 HTTP 或 OpenAI-compatible ASR Base URL" : "本地命令模式通常留空";
  $("asrApiKey").disabled = !apiMode;
  $("showAsrKey").disabled = !apiMode;
  $("clearAsrKey").disabled = !apiMode;
}

function currentTtsProfile() {
  return {
    id: "",
    label: $("ttsProfileName").value.trim() || profileAutoLabel("tts"),
    enabled: $("ttsEnabled").checked,
    provider: $("ttsProvider").value.trim(),
    model: $("ttsModel").value.trim(),
    voice: $("ttsVoice").value.trim(),
    base_url: $("ttsBaseUrl").value.trim(),
    audio_format: $("ttsFormat").value.trim(),
    sample_rate: num("ttsSampleRate"),
    timeout_seconds: num("ttsTimeout"),
  };
}

function currentAsrProfile() {
  return {
    id: "",
    label: $("asrProfileName").value.trim() || profileAutoLabel("asr"),
    enabled: $("asrEnabled").checked,
    mode: $("asrMode").value,
    provider: $("asrProvider").value.trim(),
    model: $("asrModel").value.trim(),
    base_url: $("asrBaseUrl").value.trim(),
    language: $("asrLanguage").value.trim(),
    timeout_seconds: num("asrTimeout"),
  };
}

function profileAutoLabel(kind) {
  if (kind === "tts") {
    return [$("ttsProvider").value, $("ttsModel").value, $("ttsVoice").value].map((part) => part.trim()).filter(Boolean).join(" / ") || "TTS 配置";
  }
  return [$("asrProvider").value, $("asrModel").value, $("asrLanguage").value].map((part) => part.trim()).filter(Boolean).join(" / ") || "ASR 配置";
}

function profileLabel(item, kind) {
  const head = item.label || (kind === "tts"
    ? [item.provider, item.model, item.voice].filter(Boolean).join(" / ")
    : [item.provider, item.model, item.language].filter(Boolean).join(" / "));
  const tail = item.builtin ? " · 内置" : "";
  return `${head || (kind === "tts" ? "TTS 配置" : "ASR 配置")}${tail}`;
}

function profileText(item) {
  const clean = { ...item };
  delete clean.api_key;
  return JSON.stringify(clean, null, 2);
}

function populateProfileSelect(kind, profiles) {
  const select = $(kind === "tts" ? "ttsProfile" : "asrProfile");
  if (!select) return;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = profiles.length ? "选择已保存配置" : "暂无保存配置";
  select.appendChild(empty);
  profiles.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.id || "";
    opt.textContent = profileLabel(item, kind);
    select.appendChild(opt);
  });
  previewProfile(kind);
}

function selectedProfile(kind) {
  const profiles = kind === "tts" ? ttsProfiles : asrProfiles;
  const id = $(kind === "tts" ? "ttsProfile" : "asrProfile").value;
  return profiles.find((item) => item.id === id) || null;
}

function previewProfile(kind) {
  const item = selectedProfile(kind);
  const target = kind === "tts" ? "ttsProfilePreview" : "asrProfilePreview";
  $(target).textContent = item ? profileText(item) : "";
}

function applyProfile(kind) {
  const item = selectedProfile(kind);
  if (!item) return;
  if (kind === "tts") {
    bool("ttsEnabled", item.enabled ?? true);
    val("ttsProvider", item.provider);
    val("ttsModel", item.model);
    val("ttsVoice", item.voice);
    val("ttsBaseUrl", item.base_url);
    val("ttsFormat", item.audio_format || "pcm");
    if (item.sample_rate) val("ttsSampleRate", item.sample_rate);
    if (item.timeout_seconds) val("ttsTimeout", item.timeout_seconds);
    val("ttsProfileName", item.label || "");
    refreshTtsPreset(false);
  } else {
    bool("asrEnabled", item.enabled ?? true);
    val("asrMode", item.mode || "api");
    val("asrProvider", item.provider);
    val("asrModel", item.model);
    val("asrBaseUrl", item.base_url);
    val("asrLanguage", item.language || "zh");
    if (item.timeout_seconds) val("asrTimeout", item.timeout_seconds);
    val("asrProfileName", item.label || "");
    refreshAsrPreset(false);
    updateAsrModeVisibility();
  }
  setStatus("auraRuntimeStatus", `${kind.toUpperCase()} 配置已套用到表单，保存后生效。`, true);
}

function upsertProfile(kind) {
  const current = kind === "tts" ? currentTtsProfile() : currentAsrProfile();
  const profiles = kind === "tts" ? [...ttsProfiles] : [...asrProfiles];
  const selected = selectedProfile(kind);
  if (selected && !selected.builtin) current.id = selected.id;
  const existingIndex = current.id ? profiles.findIndex((item) => item.id === current.id) : -1;
  if (existingIndex >= 0) {
    profiles[existingIndex] = { ...profiles[existingIndex], ...current, builtin: false };
  } else {
    profiles.push(current);
  }
  return saveProfiles(kind, profiles, `${kind.toUpperCase()} 配置已保存到配置池。`);
}

function deleteProfile(kind) {
  const selected = selectedProfile(kind);
  if (!selected) return Promise.resolve();
  if (selected.builtin) {
    setStatus("auraRuntimeStatus", "内置配置不能删除，可以另存一份后修改。", false);
    return Promise.resolve();
  }
  const profiles = (kind === "tts" ? ttsProfiles : asrProfiles).filter((item) => item.id !== selected.id);
  return saveProfiles(kind, profiles, `${kind.toUpperCase()} 配置已删除。`);
}

async function saveProfiles(kind, profiles, message) {
  const extra = kind === "tts" ? { tts_profiles: profiles } : { asr_profiles: profiles };
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload(extra)),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", message, true);
}

function fillDashboard(summary) {
  const hermes = summary.hermes || {};
  const aura = summary.aura_runtime || {};
  const persona = summary.persona || {};
  const location = summary.location || {};
  const state = summary.state || {};
  const worldPayload = summary.world || {};
  const world = worldPayload.world || {};

  const hermesName = [hermes.provider, hermes.model].filter(Boolean).join(" / ") || "未配置";
  $("dashHermesModel").textContent = hermesName;
  $("dashHermesPath").textContent = hermes.hermes_config_path || "未读取 Hermes 私有配置";

  const auraMode = aura.aura_model_mode === "aura_model" ? "直接 Aura LLM" : "Hermes CLI Agent";
  const auraName = aura.aura_model_mode === "aura_model"
    ? [aura.aura_model_provider, aura.aura_model_model].filter(Boolean).join(" / ") || "Aura LLM 未完整配置"
    : hermesName;
  const latency = aura.voice_latency_path || {};
  $("dashAuraMode").textContent = `${auraMode}: ${auraName}`;
  $("dashAuraTts").textContent = [
    latency.llm_label || auraMode,
    latency.tts_label || (aura.tts_enabled ? `TTS: ${aura.tts_provider || "custom"}` : "TTS 未启用"),
  ].filter(Boolean).join(" · ");

  const asrName = aura.asr_enabled ? [aura.asr_provider, aura.asr_model].filter(Boolean).join(" / ") || "ASR 已启用" : "ASR 未启用";
  $("dashAsr").textContent = asrName;
  $("dashAsrPath").textContent = latency.step_plan_realtime_ready
    ? latency.step_plan_summary || "Step Plan Realtime 实验直连已启用"
    : latency.step_plan_realtime_configured
    ? latency.step_plan_summary || "Step Plan Realtime 已配置但未启用直连"
    : latency.xiaozhi_style_ready
    ? "小智式 ASR/LLM/TTS 三段流式已就绪"
    : latency.step_plan_covered
    ? latency.step_plan_summary || "Step Plan ASR/LLM/TTS 已覆盖，走订阅内安全闭环"
    : latency.summary || latency.asr_label || (aura.asr_mode === "api" ? "API / 本机 HTTP" : "本地命令");

  $("dashPersona").textContent = persona.enabled ? "已启用" : "未启用";
  $("dashPersonaScope").textContent = [persona.platform, persona.chat_id, persona.user_id].filter(Boolean).join(" / ");

  const locationLabel = locationSummaryLabel(location);
  $("dashLocation").textContent = locationLabel.title;
  $("dashLocationDetail").textContent = locationLabel.detail;

  const editable = state.state || {};
  $("dashRelationship").textContent = `Trust ${editable.trust ?? "-"} · Mood ${editable.mood ?? "-"}`;
  $("dashActivity").textContent = editable.current_activity || "未读取当前活动";

  const current = world.current || {};
  $("dashWorld").textContent = world.enabled ? `${world.city || persona.aura_home_city || "未设置"} · 已启用` : "未启用";
  $("dashWorldDetail").textContent = current.activity_label
    ? `${current.activity_label}${current.location_label ? ` @ ${current.location_label}` : ""}`
    : "未读取世界状态";
}

function fillHermes(config) {
  providerPresets = config.provider_presets || [];
  hermesModelProfiles = config.model_profiles || [];
  val("provider", config.provider);
  val("model", config.model);
  val("baseUrl", config.base_url);
  maskInput("apiKey", config.api_key_configured, "粘贴上游 API Key");
  revealed.hermes = false;
  $("showHermesKey").textContent = "查看";
  val("toolsets", csv(config.toolsets));
  val("timeout", config.timeout_seconds || "");
  val("hermesProfileLabel", "");
  $("hermesPath").textContent = config.hermes_config_path || "未配置 Hermes home";
  populateProviderPresetSelect(config);
  populateModelProfiles("hermesModelProfile", hermesModelProfiles);
  populateAuraPresetSelect(lastSummary.aura_runtime || {});
  refreshPresetModels(false);
  refreshAuraPresetModels(false);

  setBadge("hermesKeyBadge", config.api_key_configured ? "Hermes API Key 已保存" : "Hermes API Key 未保存", config.api_key_configured ? "ok" : "warn");
  setBadge("apiKeyBadge", config.api_key_configured ? "已保存" : "未保存", config.api_key_configured ? "ok" : "warn");
}

function fillAuraRuntime(config) {
  ttsPresets = config.tts_provider_presets || [];
  asrPresets = config.asr_provider_presets || [];
  ttsProfiles = config.tts_profiles || [];
  asrProfiles = config.asr_profiles || [];
  auraModelProfiles = config.model_profiles || [];
  val("auraModelMode", config.aura_model_mode || "hermes_main");
  val("auraModelProvider", config.aura_model_provider);
  val("auraModelModel", config.aura_model_model);
  val("auraModelBaseUrl", config.aura_model_base_url);
  val("auraModelTimeout", config.aura_model_timeout_seconds || 90);
  val("auraModelMaxTokens", config.aura_model_max_tokens || 96);
  val("auraModelTemperature", config.aura_model_temperature || "0.4");
  val("auraModelReasoningEffort", config.aura_model_reasoning_effort || "");
  val("auraModelProfileLabel", "");
  maskInput("auraModelApiKey", config.aura_model_api_key_configured, "粘贴 Aura LLM API Key");
  revealed.auraModel = false;
  $("showAuraModelKey").textContent = "查看";

  bool("fastReplyEnabled", config.fast_reply_enabled);
  val("fastReplyMode", config.fast_reply_mode || "hermes_main");
  bool("voiceTurnEnabled", config.voice_turn_enabled);
  bool("ackAndEnqueueEnabled", config.ack_and_enqueue_enabled);
  val("greetingReply", config.greeting_reply);
  val("clarifyReply", config.clarify_reply);
  val("refuseReply", config.refuse_reply);
  val("backgroundAckReply", config.background_ack_reply);
  val("fastReplyProvider", config.fast_reply_provider);
  val("fastReplyModel", config.fast_reply_model);
  val("fastReplyBaseUrl", config.fast_reply_base_url);
  maskInput("fastReplyApiKey", config.fast_reply_api_key_configured, "旧快答 API Key");
  revealed.fastReply = false;
  $("showFastReplyKey").textContent = "查看";
  val("fastReplyTimeout", config.fast_reply_timeout_seconds || 8);
  bool("cachedWeatherEnabled", config.cached_weather_enabled);
  val("cachedWeatherCity", config.cached_weather_city);
  val("cachedWeatherTemperature", config.cached_weather_temperature);
  val("cachedWeatherCondition", config.cached_weather_condition);
  val("cachedWeatherIcon", String(config.cached_weather_icon ?? 0));
  val("cachedWeatherHumidity", config.cached_weather_humidity);
  val("cachedWeatherSource", config.cached_weather_source);
  val("cachedWeatherObservedAt", config.cached_weather_observed_at);
  val("cachedWeatherTtl", config.cached_weather_ttl_seconds || 3600);
  bool("weatherAutoRefreshEnabled", config.weather_auto_refresh_enabled);
  val("weatherProvider", config.weather_provider || "open_meteo");
  val("weatherRefreshInterval", config.weather_refresh_interval_seconds || 1800);
  val("weatherRequestTimeout", config.weather_request_timeout_seconds || 8);
  val("weatherLatitude", config.weather_latitude);
  val("weatherLongitude", config.weather_longitude);
  fillCachedWeather(config.cached_weather || {}, config);

  bool("ttsEnabled", config.tts_enabled);
  val("ttsProvider", config.tts_provider || "none");
  val("ttsModel", config.tts_model);
  val("ttsVoice", config.tts_voice);
  val("ttsBaseUrl", config.tts_base_url);
  maskInput("ttsApiKey", config.tts_api_key_configured, "粘贴 TTS API Key");
  revealed.tts = false;
  $("showTtsKey").textContent = "查看";
  val("ttsFormat", config.tts_format || "pcm");
  val("ttsSampleRate", config.tts_sample_rate || 24000);
  val("ttsTimeout", config.tts_timeout_seconds || 15);
  val("ttsProfileName", "");
  bool("asrEnabled", config.asr_enabled);
  val("asrMode", config.asr_mode || "api");
  val("asrProvider", config.asr_provider || "custom");
  val("asrModel", config.asr_model || "whisper-base-local");
  val("asrBaseUrl", config.asr_base_url);
  maskInput("asrApiKey", config.asr_api_key_configured, "粘贴 ASR API Key");
  revealed.asr = false;
  $("showAsrKey").textContent = "查看";
  val("asrLanguage", config.asr_language || "zh");
  val("asrTimeout", config.asr_timeout_seconds || 30);
  val("asrProfileName", "");
  $("auraRuntimePath").textContent = config.runtime_config_path || "未配置 Aura runtime JSON";

  populateAuraPresetSelect(config);
  populateModelProfiles("auraModelProfile", auraModelProfiles);
  populateTtsPresetSelect(config);
  populateAsrPresetSelect(config);
  populateProfileSelect("tts", ttsProfiles);
  populateProfileSelect("asr", asrProfiles);
  populateRuntimeHistory(config.config_history || []);
  refreshAuraPresetModels(false);
  refreshTtsPreset(false);
  refreshAsrPreset(false);
  updateAuraModelVisibility();
  updateAsrModeVisibility();

  const runtimeText = config.aura_model_mode === "aura_model" ? "直接 Aura LLM" : "Hermes CLI Agent";
  setBadge("auraRuntimeBadge", runtimeText, "ok");
  setBadge("audioRuntimeBadge", config.asr_enabled || config.tts_enabled ? "语音配置已读取" : "语音未启用", config.asr_enabled || config.tts_enabled ? "ok" : "warn");
  setBadge("auraModelKeyBadge", config.aura_model_api_key_configured ? "已保存" : "未保存", config.aura_model_api_key_configured ? "ok" : "warn");
  setBadge("fastReplyKeyBadge", config.fast_reply_api_key_configured ? "已保存" : "未保存", config.fast_reply_api_key_configured ? "ok" : "muted");
  setBadge("ttsKeyBadge", config.tts_api_key_configured ? "已保存" : "未保存", config.tts_api_key_configured ? "ok" : "warn");
  setBadge("asrKeyBadge", config.asr_api_key_configured ? "已保存" : "未保存", config.asr_api_key_configured ? "ok" : "muted");
}

function fillCachedWeather(weather, config = {}) {
  const status = weather.status || "empty";
  const labelMap = {
    fresh: "缓存新鲜",
    stale: "缓存过期",
    empty: "未填写",
    disabled: "已关闭",
    error: "刷新失败",
  };
  const toneMap = {
    fresh: "ok",
    stale: "warn",
    empty: "muted",
    disabled: "muted",
    error: "bad",
  };
  setBadge("cachedWeatherBadge", labelMap[status] || status, toneMap[status] || "muted");
  const parts = [];
  if (weather.display) parts.push(weather.display);
  if (weather.source) parts.push(`来源 ${weather.source}`);
  if (weather.observed_at) parts.push(`观测 ${weather.observed_at}`);
  if (Number.isFinite(Number(weather.age_seconds))) parts.push(`${weather.age_seconds}s 前更新`);
  if (weather.ttl_seconds) parts.push(`有效期 ${weather.ttl_seconds}s`);
  if (config.weather_last_error) parts.push(`上次错误：${config.weather_last_error}`);
  $("cachedWeatherStatus").textContent = parts.join(" · ") || "天气缓存会随 Aura runtime 一起保存。";
}

function locationSummaryLabel(location) {
  const status = location.status || "unknown";
  const geo = location.effective_geo || {};
  const gateway = location.gateway_status || {};
  const place = [geo.city, geo.region, geo.country].filter(Boolean).join(" / ");
  const modeMap = {
    manual: "手动位置生效",
    manual_missing: "手动位置未配置",
    auto_ready: "ESP32 自动定位已就绪",
    auto_waiting: "等待 ESP32 公网 IP",
    disabled: "定位已关闭",
  };
  const title = modeMap[status] || "未读取";
  const detailParts = [];
  if (place) detailParts.push(place);
  if (geo.timezone) detailParts.push(geo.timezone);
  if (gateway.device_public_ip_configured) detailParts.push(`设备 ${gateway.device_public_ip}`);
  if (!detailParts.length && gateway.available) detailParts.push(`最近 ${gateway.source_event || "gateway"}，未上报公网 IP`);
  return { title, detail: detailParts.join(" · ") || "未读取位置诊断" };
}

function fillLocation(location = {}) {
  const status = location.status || "unknown";
  const gateway = location.gateway_status || {};
  const geo = location.effective_geo || {};
  const label = locationSummaryLabel(location);
  const toneMap = {
    manual: "ok",
    auto_ready: "ok",
    manual_missing: "warn",
    auto_waiting: "warn",
    disabled: "muted",
  };
  setBadge("locationBadge", label.title, toneMap[status] || "muted");
  $("locationModeText").textContent = [
    `配置：${location.mode || "device_ip"}`,
    location.manual_configured ? "手动已填" : "手动未填",
    location.auto_enabled ? "自动开启" : "自动关闭",
  ].join(" · ");
  $("locationEffective").textContent = [
    geo.city || geo.region || geo.country || "",
    geo.timezone || "",
    geo.latitude && geo.longitude ? `${geo.latitude}, ${geo.longitude}` : "",
    geo.source ? `source=${geo.source}` : "",
  ].filter(Boolean).join(" · ") || "无";
  $("locationDeviceIp").textContent = gateway.device_public_ip_configured
    ? `${gateway.device_public_ip}${gateway.age_seconds != null ? ` · ${gateway.age_seconds}s 前` : ""}`
    : "未上报";
  $("locationClientIp").textContent = gateway.client_ip
    ? `${gateway.client_ip}${gateway.client_ip_private ? " · private/ignored" : ""}`
    : "未记录";

  const adviceMap = {
    manual: "当前使用后台手动用户位置；“现在几点/现在多少度”会按这个位置回答。",
    manual_missing: "当前选择手动定位，但还没填写城市、时区或经纬度。请在上方补齐后保存。",
    auto_ready: "ESP32 已上报公网 IP，后台可以据此推断用户位置；Docker/private IP 不参与定位。",
    auto_waiting: "还没收到 ESP32 公网 IP。需要刷入开启公网 IP 查询的固件并让设备重新连接，或改用手动位置。",
    disabled: "用户自动定位已关闭；涉及用户所在地的问题可能无法回答实时位置。",
  };
  $("locationAdvice").textContent = adviceMap[status] || "读取后台摘要后显示定位状态。";
}

function fillPersonaConfig(config) {
  bool("personaEnabled", config.enabled);
  val("auraCity", config.aura_home_city);
  val("userLocationMode", config.user_location_mode || "device_ip");
  val("userCity", config.user_home_city);
  val("userTimezone", config.user_timezone);
  val("userLatitude", config.user_latitude);
  val("userLongitude", config.user_longitude);
  bool("includeState", config.include_state);
  bool("worldModelEnabled", config.world_model_enabled);
  bool("includeMoment", config.include_latest_moment);
  bool("includeMessages", config.include_recent_messages);
  bool("includePlan", config.include_today_plan);
  bool("proactiveEnabled", config.proactive_enabled);
  bool("debugEnabled", config.debug_enabled);
  bool("spendEnabled", config.spend_enabled);
  setBadge("soulBadge", config.enabled ? "人格网关已启用" : "人格网关未启用", config.enabled ? "ok" : "warn");
}

function fillSoul(payload) {
  val("soul", payload.soul || "");
  $("soulPath").textContent = `读取：${payload.source_path || "无"}；保存：${payload.editable_path || ""}`;
  setStatus("soulStatus", payload.available ? "已读取主 Soul。" : "还没有可用 Soul。", payload.available);
}

function fillState(payload) {
  const state = payload.state || {};
  val("mood", state.mood);
  val("energy", state.energy);
  val("satiety", state.satiety);
  val("trust", state.trust);
  val("stress", state.stress);
  val("affinityXp", state.affinity_xp);
  val("beans", state.beans);
  val("scene", state.scene);
  val("outfit", state.outfit);
  val("socialNeed", state.social_need);
  val("curiosity", state.curiosity);
  val("privacySensitivity", state.privacy_sensitivity);
  val("activity", state.current_activity);
  val("location", state.current_location);
  val("locationLabel", state.location_label);
  bool("strained", state.relationship_strained);
  $("stateSummary").textContent = JSON.stringify(payload.summary || {}, null, 2);
  setStatus("stateStatus", "已读取关系状态。", true);
  setBadge("stateBadge", "状态已读取", "ok");
}

function fillWorld(payload) {
  const world = payload.world || {};
  const current = world.current || {};
  const policy = world.mention_policy || {};
  const plan = Array.isArray(world.today_plan) ? world.today_plan : [];
  setBadge("worldBadge", world.enabled ? "世界模型已启用" : "世界模型已关闭", world.enabled ? "ok" : "warn");
  $("worldCurrent").textContent = world.enabled
    ? [
        `城市：${world.city || "未设置"}`,
        `当前：${current.activity_label || "未知"}${current.location_label ? ` @ ${current.location_label}` : ""}`,
        `来源：${current.source || world.debug?.current_source || "未知"}`,
      ].join(" · ")
    : "世界模型已关闭。";
  $("worldPolicy").textContent = [
    `location=${Boolean(policy.allow_location)}`,
    `activity=${Boolean(policy.allow_activity)}`,
    `plan=${Boolean(policy.allow_plan)}`,
    `precision=${policy.location_precision || "none"}`,
    `reason=${policy.reason || ""}`,
  ].join(" · ");
  const list = $("worldPlan");
  list.replaceChildren();
  if (!plan.length) {
    const empty = document.createElement("div");
    empty.className = "mini";
    empty.textContent = world.enabled ? "今天还没有生成计划。" : "关闭后不生成计划。";
    list.appendChild(empty);
  }
  for (const item of plan) {
    const row = document.createElement("div");
    row.className = "world-plan-item";
    const time = document.createElement("div");
    time.className = "world-plan-time";
    time.textContent = formatWorldTime(item.scheduled_at);
    const title = document.createElement("div");
    title.className = "world-plan-title";
    title.textContent = item.title || "未命名";
    const meta = document.createElement("div");
    meta.className = "world-plan-meta";
    meta.textContent = [item.status, item.location].filter(Boolean).join(" · ");
    row.append(time, title, meta);
    list.appendChild(row);
  }
  $("worldDebug").textContent = JSON.stringify(world, null, 2);
  setStatus("worldStatus", world.enabled ? "世界状态已刷新。" : "世界模型已关闭。", true);
}

async function loadAll() {
  saveLoginLocally();
  setStatus("globalStatus", "读取中...", true);
  setBadge("connectionBadge", "连接中", "muted");
  const summary = await api("/admin/summary");
  lastSummary = summary;
  fillHermes(summary.hermes || {});
  fillAuraRuntime(summary.aura_runtime || {});
  fillPersonaConfig(summary.persona || {});
  fillLocation(summary.location || {});
  try {
    const soul = await api("/persona/assets");
    fillSoul(soul);
  } catch (err) {
    setStatus("soulStatus", err.message, false);
  }
  try {
    const state = await api("/persona/state");
    summary.state = state;
    fillState(state);
  } catch (err) {
    setStatus("stateStatus", err.message, false);
  }
  try {
    const world = await api("/persona/world");
    summary.world = world;
    fillWorld(world);
  } catch (err) {
    setStatus("worldStatus", err.message, false);
  }
  await refreshQuotas();
  fillDashboard(summary);
  setStatus("globalStatus", "已登录。", true);
  setBadge("connectionBadge", "已连接", "ok");
}

async function saveHermes() {
  const payload = await api("/admin/hermes/config", {
    method: "POST",
    body: JSON.stringify({
      provider: $("provider").value.trim(),
      model: $("model").value.trim(),
      api_key: $("apiKey").value.trim(),
      base_url: $("baseUrl").value.trim(),
      profile_label: $("hermesProfileLabel").value.trim(),
      toolsets: $("toolsets").value,
      timeout_seconds: num("timeout"),
    }),
  });
  fillHermes(payload.config || {});
  lastSummary.hermes = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("hermesStatus", "Hermes 上游配置已保存。", true);
}

async function testHermes() {
  await saveHermes();
  setTestResult("hermesTestResult", { detail: "测试中..." }, true);
  const payload = await testApi("/admin/test/hermes");
  setTestResult("hermesTestResult", payload, payload.ok);
  setStatus("hermesStatus", payload.ok ? "Hermes 模型测试通过。" : "Hermes 模型测试失败。", payload.ok);
}

async function clearHermesKey() {
  const payload = await api("/admin/hermes/config", {
    method: "POST",
    body: JSON.stringify({
      provider: $("provider").value.trim(),
      model: $("model").value.trim(),
      base_url: $("baseUrl").value.trim(),
      toolsets: $("toolsets").value,
      timeout_seconds: num("timeout"),
      clear_api_key: true,
    }),
  });
  fillHermes(payload.config || {});
  lastSummary.hermes = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("hermesStatus", "Hermes API Key 已清除。", true);
}

function auraRuntimePayload(extra = {}) {
  return {
    aura_model_mode: $("auraModelMode").value,
    aura_model_provider: $("auraModelProvider").value.trim(),
    aura_model_model: $("auraModelModel").value.trim(),
      aura_model_base_url: $("auraModelBaseUrl").value.trim(),
      profile_label: $("auraModelProfileLabel").value.trim(),
    aura_model_api_key: $("auraModelApiKey").value.trim(),
    aura_model_timeout_seconds: num("auraModelTimeout"),
    aura_model_max_tokens: intOr("auraModelMaxTokens", 96),
    aura_model_temperature: $("auraModelTemperature").value.trim(),
    aura_model_reasoning_effort: $("auraModelReasoningEffort").value,
    fast_reply_enabled: $("fastReplyEnabled").checked,
    fast_reply_mode: $("fastReplyMode").value,
    voice_turn_enabled: $("voiceTurnEnabled").checked,
    ack_and_enqueue_enabled: $("ackAndEnqueueEnabled").checked,
    greeting_reply: $("greetingReply").value.trim(),
    clarify_reply: $("clarifyReply").value.trim(),
    refuse_reply: $("refuseReply").value.trim(),
    background_ack_reply: $("backgroundAckReply").value.trim(),
    cached_weather_enabled: $("cachedWeatherEnabled").checked,
    cached_weather_city: $("cachedWeatherCity").value.trim(),
    cached_weather_temperature: $("cachedWeatherTemperature").value.trim(),
    cached_weather_condition: $("cachedWeatherCondition").value.trim(),
    cached_weather_icon: num("cachedWeatherIcon"),
    cached_weather_humidity: $("cachedWeatherHumidity").value.trim(),
    cached_weather_source: $("cachedWeatherSource").value.trim(),
    cached_weather_observed_at: $("cachedWeatherObservedAt").value.trim(),
    cached_weather_ttl_seconds: intOr("cachedWeatherTtl", 3600),
    weather_provider: $("weatherProvider").value.trim(),
    weather_auto_refresh_enabled: $("weatherAutoRefreshEnabled").checked,
    weather_refresh_interval_seconds: intOr("weatherRefreshInterval", 1800),
    weather_request_timeout_seconds: intOr("weatherRequestTimeout", 8),
    weather_latitude: $("weatherLatitude").value.trim(),
    weather_longitude: $("weatherLongitude").value.trim(),
    fast_reply_provider: $("fastReplyProvider").value.trim(),
    fast_reply_model: $("fastReplyModel").value.trim(),
    fast_reply_base_url: $("fastReplyBaseUrl").value.trim(),
    fast_reply_api_key: $("fastReplyApiKey").value.trim(),
    fast_reply_timeout_seconds: num("fastReplyTimeout"),
    tts_enabled: $("ttsEnabled").checked,
    tts_provider: $("ttsProvider").value.trim(),
    tts_model: $("ttsModel").value.trim(),
    tts_voice: $("ttsVoice").value.trim(),
    tts_base_url: $("ttsBaseUrl").value.trim(),
    tts_api_key: $("ttsApiKey").value.trim(),
    tts_format: $("ttsFormat").value.trim(),
    tts_sample_rate: num("ttsSampleRate"),
    tts_timeout_seconds: num("ttsTimeout"),
    asr_enabled: $("asrEnabled").checked,
    asr_mode: $("asrMode").value,
    asr_provider: $("asrProvider").value.trim(),
    asr_model: $("asrModel").value.trim(),
    asr_base_url: $("asrBaseUrl").value.trim(),
    asr_api_key: $("asrApiKey").value.trim(),
    asr_language: $("asrLanguage").value.trim(),
    asr_timeout_seconds: num("asrTimeout"),
    ...extra,
  };
}

async function saveAuraRuntime() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload()),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "Aura 运行配置已保存。", true);
}

async function testAuraModel() {
  await saveAuraRuntime();
  setTestResult("auraModelTestResult", { detail: "测试中..." }, true);
  const payload = await testApi("/admin/test/aura-model");
  setTestResult("auraModelTestResult", payload, payload.ok);
  setStatus("auraRuntimeStatus", payload.ok ? "Aura LLM 测试通过。" : "Aura LLM 测试失败。", payload.ok);
}

async function testTts() {
  await saveAuraRuntime();
  setTestResult("ttsTestResult", { detail: "测试中..." }, true);
  const payload = await testApi("/admin/test/tts");
  setTestResult("ttsTestResult", payload, payload.ok);
  setStatus("auraRuntimeStatus", payload.ok ? "TTS 测试通过。" : "TTS 测试失败。", payload.ok);
}

async function testAsr() {
  await saveAuraRuntime();
  setTestResult("asrTestResult", { detail: "测试中..." }, true);
  const payload = await testApi("/admin/test/asr");
  setTestResult("asrTestResult", payload, payload.ok);
  setStatus("auraRuntimeStatus", payload.ok ? "ASR 测试通过。" : "ASR 测试失败。", payload.ok);
}

async function applyStepPlanAsrPreset() {
  const preset = asrPresets.find((item) => item.id === "stepfun-step-plan");
  if (!preset) throw new Error("未找到 StepFun Step Plan ASR preset。");
  val("asrPreset", preset.id);
  bool("asrEnabled", true);
  val("asrMode", "api");
  val("asrProvider", preset.provider || "stepfun");
  val("asrBaseUrl", preset.base_url || "https://api.stepfun.com/step_plan/v1");
  const model = Array.isArray(preset.models) && preset.models.length ? preset.models[0] : "stepaudio-2.5-asr";
  val("asrModel", model);
  if (!$("asrLanguage").value.trim()) val("asrLanguage", "zh");
  refreshAsrPreset(false);
  updateAsrModeVisibility();
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload()),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const keyConfigured = Boolean(payload.config?.asr_api_key_configured);
  setStatus(
    "auraRuntimeStatus",
    keyConfigured
      ? "已套用 Step Plan ASR 兜底。可以点测试 ASR 验证 /audio/asr/sse。"
      : "已套用 Step Plan ASR 兜底字段；还需要填写 ASR API Key，或点“复用已保存 StepFun Key 到 ASR”。",
    keyConfigured,
  );
}

async function applyStepPlanRealtimePreset() {
  const preset = asrPresets.find((item) => item.id === "stepfun-step-plan-realtime");
  if (!preset) throw new Error("未找到 StepFun Step Plan Realtime preset。");
  val("asrPreset", preset.id);
  bool("asrEnabled", true);
  val("asrMode", "api");
  val("asrProvider", preset.provider || "stepfun-realtime");
  val("asrBaseUrl", preset.base_url || "https://api.stepfun.com/step_plan/v1");
  const model = Array.isArray(preset.models) && preset.models.length ? preset.models[0] : "stepaudio-2.5-realtime";
  val("asrModel", model);
  if (!$("asrLanguage").value.trim()) val("asrLanguage", "zh");
  refreshAsrPreset(false);
  updateAsrModeVisibility();
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload()),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const keyConfigured = Boolean(payload.config?.asr_api_key_configured);
  setStatus(
    "auraRuntimeStatus",
    keyConfigured
      ? "已套用实验 Step Plan Realtime。默认仍不会绕过 Aura/Lily；只有服务端设置 AURA_STEPFUN_REALTIME_DIRECT_REPLY_ENABLED=1 才会作为直连真机链路。"
      : "已套用实验 Step Plan Realtime 字段；还需要填写 ASR Realtime API Key，或点“复用已保存 StepFun Key 到 ASR”。生产默认建议用 Plan ASR。",
    keyConfigured,
  );
}

async function applyXiaozhiAsrPreset() {
  const preset = asrPresets.find((item) => item.id === "stepfun-stream");
  if (!preset) throw new Error("未找到 StepFun 实时 ASR preset。");
  val("asrPreset", preset.id);
  bool("asrEnabled", true);
  val("asrMode", "api");
  val("asrProvider", preset.provider || "stepfun");
  val("asrBaseUrl", preset.base_url || "https://api.stepfun.com/v1");
  const model = Array.isArray(preset.models) && preset.models.length ? preset.models[0] : "stepaudio-2.5-asr-stream";
  val("asrModel", model);
  if (!$("asrLanguage").value.trim()) val("asrLanguage", "zh");
  refreshAsrPreset(false);
  updateAsrModeVisibility();
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload()),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const keyConfigured = Boolean(payload.config?.asr_api_key_configured);
  setStatus(
    "auraRuntimeStatus",
    keyConfigured
      ? "已套用小智式语义流式：实时 ASR 只做听写，回复仍进入 Aura/Lily，再由 StepFun TTS 流式播放。"
      : "已套用小智式语义流式字段；需要填写 ASR API Key，或点“复用已保存 StepFun Key 到 ASR”。",
    keyConfigured,
  );
}

async function applyStepfunOpenPlatformPreset() {
  const payload = await api("/admin/aura/apply-stepfun-open-platform");
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const source = payload.source ? `（复用 Key 来源：${payload.source}）` : "";
  setStatus(
    "auraRuntimeStatus",
    `已套用 StepFun Open Platform 小智式语义流式${source}。这是非 Step Plan 路由，会走 /v1 ASR/LLM/TTS；可以分别测试 Aura LLM、ASR、TTS。`,
    true,
  );
}

async function clearAuraModelKey() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload({ clear_aura_model_api_key: true })),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "Aura LLM API Key 已清除。", true);
}

async function clearFastReplyKey() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify({ clear_fast_reply_api_key: true }),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "旧快答 API Key 已清除。", true);
}

async function touchCachedWeather() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload({ touch_cached_weather: true })),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "天气缓存时间已刷新。", true);
}

async function refreshCachedWeather() {
  await saveAuraRuntime();
  setStatus("auraRuntimeStatus", "正在联网刷新 Aura 所在地天气...", true);
  const payload = await api("/admin/aura/weather/refresh", {
    method: "POST",
    body: JSON.stringify({
      city: $("cachedWeatherCity").value.trim(),
      force: true,
    }),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const weather = payload.result?.weather || payload.config?.cached_weather || {};
  const detail = weather.display ? `：${weather.display}` : "";
  setStatus("auraRuntimeStatus", payload.ok ? `实时天气已刷新${detail}` : "实时天气刷新失败。", payload.ok);
}

async function clearCachedWeather() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload({ clear_cached_weather: true })),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "天气缓存已清空。", true);
}

async function clearTtsKey() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload({ clear_tts_api_key: true })),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "TTS API Key 已清除。", true);
}

async function clearAsrKey() {
  const payload = await api("/admin/aura/runtime", {
    method: "POST",
    body: JSON.stringify(auraRuntimePayload({ clear_asr_api_key: true })),
  });
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  setStatus("auraRuntimeStatus", "ASR API Key 已清除。", true);
}

async function copyStepPlanKeyToAsr() {
  const payload = await api("/admin/aura/copy-stepfun-plan-key");
  fillAuraRuntime(payload.config || {});
  lastSummary.aura_runtime = payload.config || {};
  fillDashboard(lastSummary);
  const source = payload.source ? `（来源：${payload.source}）` : "";
  setStatus("auraRuntimeStatus", `已把保存的 StepFun Plan Key 写入 ASR${source}，默认套用 Step Plan ASR SSE；可以点测试 ASR 验证。`, true);
}

async function revealSecret(kind) {
  const config = lastSummary.aura_runtime || {};
  const hermesConfig = lastSummary.hermes || {};
  const map = {
    hermes: {
      input: "apiKey",
      button: "showHermesKey",
      badge: "apiKeyBadge",
      path: "/admin/hermes/secret/api_key",
      configured: hermesConfig.api_key_configured,
    },
    auraModel: {
      input: "auraModelApiKey",
      button: "showAuraModelKey",
      badge: "auraModelKeyBadge",
      path: "/admin/aura/secret/aura_model_api_key",
      configured: config.aura_model_api_key_configured,
    },
    fastReply: {
      input: "fastReplyApiKey",
      button: "showFastReplyKey",
      badge: "fastReplyKeyBadge",
      path: "/admin/aura/secret/fast_reply_api_key",
      configured: config.fast_reply_api_key_configured,
    },
    tts: {
      input: "ttsApiKey",
      button: "showTtsKey",
      badge: "ttsKeyBadge",
      path: "/admin/aura/secret/tts_api_key",
      configured: config.tts_api_key_configured,
    },
    asr: {
      input: "asrApiKey",
      button: "showAsrKey",
      badge: "asrKeyBadge",
      path: "/admin/aura/secret/asr_api_key",
      configured: config.asr_api_key_configured,
    },
  };
  const item = map[kind];
  if (!item) return;
  const input = $(item.input);
  const button = $(item.button);
  if (revealed[kind]) {
    input.type = "password";
    input.value = "";
    input.placeholder = item.configured ? "•••••••• 已保存；留空则不修改" : "未保存";
    button.textContent = "查看";
    revealed[kind] = false;
    return;
  }
  if (!item.configured) {
    setBadge(item.badge, "未保存", "warn");
    return;
  }
  const payload = await api(item.path);
  input.type = "text";
  input.value = payload.value || "";
  button.textContent = "隐藏";
  revealed[kind] = true;
}

function populateRuntimeHistory(history) {
  const select = $("runtimeHistory");
  if (!select) return;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = history.length ? "选择一个已保存配置" : "暂无历史配置";
  select.appendChild(empty);
  history.forEach((item, index) => {
    const opt = document.createElement("option");
    opt.value = String(index);
    opt.textContent = historyLabel(item);
    select.appendChild(opt);
  });
  $("historyPreview").textContent = "";
}

function historyLabel(item) {
  const kind = item.kind === "llm" ? "LLM" : item.kind === "tts" ? "TTS" : "ASR";
  return `${kind} · ${[item.provider, item.model, item.voice].filter(Boolean).join(" / ")}`;
}

function selectedHistoryItem() {
  const config = lastSummary.aura_runtime || {};
  const history = config.config_history || [];
  const index = Number($("runtimeHistory").value);
  return Number.isInteger(index) ? history[index] : null;
}

function previewHistory() {
  const item = selectedHistoryItem();
  $("historyPreview").textContent = item ? JSON.stringify(item, null, 2) : "";
}

function applyHistory() {
  const item = selectedHistoryItem();
  if (!item) return;
  if (item.kind === "llm") {
    val("auraModelMode", item.mode || "aura_model");
    val("auraModelProvider", item.provider);
    val("auraModelModel", item.model);
    val("auraModelBaseUrl", item.base_url);
    if (item.timeout_seconds) val("auraModelTimeout", item.timeout_seconds);
    updateAuraModelVisibility();
  }
  if (item.kind === "tts") {
    val("ttsProvider", item.provider);
    val("ttsModel", item.model);
    val("ttsBaseUrl", item.base_url);
    val("ttsVoice", item.voice);
    if (item.audio_format) val("ttsFormat", item.audio_format);
    if (item.sample_rate) val("ttsSampleRate", item.sample_rate);
    if (item.timeout_seconds) val("ttsTimeout", item.timeout_seconds);
  }
  if (item.kind === "asr") {
    val("asrMode", item.mode || "api");
    val("asrProvider", item.provider);
    val("asrModel", item.model);
    val("asrBaseUrl", item.base_url);
    val("asrLanguage", item.language || "zh");
    if (item.timeout_seconds) val("asrTimeout", item.timeout_seconds);
    updateAsrModeVisibility();
  }
  setStatus("auraRuntimeStatus", "历史配置已套用到表单，保存后生效。", true);
}

async function savePersonaConfig() {
  const payload = await api("/persona/config", {
    method: "POST",
    body: JSON.stringify({
      enabled: $("personaEnabled").checked,
      aura_home_city: $("auraCity").value.trim(),
      user_location_mode: $("userLocationMode").value,
      user_home_city: $("userCity").value.trim(),
      user_timezone: $("userTimezone").value.trim(),
      user_latitude: $("userLatitude").value.trim(),
      user_longitude: $("userLongitude").value.trim(),
      include_state: $("includeState").checked,
      world_model_enabled: $("worldModelEnabled").checked,
      include_latest_moment: $("includeMoment").checked,
      include_recent_messages: $("includeMessages").checked,
      include_today_plan: $("includePlan").checked,
      proactive_enabled: $("proactiveEnabled").checked,
      spend_enabled: $("spendEnabled").checked,
      debug_enabled: $("debugEnabled").checked,
      include_debug_context: $("debugEnabled").checked,
    }),
  });
  fillPersonaConfig(payload.config || {});
  lastSummary.persona = payload.config || {};
  const summary = await api("/admin/summary");
  lastSummary.location = summary.location || {};
  fillLocation(lastSummary.location);
  fillDashboard(lastSummary);
  setStatus("personaConfigStatus", "人格配置已保存。", true);
  await refreshWorld();
}

async function saveSoul() {
  const payload = await api("/persona/assets", {
    method: "POST",
    body: JSON.stringify({ soul: $("soul").value }),
  });
  fillSoul(payload);
  setStatus("soulStatus", "Soul 已保存。", true);
}

async function saveState() {
  const payload = await api("/persona/state", {
    method: "POST",
    body: JSON.stringify({
      mood: num("mood"),
      energy: num("energy"),
      satiety: num("satiety"),
      trust: num("trust"),
      stress: num("stress"),
      affinity_xp: num("affinityXp"),
      beans: num("beans"),
      scene: $("scene").value.trim(),
      outfit: $("outfit").value.trim(),
      social_need: num("socialNeed"),
      curiosity: num("curiosity"),
      privacy_sensitivity: num("privacySensitivity"),
      current_activity: $("activity").value.trim(),
      current_location: $("location").value.trim(),
      location_label: $("locationLabel").value.trim(),
      relationship_strained: $("strained").checked,
    }),
  });
  fillState(payload);
  lastSummary.state = payload;
  fillDashboard(lastSummary);
  setStatus("stateStatus", "关系状态已保存。", true);
  await refreshWorld();
}

async function refreshWorld() {
  const payload = await api("/persona/world");
  fillWorld(payload);
  lastSummary.world = payload;
  fillDashboard(lastSummary);
}

function setActivePanel(name) {
  const target = name || "dashboard";
  let activePanel = null;
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    const active = panel.dataset.panel === target;
    panel.classList.toggle("active", active);
    if (active) activePanel = panel;
  });
  document.querySelectorAll("[data-panel-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.panelLink === target);
  });
  if (activePanel) {
    requestAnimationFrame(() => {
      const compactLayout = window.matchMedia("(max-width: 980px)").matches;
      if (compactLayout) {
        activePanel.scrollIntoView({ block: "start", inline: "nearest" });
      } else {
        window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      }
    });
  }
}

const LEGACY_PANEL_ALIASES = { aura: "chat-model", world: "state" };

function syncPanelFromHash() {
  const name = (location.hash || "#dashboard").replace("#", "");
  setActivePanel(LEGACY_PANEL_ALIASES[name] || name);
}

function wireButton(id, fn, statusId) {
  $(id).addEventListener("click", async () => {
    const button = $(id);
    button.disabled = true;
    try {
      await fn();
    } catch (err) {
      setStatus(statusId, err.message, false);
      if (id === "load") setBadge("connectionBadge", "连接失败", "bad");
    } finally {
      button.disabled = false;
    }
  });
}

$("providerPreset").addEventListener("change", () => refreshPresetModels(true));
$("auraProviderPreset").addEventListener("change", () => refreshAuraPresetModels(true));
$("hermesModelProfile").addEventListener("change", () => applyHermesModelProfile(profileFromSelect("hermesModelProfile")));
$("auraModelProfile").addEventListener("change", () => applyAuraModelProfile(profileFromSelect("auraModelProfile")));
$("auraModelMode").addEventListener("change", updateAuraModelVisibility);
$("ttsPreset").addEventListener("change", () => refreshTtsPreset(true));
$("asrPreset").addEventListener("change", () => refreshAsrPreset(true));
$("asrMode").addEventListener("change", updateAsrModeVisibility);
$("runtimeHistory").addEventListener("change", previewHistory);
$("ttsProfile").addEventListener("change", () => previewProfile("tts"));
$("asrProfile").addEventListener("change", () => previewProfile("asr"));
$("forget").addEventListener("click", () => {
  sessionStorage.removeItem("aura_lily_admin_password");
  adminPasswordInput.value = "";
  setStatus("globalStatus", "已退出本地后台。", true);
  setBadge("connectionBadge", "未连接", "muted");
});

window.addEventListener("hashchange", syncPanelFromHash);
wireButton("load", loadAll, "globalStatus");
wireButton("saveHermes", saveHermes, "hermesStatus");
wireButton("testHermes", testHermes, "hermesStatus");
wireButton("clearHermesKey", clearHermesKey, "hermesStatus");
wireButton("showHermesKey", () => revealSecret("hermes"), "hermesStatus");
wireButton("saveAuraRuntimeTop", saveAuraRuntime, "auraRuntimeStatus");
wireButton("saveAudioRuntimeTop", saveAuraRuntime, "auraRuntimeStatus");
wireButton("saveAuraRuntime", saveAuraRuntime, "auraRuntimeStatus");
wireButton("testAuraModel", testAuraModel, "auraRuntimeStatus");
wireButton("testTts", testTts, "auraRuntimeStatus");
wireButton("testAsr", testAsr, "auraRuntimeStatus");
wireButton("applyStepPlanRealtime", applyStepPlanRealtimePreset, "auraRuntimeStatus");
wireButton("applyStepPlanAsr", applyStepPlanAsrPreset, "auraRuntimeStatus");
wireButton("applyStepfunOpenPlatform", applyStepfunOpenPlatformPreset, "auraRuntimeStatus");
wireButton("applyXiaozhiAsr", applyXiaozhiAsrPreset, "auraRuntimeStatus");
wireButton("clearAuraModelKey", clearAuraModelKey, "auraRuntimeStatus");
wireButton("clearAuraModelKeyBottom", clearAuraModelKey, "auraRuntimeStatus");
wireButton("showAuraModelKey", () => revealSecret("auraModel"), "auraRuntimeStatus");
wireButton("showFastReplyKey", () => revealSecret("fastReply"), "auraRuntimeStatus");
wireButton("showTtsKey", () => revealSecret("tts"), "auraRuntimeStatus");
wireButton("showAsrKey", () => revealSecret("asr"), "auraRuntimeStatus");
wireButton("clearFastReplyKey", clearFastReplyKey, "auraRuntimeStatus");
wireButton("refreshCachedWeather", refreshCachedWeather, "auraRuntimeStatus");
wireButton("touchCachedWeather", touchCachedWeather, "auraRuntimeStatus");
wireButton("clearCachedWeather", clearCachedWeather, "auraRuntimeStatus");
wireButton("clearTtsKey", clearTtsKey, "auraRuntimeStatus");
wireButton("clearAsrKey", clearAsrKey, "auraRuntimeStatus");
wireButton("copyStepPlanKeyToAsr", copyStepPlanKeyToAsr, "auraRuntimeStatus");
wireButton("applyHistory", applyHistory, "auraRuntimeStatus");
wireButton("applyTtsProfile", () => applyProfile("tts"), "auraRuntimeStatus");
wireButton("saveTtsProfile", () => upsertProfile("tts"), "auraRuntimeStatus");
wireButton("deleteTtsProfile", () => deleteProfile("tts"), "auraRuntimeStatus");
wireButton("applyAsrProfile", () => applyProfile("asr"), "auraRuntimeStatus");
wireButton("saveAsrProfile", () => upsertProfile("asr"), "auraRuntimeStatus");
wireButton("deleteAsrProfile", () => deleteProfile("asr"), "auraRuntimeStatus");
wireButton("savePersonaConfig", savePersonaConfig, "personaConfigStatus");
wireButton("saveSoul", saveSoul, "soulStatus");
wireButton("saveState", saveState, "stateStatus");
wireButton("refreshWorld", refreshWorld, "worldStatus");

syncPanelFromHash();
populateProviderPresetSelect();
populateAuraPresetSelect();
populateTtsPresetSelect();
populateAsrPresetSelect();
updateAuraModelVisibility();
updateAsrModeVisibility();
if (adminPasswordInput.value) {
  loadAll().catch(() => {
    setStatus("globalStatus", "保存的会话密码暂时无法连接。", false);
    setBadge("connectionBadge", "未连接", "warn");
  });
}
