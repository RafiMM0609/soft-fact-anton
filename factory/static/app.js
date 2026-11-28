const TOKEN_KEY = "acp_factory_operator_token";
const THEME_KEY = "acp_factory_theme";
const CLIENT_MODE_KEY = "acp_factory_client_mode";
const LANG_KEY = "acp_factory_lang";
const $ = (id) => document.getElementById(id);

let currentTab = "overview";
let currentDetailTab = "overview";
let selectedProjectId = null;
let selectedJobId = null;
let retryBuildJobId = null;
const buildRoles = ["planner", "worker"];
let currentProjectData = null;

let sseController = null;
let sseConnected = false;
let sseReconnectTimer = null;
let sseRefreshDebounce = null;
let fallbackPollTimer = null;
let lastEventSeq = 0;

let isPollingPaused = false;
let isClientMode = localStorage.getItem(CLIENT_MODE_KEY) === "true";
let lastAdjustPreview = null;

// Cached workspace state
let cachedProjects = [];
let cachedJobs = [];
let cachedEvents = [];
let cachedFiles = [];
let selectedFilePath = null;
let selectedFileContent = "";

let projectFilter = "all";
let jobFilter = "all";
let eventCatFilter = "all";

let uiBranding = {
  brand_name: "acp-factory",
  brand_subtitle: "AUTONOMOUS STUDIO",
  client_label: "",
  accent_color: "#3b82f6",
  default_lang: "id",
};

const DEMO_PRESETS = {
  landing: {
    product: "aurora-enterprise",
    profile: "static",
    brief:
      "Bangun landing page korporat modern untuk perusahaan konsultan AI & Cloud bernama Aurora Digital. Sertakan hero section dengan CTA jadwal demo, grid layanan utama, kalkulator estimasi biaya proyek interaktif, studi kasus klien, dan formulir kontak.",
  },
  saas: {
    product: "pulse-ops-dashboard",
    profile: "static",
    brief:
      "Bangun dashboard analitik operasional SaaS bernama PulseOps untuk memantau SLA layanan, ringkasan KPI bulanan, tabel insiden dengan pencarian & filter status, serta panel detail tiket interaktif.",
  },
  "go-app": {
    product: "ruang-booking-app",
    profile: "go-htmx",
    brief:
      "Bangun aplikasi pemesanan ruang meeting internal menggunakan Go dan HTMX. Fitur: daftar ruangan dengan kapasitas, formulir reservasi baru dengan validasi jadwal, pembatalan reservasi, dan endpoint healthz.",
  },
};

const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => localStorage.setItem(TOKEN_KEY, t.trim());
const clearToken = () => localStorage.removeItem(TOKEN_KEY);

const tr = (key) => (window.t ? window.t(key) : key);
const fmtNum = (n) => Number(n || 0).toLocaleString(window.getFactoryLang() === "en" ? "en-US" : "id-ID");

function fmtCompact(n) {
  const num = Number(n || 0);
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
  if (num >= 10_000) return `${(num / 1_000).toFixed(1)}K`;
  return fmtNum(num);
}

function fmtBytes(bytes) {
  const b = Number(bytes || 0);
  if (!b) return "-";
  if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(2)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

function fmtDuration(start, end) {
  if (!start) return "-";
  const stop = end || Date.now() / 1000;
  const sec = Math.max(0, Math.round(stop - start));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${s}s`;
}

function fmtRelative(ts) {
  if (!ts) return "";
  const isEn = window.getFactoryLang() === "en";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return isEn ? "just now" : "baru saja";
  if (diff < 3600) return isEn ? `${Math.floor(diff / 60)}m ago` : `${Math.floor(diff / 60)} mnt lalu`;
  if (diff < 86400) return isEn ? `${Math.floor(diff / 3600)}h ago` : `${Math.floor(diff / 3600)} jam lalu`;
  return new Date(ts * 1000).toLocaleDateString(isEn ? "en-US" : "id-ID", { day: "numeric", month: "short" });
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderBadge(state) {
  const st = String(state || "unknown");
  return `<span class="badge ${esc(st)}"><span class="badge-dot"></span>${esc(st)}</span>`;
}

function renderTokenBar(u = {}) {
  const inp = Number(u.input_tokens || 0);
  const out = Number(u.output_tokens || 0);
  const think = Number(u.thinking_tokens || 0);
  const cache = Number(u.cache_read_tokens || 0);
  const sum = inp + out + think + cache;
  if (!sum) return "";
  const pct = (v) => `${Math.max(0, (v / sum) * 100).toFixed(1)}%`;
  return `
    <div class="tok-seg-in" style="width:${pct(inp)}" title="Input: ${fmtNum(inp)}"></div>
    <div class="tok-seg-out" style="width:${pct(out)}" title="Output: ${fmtNum(out)}"></div>
    <div class="tok-seg-think" style="width:${pct(think)}" title="Thinking: ${fmtNum(think)}"></div>
    <div class="tok-seg-cache" style="width:${pct(cache)}" title="Cache Read: ${fmtNum(cache)}"></div>
  `;
}

function showToast(message, type = "info") {
  const container = $("toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.2s";
    setTimeout(() => el.remove(), 220);
  }, 3400);
}

window.copyText = async (text, label = "Teks") => {
  try {
    await navigator.clipboard.writeText(text);
    showToast(`${label} disalin ke clipboard`, "ok");
  } catch {
    showToast("Gagal menyalin ke clipboard", "err");
  }
};

/* ==========================================================================
   Theme, White-Label Branding, Client Mode & Language
   ========================================================================== */
function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  if (!m) return null;
  return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
}

function applyAccentColor(hex) {
  const rgb = hexToRgb(hex);
  if (!rgb) return;
  const root = document.documentElement;
  const hoverR = Math.min(255, rgb.r + 34);
  const hoverG = Math.min(255, rgb.g + 34);
  const hoverB = Math.min(255, rgb.b + 34);
  root.style.setProperty("--accent", hex);
  root.style.setProperty("--accent-hover", `rgb(${hoverR}, ${hoverG}, ${hoverB})`);
  root.style.setProperty("--accent-soft", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.14)`);
  root.style.setProperty("--accent-border", `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.36)`);
}

function applyBranding(data) {
  uiBranding = Object.assign({}, uiBranding, data || {});
  $("brand-name").textContent = uiBranding.brand_name || "acp-factory";
  $("brand-sub").textContent = uiBranding.brand_subtitle || "AUTONOMOUS STUDIO";
  const pill = $("client-label-pill");
  if (uiBranding.client_label && uiBranding.client_label.trim()) {
    pill.textContent = uiBranding.client_label.trim();
    pill.hidden = false;
  } else {
    pill.hidden = true;
  }
  if (uiBranding.accent_color) applyAccentColor(uiBranding.accent_color);

  if (!localStorage.getItem(LANG_KEY) && uiBranding.default_lang) {
    localStorage.setItem(LANG_KEY, uiBranding.default_lang);
  }
}

async function loadUiBranding() {
  try {
    const res = await fetch("/api/settings/ui");
    if (res.ok) {
      const data = await res.json();
      applyBranding(data);
    }
  } catch {
    // Non-fatal: keep defaults
  }
}

function applyTheme(theme) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem(THEME_KEY, t);
  $("theme-icon-sun").hidden = t !== "dark";
  $("theme-icon-moon").hidden = t !== "light";
}

function applyClientMode(enabled) {
  isClientMode = Boolean(enabled);
  document.documentElement.setAttribute("data-client-mode", isClientMode ? "true" : "false");
  localStorage.setItem(CLIENT_MODE_KEY, isClientMode ? "true" : "false");
  $("client-mode-btn").classList.toggle("active", isClientMode);
  $("client-mode-label").textContent = isClientMode ? tr("top.operator_view") : tr("top.client_view");

  // If client mode hides Event Ledger tab while user is on it, move back to overview
  if (isClientMode && currentTab === "events") {
    setActiveNav("overview");
    showView("overview-view");
  }
  if (isClientMode && currentTab === "detail" && currentDetailTab === "adjust") {
    // Adjust remains available in client view for interactive change requests
  }
}

function applyTranslations() {
  const lang = window.getFactoryLang();
  document.documentElement.lang = lang;
  $("lang-toggle-btn").textContent = lang.toUpperCase();

  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.dataset.i18n;
    if (key) el.textContent = tr(key);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const key = el.dataset.i18nPlaceholder;
    if (key) el.placeholder = tr(key);
  });

  const visBtn = $("toggle-token-vis");
  if (visBtn) {
    visBtn.textContent = $("token-input").type === "password" ? tr("login.show") : tr("login.hide");
  }
  $("client-mode-label").textContent = isClientMode ? tr("top.operator_view") : tr("top.client_view");
  $("poll-label").textContent = isPollingPaused ? tr("top.paused") : tr("top.live");

  setActiveNav(currentTab);
  if (cachedProjects.length) renderProjects();
  if (cachedJobs.length) renderAllJobs();
  if (cachedEvents.length) renderEvents();
  if (currentTab === "detail" && currentProjectData) renderProjectDetail(currentProjectData);
}

applyTheme(localStorage.getItem(THEME_KEY) || "dark");
applyClientMode(isClientMode);

$("theme-toggle-btn").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
});

$("lang-toggle-btn").addEventListener("click", () => {
  const next = window.getFactoryLang() === "id" ? "en" : "id";
  localStorage.setItem(LANG_KEY, next);
  applyTranslations();
});

$("client-mode-btn").addEventListener("click", () => {
  applyClientMode(!isClientMode);
  showToast(isClientMode ? "Client Presentation Mode aktif" : "Operator Mode aktif");
});

/* ==========================================================================
   API & Navigation Helpers
   ========================================================================== */
async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body).detail;
      if (typeof parsed === "string") detail = parsed;
      if (Array.isArray(parsed)) {
        detail = parsed.map(({ loc, msg }) => [loc?.slice(1).join("."), msg].filter(Boolean).join(": ")).join("\n");
      }
    } catch {
      // Preserve plain-text HTTP errors.
    }
    throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
  }
  return res.json();
}

function showView(viewId) {
  for (const id of ["login-view", "overview-view", "detail-view", "jobs-view", "events-view"]) {
    $(id).hidden = id !== viewId;
  }
}

function setActiveNav(tab) {
  currentTab = tab;
  document.querySelectorAll(".nav").forEach((btn) => {
    const isDetailOverview = tab === "detail" && btn.dataset.tab === "overview";
    btn.classList.toggle("active", btn.dataset.tab === tab || isDetailOverview);
  });
  const titles = {
    overview: tr("nav.projects"),
    detail: (currentProjectData && currentProjectData.name) || selectedProjectId || tr("nav.projects"),
    jobs: tr("nav.jobs"),
    events: tr("nav.events"),
  };
  $("crumb-title").textContent = titles[tab] || tr("nav.projects");
}

function setDetailTab(subTab) {
  currentDetailTab = subTab;
  document.querySelectorAll(".detail-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.detailTab === subTab);
  });
  for (const t of ["overview", "jobs", "code", "adjust"]) {
    $(`d-tab-${t}`).hidden = t !== subTab;
  }
  if (subTab === "code" && selectedProjectId) {
    loadProjectFiles(selectedProjectId);
  }
}

/* ==========================================================================
   Real-Time SSE Event Stream
   ========================================================================== */
function disconnectEventStream() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (sseController) {
    sseController.abort();
    sseController = null;
  }
  sseConnected = false;
}

function scheduleLiveRefresh(projectIdFromEvent) {
  if (sseRefreshDebounce) clearTimeout(sseRefreshDebounce);
  sseRefreshDebounce = setTimeout(async () => {
    if (!getToken()) return;
    if (currentTab === "detail" && selectedProjectId && (!projectIdFromEvent || projectIdFromEvent === selectedProjectId)) {
      await loadProjectDetail(selectedProjectId);
    } else {
      await refreshWorkspace(true);
    }
  }, 350);
}

function handleIncomingEvent(evt) {
  if (!evt || typeof evt !== "object") return;
  if (evt.seq && evt.seq > lastEventSeq) lastEventSeq = evt.seq;
  if (!cachedEvents.some((e) => e.seq === evt.seq)) {
    cachedEvents = [evt, ...cachedEvents].slice(0, 80);
    if (currentTab === "events") renderEvents();
  }
  if (evt.kind === "build.succeeded") {
    showToast(`Build selesai: ${evt.project_id}`, "ok");
  } else if (evt.kind === "build.failed") {
    showToast(`Build gagal: ${evt.project_id}`, "err");
  }
  scheduleLiveRefresh(evt.project_id);
}

async function connectEventStream() {
  disconnectEventStream();
  const token = getToken();
  if (!token || isPollingPaused) return;

  sseController = new AbortController();
  try {
    const res = await fetch(`/api/events/stream?after_seq=${lastEventSeq}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: sseController.signal,
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    sseConnected = true;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const lines = block.split("\n");
        let dataLine = "";
        for (const line of lines) {
          if (line.startsWith("data:")) {
            dataLine += line.slice(5).trim();
          }
        }
        if (dataLine) {
          try {
            handleIncomingEvent(JSON.parse(dataLine));
          } catch {
            // ignore malformed SSE chunk
          }
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") return;
  } finally {
    sseConnected = false;
    if (getToken() && !isPollingPaused) {
      sseReconnectTimer = setTimeout(connectEventStream, 3500);
    }
  }
}

/* ==========================================================================
   Health, Auth & Workspace Refresh
   ========================================================================== */
async function checkHealthAndAuth() {
  try {
    const h = await apiFetch("/health");
    $("sidebar-dot").className = h.status === "ok" ? "dot ok" : "dot err";
    $("sidebar-status").textContent = h.status === "ok" ? tr("top.backend_online") : tr("top.backend_offline");
    if (h.version) $("sidebar-version").textContent = `v${h.version}`;
  } catch {
    $("sidebar-dot").className = "dot err";
    $("sidebar-status").textContent = tr("top.backend_offline");
  }

  if (!getToken()) {
    disconnectEventStream();
    $("connection-text").textContent = tr("top.disconnected");
    $("connection-badge").classList.remove("connected");
    $("refresh-btn").hidden = true;
    $("logout-btn").hidden = true;
    $("poll-toggle-btn").hidden = true;
    $("client-mode-btn").hidden = true;
    $("branding-btn").hidden = true;
    $("sidebar-runtime-row").hidden = true;
    showView("login-view");
    return false;
  }

  try {
    const doc = await apiFetch("/api/doctor");
    $("connection-text").textContent = tr("top.connected");
    $("connection-badge").classList.add("connected");
    $("refresh-btn").hidden = false;
    $("logout-btn").hidden = false;
    $("poll-toggle-btn").hidden = false;
    $("client-mode-btn").hidden = false;
    $("branding-btn").hidden = false;
    $("sidebar-runtime-row").hidden = false;
    $("sidebar-harness-pill").textContent = doc.harness || "acp";
    $("sidebar-py-pill").textContent = `Py ${doc.python || "-"}`;
    return true;
  } catch (err) {
    disconnectEventStream();
    $("login-error").textContent = `Token ditolak atau server bermasalah: ${err.message}`;
    clearToken();
    $("connection-badge").classList.remove("connected");
    $("connection-text").textContent = tr("top.disconnected");
    $("refresh-btn").hidden = true;
    $("logout-btn").hidden = true;
    $("poll-toggle-btn").hidden = true;
    $("client-mode-btn").hidden = true;
    $("branding-btn").hidden = true;
    showView("login-view");
    return false;
  }
}

async function refreshWorkspace(quiet = false) {
  const ok = await checkHealthAndAuth();
  if (!ok) return;

  try {
    const [doctor, projects, jobs, events] = await Promise.all([
      apiFetch("/api/doctor"),
      apiFetch("/api/projects"),
      apiFetch("/api/jobs?limit=50"),
      apiFetch("/api/events?limit=80"),
    ]);

    cachedProjects = projects || [];
    cachedJobs = jobs || [];
    cachedEvents = events || [];
    if (cachedEvents.length) {
      lastEventSeq = Math.max(lastEventSeq, Number(cachedEvents[0].seq || 0));
    }

    // Sidebar badges
    $("nav-count-projects").textContent = fmtNum(cachedProjects.length);
    $("nav-count-projects").hidden = cachedProjects.length === 0;

    const activeJobs = cachedJobs.filter((j) => j.state === "running" || j.state === "queued").length;
    const okJobs = cachedJobs.filter((j) => j.state === "succeeded").length;
    const terminalJobs = cachedJobs.filter((j) => ["succeeded", "failed", "cancelled"].includes(j.state)).length;
    $("nav-count-active").textContent = fmtNum(activeJobs);
    $("nav-count-active").hidden = activeJobs === 0;

    // Operator Metrics
    $("m-projects").textContent = fmtNum(cachedProjects.length);
    $("m-harness").textContent = `Harness: ${doctor.harness} (${doctor.status})`;
    populateHarnessSelect(doctor);

    $("m-jobs").textContent = fmtNum(cachedJobs.length);
    $("m-jobs-sub").textContent = `aktif: ${activeJobs} · sukses: ${okJobs}`;

    // Client Metrics
    const builtCount = cachedProjects.filter((p) => p.state === "built").length;
    $("m-releases").textContent = fmtNum(builtCount);
    const rate = terminalJobs ? Math.round((okJobs / terminalJobs) * 100) : builtCount ? 100 : 0;
    $("m-success-rate").textContent = `${rate}%`;

    let totIn = 0, totOut = 0, totThink = 0, totCache = 0, totAll = 0;
    for (const j of cachedJobs) {
      const u = j.token_usage || {};
      totIn += u.input_tokens || 0;
      totOut += u.output_tokens || 0;
      totThink += u.thinking_tokens || 0;
      totCache += u.cache_read_tokens || 0;
      totAll += u.total_tokens || 0;
    }
    $("m-tokens").textContent = fmtCompact(totAll);
    $("m-token-bar").innerHTML = renderTokenBar({
      input_tokens: totIn,
      output_tokens: totOut,
      thinking_tokens: totThink,
      cache_read_tokens: totCache,
    });
    $("m-tokens-sub").textContent = `in ${fmtCompact(totIn)} · out ${fmtCompact(totOut)} · think ${fmtCompact(totThink)}`;
    $("m-cache").textContent = `cache read: ${fmtCompact(totCache)} tok`;

    if (doctor.memory_used_mb != null && doctor.memory_total_mb != null && doctor.memory_total_mb > 0) {
      const pct = Math.min(100, Math.round((doctor.memory_used_mb / doctor.memory_total_mb) * 100));
      $("m-memory").textContent = `${doctor.memory_used_mb} / ${doctor.memory_total_mb} MB`;
      $("m-memory-bar").style.width = `${pct}%`;
    } else {
      $("m-memory").textContent = "N/A";
      $("m-memory-bar").style.width = "0%";
    }

    if (!doctor.harness_ready) {
      $("readiness-notice").hidden = false;
      $("readiness-notice").textContent = `Perhatian: Harness CLI '${doctor.harness}' belum terdeteksi siap pada host ini.`;
    } else {
      $("readiness-notice").hidden = true;
    }

    renderProjects();
    renderAllJobs();
    renderEvents();

    if (currentTab === "overview") showView("overview-view");
    else if (currentTab === "jobs") showView("jobs-view");
    else if (currentTab === "events") showView("events-view");
    else if (currentTab === "detail" && selectedProjectId) {
      showView("detail-view");
      await loadProjectDetail(selectedProjectId);
    }

    if (!sseConnected && !isPollingPaused) {
      connectEventStream();
    }
  } catch (err) {
    if (!quiet) showToast(`Gagal memuat data workspace: ${err.message}`, "err");
  }
}

/* ==========================================================================
   Projects Grid
   ========================================================================== */
function renderProjects() {
  const grid = $("project-grid");
  const q = ($("project-search").value || "").trim().toLowerCase();

  const filtered = cachedProjects.filter((p) => {
    if (projectFilter === "built" && p.state !== "built") return false;
    if (projectFilter === "building" && p.state !== "building") return false;
    if (projectFilter === "other" && (p.state === "built" || p.state === "building")) return false;
    if (q) {
      const hay = `${p.id} ${p.name || ""} ${p.profile || ""} ${p.brief || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (!cachedProjects.length) {
    grid.innerHTML = `
      <div class="card empty-state" style="grid-column:1 / -1;">
        <div class="empty-icon">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
            <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
            <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
            <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
          </svg>
        </div>
        <strong>${esc(tr("ov.empty_title"))}</strong>
        <span class="muted-xs">${esc(tr("ov.empty_desc"))}</span>
      </div>
    `;
    return;
  }

  if (!filtered.length) {
    grid.innerHTML = `
      <div class="card empty-state" style="grid-column:1 / -1;">
        <strong>${esc(tr("ov.no_match"))}</strong>
        <span class="muted-xs">${esc(tr("ov.no_match_desc"))}</span>
      </div>
    `;
    return;
  }

  grid.innerHTML = filtered
    .map((p) => {
      const projJobs = cachedJobs.filter((j) => j.project_id === p.id);
      const tok = projJobs.reduce((acc, j) => acc + ((j.token_usage && j.token_usage.total_tokens) || 0), 0);
      const briefText = p.brief ? esc(p.brief) : "-";
      return `
        <article class="card proj-card" onclick="window.openProject('${esc(p.id)}')">
          <div>
            <div class="proj-top">
              <span class="proj-name">${esc(p.name || p.id)}</span>
              ${renderBadge(p.state)}
            </div>
            <p class="proj-brief">${briefText}</p>
          </div>
          <div class="proj-footer">
            <div class="proj-footer-left">
              <span class="profile-chip">${esc(p.profile || "static")}</span>
              <span>${projJobs.length} job</span>
              ${!isClientMode ? `<span>${fmtCompact(tok)} tok</span>` : ""}
            </div>
            <span>${esc(fmtRelative(p.created))}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

/* ==========================================================================
   Project Detail, Event-Driven Pipeline & Wave DAG
   ========================================================================== */
window.openProject = async (projectId, jobId = null) => {
  const isNew = selectedProjectId !== projectId;
  selectedProjectId = projectId;
  selectedJobId = jobId;
  lastAdjustPreview = null;
  cachedFiles = [];
  selectedFilePath = null;
  $("adj-scope-box").hidden = true;
  $("adj-status").textContent = "";
  if (jobId) setDetailTab("jobs");
  else if (isNew) setDetailTab("overview");
  setActiveNav("detail");
  showView("detail-view");
  await loadProjectDetail(projectId);
};

function computePipelineStages(proj) {
  const jobs = proj.recent_jobs || [];
  const latest = jobs[0];
  const events = proj.recent_events || [];
  const tasks = proj.latest_job_tasks || [];
  const plan = proj.plan && typeof proj.plan === "object" ? proj.plan : null;
  const artifact = proj.artifact && typeof proj.artifact === "object" ? proj.artifact : null;

  // Scope events to the latest job where applicable
  const latestJobId = latest ? latest.id : null;
  const jobEvents = latestJobId
    ? events.filter((e) => !e.job_id || e.job_id === latestJobId)
    : events;
  const hasEvent = (kind) => jobEvents.some((e) => e.kind === kind);

  const isRunning = latest && (latest.state === "running" || latest.state === "queued");
  const isSucceeded = proj.state === "built" || (latest && latest.state === "succeeded");
  const isFailed = latest && latest.state === "failed";
  const errStr = String((latest && latest.error) || "").toLowerCase();

  // Stage 1: Plan
  let s1 = "idle";
  if (plan || hasEvent("plan.created")) s1 = "done";
  else if (isRunning) s1 = "active";
  else if (isFailed) s1 = "failed";

  // Stage 2: Build & Review
  const totalTasks = (plan && Array.isArray(plan.tasks) && plan.tasks.length) || tasks.length;
  const mergedTasks = tasks.filter((t) => t.state === "merged" || t.state === "completed").length;
  const activeTask = tasks.some((t) => ["running", "reviewing", "repairing"].includes(t.state));
  let s2 = "idle";
  let s2Label = "";
  if (isSucceeded || hasEvent("verify.started") || hasEvent("verify.passed")) {
    s2 = "done";
  } else if (hasEvent("build.tasks_started") || hasEvent("worker.created") || hasEvent("task.merged") || activeTask || (s1 === "done" && isRunning)) {
    s2 = isFailed ? "failed" : "active";
  } else if (isFailed && s1 === "done" && !errStr.includes("verification") && !errStr.includes("acceptance")) {
    s2 = "failed";
  }
  if (totalTasks > 0) {
    s2Label = `${mergedTasks}/${totalTasks} tasks`;
  }

  // Stage 3: Verify & Secret Gate
  let s3 = "idle";
  if (hasEvent("verify.passed") || hasEvent("test.started") || hasEvent("release.packaging") || isSucceeded) {
    s3 = "done";
  } else if (hasEvent("verify.started")) {
    s3 = isFailed ? "failed" : "active";
  } else if (isFailed && (errStr.includes("verification") || errStr.includes("secret"))) {
    s3 = "failed";
  }

  // Stage 4: Acceptance Test
  const flowCount = (plan && Array.isArray(plan.test_flows) && plan.test_flows.length) || 0;
  let s4 = "idle";
  let s4Label = "";
  if (hasEvent("test.passed") || hasEvent("release.packaging") || isSucceeded) {
    s4 = "done";
    if (flowCount === 0 && isSucceeded) s4Label = tr("stage.skipped");
  } else if (hasEvent("test.started")) {
    s4 = isFailed ? "failed" : "active";
  } else if (isFailed && errStr.includes("acceptance")) {
    s4 = "failed";
  }

  // Stage 5: Release Bundle
  let s5 = "idle";
  if (artifact || isSucceeded) s5 = "done";
  else if (hasEvent("release.packaging")) s5 = isFailed ? "failed" : "active";

  return [
    { num: "01", name: tr("stage.1"), status: s1, customLabel: "" },
    { num: "02", name: tr("stage.2"), status: s2, customLabel: s2Label },
    { num: "03", name: tr("stage.3"), status: s3, customLabel: "" },
    { num: "04", name: tr("stage.4"), status: s4, customLabel: s4Label },
    { num: "05", name: tr("stage.5"), status: s5, customLabel: "" },
  ];
}

function computeTaskWaves(tasks) {
  const remaining = tasks.map((t) => ({ task: t, deps: new Set(t.depends_on || []) }));
  const done = new Set();
  const waves = [];

  while (remaining.length) {
    const ready = remaining.filter((item) => [...item.deps].every((d) => done.has(d)));
    if (!ready.length) {
      // Fallback if graph has external/cycle reference
      waves.push(remaining.map((item) => item.task));
      break;
    }
    waves.push(ready.map((item) => item.task));
    for (const item of ready) {
      done.add(item.task.id);
      const idx = remaining.indexOf(item);
      if (idx >= 0) remaining.splice(idx, 1);
    }
  }
  return waves;
}

async function loadProjectDetail(projectId) {
  const proj = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`);
  currentProjectData = proj;
  setActiveNav("detail");
  renderProjectDetail(proj);
  if (selectedJobId) await inspectJob(selectedJobId);
  if (currentDetailTab === "code") await loadProjectFiles(projectId);
}

function renderProjectDetail(proj) {
  $("d-profile").textContent = proj.profile || "static";
  $("d-name").textContent = proj.name || proj.id;
  $("d-brief").textContent = proj.brief || "-";
  $("d-state").outerHTML = `<span id="d-state" class="badge ${esc(proj.state)}"><span class="badge-dot"></span>${esc(proj.state)}</span>`;
  $("d-created").textContent = proj.created ? fmtRelative(proj.created) : "";

  const jobs = proj.recent_jobs || [];
  $("d-jobs-count").textContent = `${jobs.length} job`;
  const latest = jobs[0];
  if (!selectedJobId && latest) selectedJobId = latest.id;

  const artifact = proj.artifact && typeof proj.artifact === "object" ? proj.artifact : null;
  const dlBtn = $("d-download-latest-btn");
  if (artifact && artifact.job_id && artifact.artifact_path) {
    dlBtn.hidden = false;
    dlBtn.onclick = () => window.downloadArtifact(proj.id, artifact.job_id);
  } else {
    dlBtn.hidden = true;
  }

  const previewBtn = $("d-preview-btn");
  previewBtn.hidden = !(proj.has_files || proj.state === "built" || artifact);
  previewBtn.onclick = () => openLivePreview(proj.id, proj.name || proj.id);

  // Connected Delivery Pipeline
  const stages = computePipelineStages(proj);
  $("d-pipeline").innerHTML = stages
    .map((s) => {
      const icon = s.status === "done" ? "✓" : s.status === "active" ? "●" : s.status === "failed" ? "✕" : "○";
      const label = s.customLabel || tr(`stage.${s.status}`);
      return `
        <div class="stage ${s.status}">
          <div class="stage-top">
            <span class="stage-num">${s.num}</span>
            <span class="stage-icon">${icon}</span>
          </div>
          <strong>${esc(s.name)}</strong>
          <span class="stage-state">${esc(label)}</span>
        </div>
      `;
    })
    .join("");

  // Release Artifact Banner
  const artCard = $("d-artifact-card");
  if (artifact && artifact.artifact_path) {
    artCard.hidden = false;
    const shaShort = artifact.commit_sha ? artifact.commit_sha.slice(0, 10) : "-";
    const digestShort = artifact.digest ? `${artifact.digest.slice(0, 18)}…` : "-";
    artCard.innerHTML = `
      <div>
        <div class="eyebrow" style="color:var(--ok);">${esc(tr("art.eyebrow"))}</div>
        <h2 style="margin-top:2px;">${esc(tr("art.title"))} (${esc(fmtBytes(artifact.size_bytes))})</h2>
        <div class="artifact-meta">
          <button type="button" class="copy-chip" onclick="window.copyText('${esc(artifact.commit_sha || "")}', 'Commit SHA')">
            <span>Commit: ${esc(shaShort)}</span>
          </button>
          <button type="button" class="copy-chip" onclick="window.copyText('${esc(artifact.digest || "")}', 'SHA-256 Digest')">
            <span>SHA-256: ${esc(digestShort)}</span>
          </button>
          <span class="mono muted-xs">${esc(artifact.verification || "Verification passed")}</span>
        </div>
      </div>
      <button class="primary" type="button" onclick="window.downloadArtifact('${esc(proj.id)}','${esc(artifact.job_id || (latest && latest.id) || "")}')">
        ${esc(tr("art.download"))}
      </button>
    `;
  } else {
    artCard.hidden = true;
  }

  // Architecture Plan & Wave DAG
  const planCard = $("d-plan-card");
  const planEmpty = $("d-plan-empty");
  const plan = proj.plan && typeof proj.plan === "object" ? proj.plan : null;
  const taskStateMap = {};
  for (const t of proj.latest_job_tasks || []) {
    taskStateMap[t.id] = t.state;
  }

  if (plan && Array.isArray(plan.tasks) && plan.tasks.length) {
    planCard.hidden = false;
    planEmpty.hidden = true;
    const flows = Array.isArray(plan.test_flows) ? plan.test_flows : [];
    const waves = computeTaskWaves(plan.tasks);

    $("d-plan-stats").innerHTML = `
      <span class="profile-chip">${waves.length} waves</span>
      <span class="profile-chip">${plan.tasks.length} tasks</span>
      <span class="profile-chip">${flows.length} flows</span>
    `;
    $("d-plan-summary").textContent = plan.summary || "";

    window.__currentPlanTaskLookup = {};
    $("d-plan-tasks").innerHTML = waves
      .map((waveTasks, wIdx) => {
        const cardsHtml = waveTasks
          .map((t) => {
            const liveState = taskStateMap[t.id] || (proj.state === "built" ? "merged" : "queued");
            window.__currentPlanTaskLookup[t.id] = { task: t, wave: wIdx + 1, state: liveState };
            const deps = (t.depends_on || []).map((d) => `<span class="dep-chip">← ${esc(d)}</span>`).join("");
            const skills = (t.skills || []).map((s) => `<span class="skill-chip">${esc(s)}</span>`).join("");
            return `
              <button type="button" class="dag-task interactive state-${esc(liveState)}" onclick="window.openTaskModal('${esc(t.id)}')">
                <div class="dag-task-head">
                  <span class="mono muted-xs">${esc(t.id)}</span>
                  ${renderBadge(liveState)}
                </div>
                <div class="dag-task-title">${esc(t.title)}</div>
                ${deps || skills ? `<div class="dag-deps">${deps}${skills}</div>` : ""}
              </button>
            `;
          })
          .join("");

        return `
          <div class="wave-lane">
            <div class="wave-header">
              <span>Wave ${wIdx + 1}</span>
              <span>${waveTasks.length} parallel task${waveTasks.length > 1 ? "s" : ""}</span>
            </div>
            <div class="dag-grid">${cardsHtml}</div>
          </div>
        `;
      })
      .join("");

    if (flows.length) {
      $("d-plan-flows-wrap").hidden = false;
      $("d-plan-flows").innerHTML = flows
        .map(
          (f) => `
          <div class="flow-item">
            <div>
              <strong>${esc(f.id)}</strong>
              <span class="muted-xs" style="margin-left:8px;">${esc(f.goal)}</span>
            </div>
            <span class="profile-chip">${esc(f.kind)} · ${(f.steps || []).length} steps</span>
          </div>
        `
        )
        .join("");
    } else {
      $("d-plan-flows-wrap").hidden = true;
    }
  } else {
    planCard.hidden = true;
    planEmpty.hidden = false;
  }

  // Test Execution Report (in Code & Tests tab)
  renderTestReport(artifact);

  // Job History List
  $("d-jobs").innerHTML = jobs.length
    ? jobs
        .map(
          (j) => `
        <div class="row clickable ${j.id === selectedJobId ? "selected" : ""}" onclick="window.inspectJob('${esc(j.id)}')">
          <div>
            <div class="mono" style="font-weight:600;">${esc(j.id.slice(0, 12))}</div>
            <div class="muted-xs">${esc(j.kind || "build")} · ${esc(fmtRelative(j.created))}</div>
          </div>
          ${renderBadge(j.state)}
        </div>
      `
        )
        .join("")
    : '<div class="empty-inline">Belum ada riwayat job.</div>';

  // Wire Hero Action Buttons
  $("d-rebuild-btn").onclick = async () => {
    const btn = $("d-rebuild-btn");
    btn.disabled = true;
    try {
      const res = await apiFetch("/api/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product: proj.id, profile: proj.profile || "static", brief: proj.brief }),
      });
      selectedJobId = res.job_id;
      showToast(`Build ulang dimulai (${res.job_id.slice(0, 8)})`, "ok");
      await refreshWorkspace();
    } catch (err) {
      showToast(`Gagal memulai build ulang: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  };

  $("d-edit-btn").onclick = () => {
    $("ep-name").value = proj.name || proj.id;
    $("ep-profile").value = proj.profile || "static";
    $("ep-brief").value = proj.brief || "";
    $("edit-project-modal").hidden = false;
  };

  $("d-delete-btn").onclick = () => {
    $("delete-project-name").textContent = `${proj.name || proj.id} (${proj.id})`;
    $("delete-project-modal").hidden = false;
  };
}

/* ==========================================================================
   Task Detail Modal
   ========================================================================== */
window.openTaskModal = (taskId) => {
  const item = window.__currentPlanTaskLookup && window.__currentPlanTaskLookup[taskId];
  if (!item) return;
  const { task, wave, state } = item;

  $("task-modal-title").textContent = task.title;
  $("task-modal-id").textContent = task.id;
  $("task-modal-wave").textContent = `Wave ${wave}`;
  $("task-modal-state").outerHTML = `<span id="task-modal-state" class="badge ${esc(state)}"><span class="badge-dot"></span>${esc(state)}</span>`;

  const deps = task.depends_on || [];
  $("task-modal-deps-wrap").hidden = deps.length === 0;
  $("task-modal-deps").innerHTML = deps.map((d) => `<span class="dep-chip">← ${esc(d)}</span>`).join("");

  const skills = task.skills || [];
  $("task-modal-skills-wrap").hidden = skills.length === 0;
  $("task-modal-skills").innerHTML = skills.map((s) => `<span class="skill-chip">${esc(s)}</span>`).join("");

  $("task-modal-instructions").textContent = task.instructions || "-";
  $("task-modal-acceptance").innerHTML = (task.acceptance || [])
    .map((c) => `<li>${esc(c)}</li>`)
    .join("");

  $("task-modal").hidden = false;
};

/* ==========================================================================
   Code Browser & Test Execution Report
   ========================================================================== */
function renderTestReport(artifact) {
  const box = $("d-test-report");
  if (!artifact) {
    box.innerHTML = `<div class="empty-inline" style="grid-column:1 / -1;">${esc(tr("code.no_tests"))}</div>`;
    return;
  }

  const cards = [];
  if (artifact.verification) {
    cards.push(`
      <div class="test-card passed">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <strong>Build &amp; Secret Gate</strong>
          ${renderBadge("passed")}
        </div>
        <div class="mono muted-xs">${esc(artifact.verification)}</div>
      </div>
    `);
  }

  for (const r of artifact.test_results || []) {
    const cls = r.passed ? "passed" : "failed";
    cards.push(`
      <div class="test-card ${cls}">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <strong class="mono">${esc(r.id || "flow")}</strong>
          ${renderBadge(r.passed ? "passed" : "failed")}
        </div>
        <div style="font-size:0.82rem;">${esc(r.goal || "")}</div>
        <div class="mono muted-xs">${esc(r.kind || "")} · ${esc(r.detail || "")}</div>
      </div>
    `);
  }

  box.innerHTML = cards.length
    ? cards.join("")
    : `<div class="empty-inline" style="grid-column:1 / -1;">${esc(tr("code.no_tests"))}</div>`;
}

async function loadProjectFiles(projectId) {
  try {
    const data = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/files`);
    cachedFiles = data.files || [];
    renderFileList();
    if (cachedFiles.length && !cachedFiles.some((f) => f.path === selectedFilePath)) {
      const preferred =
        cachedFiles.find((f) => f.path === "index.html") ||
        cachedFiles.find((f) => f.path === "main.go") ||
        cachedFiles[0];
      if (preferred) await selectProjectFile(projectId, preferred.path);
    } else if (!cachedFiles.length) {
      $("d-file-path").textContent = "-";
      $("d-file-meta").textContent = "";
      $("d-file-copy-btn").hidden = true;
      $("d-code-viewer").innerHTML = `<div class="empty-inline" style="padding:20px;">${esc(tr("code.no_files"))}</div>`;
    }
  } catch (err) {
    $("d-file-list").innerHTML = `<div class="empty-inline">${esc(err.message)}</div>`;
  }
}

function renderFileList() {
  const q = ($("d-file-search").value || "").trim().toLowerCase();
  const filtered = cachedFiles.filter((f) => !q || f.path.toLowerCase().includes(q));
  if (!filtered.length) {
    $("d-file-list").innerHTML = `<div class="empty-inline">${esc(tr("code.no_files"))}</div>`;
    return;
  }
  $("d-file-list").innerHTML = filtered
    .map(
      (f) => `
      <button type="button" class="file-item ${f.path === selectedFilePath ? "active" : ""}" onclick="window.selectProjectFile('${esc(selectedProjectId)}','${esc(f.path)}')">
        <span class="file-item-name">${esc(f.path)}</span>
        <span class="muted-xs">${esc(fmtBytes(f.size))}</span>
      </button>
    `
    )
    .join("");
}

window.selectProjectFile = async (projectId, filePath) => {
  selectedFilePath = filePath;
  renderFileList();
  try {
    const data = await apiFetch(
      `/api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(filePath)}`
    );
    $("d-file-path").textContent = data.path;
    $("d-file-meta").textContent = `${fmtBytes(data.size)}${data.truncated ? " (truncated)" : ""}`;
    if (data.binary) {
      selectedFileContent = "";
      $("d-file-copy-btn").hidden = true;
      $("d-code-viewer").innerHTML = `<div class="empty-inline" style="padding:20px;">Binary file (${esc(fmtBytes(data.size))})</div>`;
      return;
    }
    selectedFileContent = data.content || "";
    $("d-file-copy-btn").hidden = false;
    const lines = selectedFileContent.split("\n");
    $("d-code-viewer").innerHTML = `<div class="code-lines">${lines
      .map(
        (ln, i) =>
          `<div class="code-line"><span class="code-ln">${i + 1}</span><span class="code-txt">${esc(ln) || " "}</span></div>`
      )
      .join("")}</div>`;
  } catch (err) {
    $("d-code-viewer").innerHTML = `<div class="error-box" style="margin:12px;">${esc(err.message)}</div>`;
  }
};

$("d-file-search").addEventListener("input", renderFileList);
$("d-file-copy-btn").addEventListener("click", () => {
  if (selectedFileContent) window.copyText(selectedFileContent, selectedFilePath || "Kode");
});

/* ==========================================================================
   Live Product Preview Modal
   ========================================================================== */
async function openLivePreview(projectId, projectName) {
  try {
    showToast("Menyiapkan live preview...", "info");
    const res = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/preview/start`, {
      method: "POST",
    });
    $("preview-title").textContent = `${projectName} — Live Preview`;
    $("preview-mode-badge").textContent = res.mode === "proxy" ? "live backend" : "static";
    $("preview-stop-btn").hidden = res.mode !== "proxy";
    $("preview-newtab-btn").href = res.preview_url;
    $("preview-iframe").src = res.preview_url;
    setPreviewViewport("desktop");
    $("preview-modal").hidden = false;
  } catch (err) {
    showToast(`Gagal membuka preview: ${err.message}`, "err");
  }
}

function setPreviewViewport(vp) {
  $("preview-stage").className = `preview-stage vp-${vp}`;
  $("preview-viewports").querySelectorAll(".filter-pill").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.vp === vp);
  });
}

$("preview-viewports").querySelectorAll(".filter-pill").forEach((btn) => {
  btn.addEventListener("click", () => setPreviewViewport(btn.dataset.vp));
});

$("preview-reload-btn").addEventListener("click", () => {
  const frame = $("preview-iframe");
  if (frame.src) frame.src = frame.src;
});

$("preview-stop-btn").addEventListener("click", async () => {
  if (!selectedProjectId) return;
  try {
    await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}/preview/stop`, { method: "POST" });
    $("preview-modal").hidden = true;
    $("preview-iframe").src = "about:blank";
    showToast("Preview server dihentikan", "ok");
  } catch (err) {
    showToast(`Gagal menghentikan preview: ${err.message}`, "err");
  }
});

function closePreviewModal() {
  $("preview-modal").hidden = true;
  $("preview-iframe").src = "about:blank";
}
$("preview-close-btn").addEventListener("click", closePreviewModal);

/* ==========================================================================
   Incremental Adjust 2-Step
   ========================================================================== */
async function runAdjustPreview() {
  if (!selectedProjectId) return;
  const instruction = $("adj-instruction").value.trim();
  if (!instruction) {
    $("adj-status").textContent = "Isi instruksi perubahan terlebih dahulu.";
    return;
  }
  $("adj-status").textContent = "Menganalisis scope domain...";
  $("adj-preview-btn").disabled = true;
  try {
    const data = await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}/adjust/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    lastAdjustPreview = { instruction, candidates: data.candidates || [] };
    $("adj-candidates").innerHTML = lastAdjustPreview.candidates
      .map(
        (c, idx) => `
      <label class="cand-row">
        <input type="checkbox" class="adj-cand-cb" data-idx="${idx}" ${idx === 0 ? "checked" : ""} style="margin-top:3px;" />
        <div style="flex:1;min-width:0;">
          <div style="font-weight:600;font-size:0.88rem;">${esc(c.domain_path)}</div>
          <div class="muted-xs" style="margin-top:2px;">${esc(c.reason)}</div>
          ${
            c.target_files && c.target_files.length
              ? `<div class="file-chips">${c.target_files.map((f) => `<span class="dep-chip">${esc(f)}</span>`).join("")}</div>`
              : ""
          }
        </div>
      </label>
    `
      )
      .join("");
    $("adj-scope-box").hidden = false;
    $("adj-status").textContent = `${lastAdjustPreview.candidates.length} kandidat domain ditemukan.`;
  } catch (err) {
    $("adj-status").textContent = `Gagal cek scope: ${err.message}`;
    showToast(`Gagal analisis scope: ${err.message}`, "err");
  } finally {
    $("adj-preview-btn").disabled = false;
  }
}

$("adj-preview-btn").addEventListener("click", runAdjustPreview);
$("adjust-preview-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runAdjustPreview();
});

$("adj-cancel-btn").addEventListener("click", () => {
  $("adj-scope-box").hidden = true;
  $("adj-status").textContent = "";
  lastAdjustPreview = null;
});

$("adj-confirm-btn").addEventListener("click", async () => {
  if (!selectedProjectId || !lastAdjustPreview) return;
  const selectedDomains = [];
  const targetFiles = [];
  document.querySelectorAll(".adj-cand-cb:checked").forEach((cb) => {
    const cand = lastAdjustPreview.candidates[Number(cb.dataset.idx)];
    if (cand) {
      selectedDomains.push(cand.domain_path);
      for (const f of cand.target_files || []) {
        if (!targetFiles.includes(f)) targetFiles.push(f);
      }
    }
  });
  if (!selectedDomains.length) {
    $("adj-status").textContent = "Pilih minimal 1 domain scope.";
    return;
  }
  $("adj-confirm-btn").disabled = true;
  $("adj-status").textContent = "Menjalankan incremental adjust...";
  try {
    const res = await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}/adjust`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instruction: lastAdjustPreview.instruction,
        selected_domains: selectedDomains,
        target_files: targetFiles,
      }),
    });
    $("adj-scope-box").hidden = true;
    $("adj-instruction").value = "";
    $("adj-status").textContent = "";
    selectedJobId = res.job_id;
    showToast(`Adjust job dimulai (${res.job_id.slice(0, 8)})`, "ok");
    await refreshWorkspace();
  } catch (err) {
    $("adj-status").textContent = `Gagal memulai adjust: ${err.message}`;
    showToast(`Gagal memulai adjust: ${err.message}`, "err");
  } finally {
    $("adj-confirm-btn").disabled = false;
  }
});

/* ==========================================================================
   Job Inspector, Jobs List & Event Ledger + Export
   ========================================================================== */
window.inspectJob = async (jobId) => {
  selectedJobId = jobId;
  document.querySelectorAll("#d-jobs .row").forEach((el) => {
    el.classList.toggle("selected", el.getAttribute("onclick")?.includes(jobId));
  });

  const j = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  const u = (j.result && j.result.token_usage) || {};
  $("d-job-duration").textContent = j.started ? `Durasi: ${fmtDuration(j.started, j.finished)}` : "";

  const tasksHtml = (j.tasks || [])
    .map(
      (t) => `
    <div class="row" style="padding:7px 10px;">
      <span class="mono">${esc(t.id)}</span>
      ${renderBadge(t.state)}
    </div>
  `
    )
    .join("");

  const acpErrors = (j.acp_errors || []).map(({ created, detail }) => {
    const context = [detail.stage, detail.harness, detail.model_id, detail.task_id];
    if (detail.attempt != null) context.push(`${tr("job.attempt")} ${detail.attempt}`);
    if (detail.review_attempt != null) context.push(`${tr("job.review_attempt")} ${detail.review_attempt}`);
    context.push(fmtRelative(created));
    return `<div class="stack-xs">
      <small class="muted-xs">${esc(context.filter(Boolean).join(" · "))}</small>
      <div class="error-box">${esc(detail.error)}</div>
    </div>`;
  }).join("");

  const btns = [];
  if (j.can_retry) {
    btns.push(`<button class="primary sm" id="retry-build-btn" type="button">${esc(tr("job.retry"))}</button>`);
  }
  if (j.state === "running" || j.state === "queued") {
    btns.push(`<button class="danger sm" onclick="window.cancelJob('${esc(j.id)}')">${esc(tr("job.cancel"))}</button>`);
  }
  if (j.result && j.result.artifact_path) {
    btns.push(
      `<button class="primary sm" onclick="window.downloadArtifact('${esc(j.project_id)}','${esc(j.id)}')">${esc(tr("art.download"))}</button>`
    );
  }

  $("d-job-panel").innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
      <button type="button" class="copy-chip" onclick="window.copyText('${esc(j.id)}', 'Job ID')">${esc(j.id)}</button>
      ${renderBadge(j.state)}
    </div>
    ${j.payload ? buildRoles.map((role) => {
      const harness = j.payload[`${role}_harness`] || j.payload.harness;
      const model = j.payload[`${role}_harness`] ? j.payload[`${role}_model_id`] : j.payload.model_id;
      return harness ? `<div class="muted-xs">${esc(tr(`mod.${role}_harness`))} · ${esc([harness, model].filter(Boolean).join(" · "))}</div>` : "";
    }).join("") : ""}

    ${
      !isClientMode
        ? `
      <div class="token-bar">${renderTokenBar(u)}</div>
      <div class="token-grid">
        <div class="tok-box"><small>Total</small><strong>${fmtCompact(u.total_tokens)}</strong></div>
        <div class="tok-box"><small>Input</small><strong>${fmtCompact(u.input_tokens)}</strong></div>
        <div class="tok-box"><small>Output</small><strong>${fmtCompact(u.output_tokens)}</strong></div>
        <div class="tok-box"><small>Think / Cache</small><strong>${fmtCompact((u.thinking_tokens || 0) + (u.cache_read_tokens || 0))}</strong></div>
      </div>
    `
        : ""
    }

    ${j.error ? `<div class="error-box">${esc(j.error)}</div>` : ""}
    ${acpErrors ? `<section class="stack-xs"><strong>${esc(tr("job.acp_errors"))}</strong>${acpErrors}</section>` : ""}
    ${btns.length ? `<div style="display:flex;gap:8px;flex-wrap:wrap;">${btns.join("")}</div>` : ""}

    <div class="stack-xs">
      <strong style="font-size:0.84rem;">${esc(tr("job.tasks_title"))} (${(j.tasks || []).length})</strong>
      ${tasksHtml || '<span class="muted-xs">Belum ada task tercatat untuk job ini.</span>'}
    </div>
  `;
  $("retry-build-btn")?.addEventListener("click", () => openBuildModal(j));
};

function renderAllJobs() {
  const q = ($("job-search").value || "").trim().toLowerCase();
  const filtered = cachedJobs.filter((j) => {
    if (jobFilter === "active" && j.state !== "running" && j.state !== "queued") return false;
    if (jobFilter === "succeeded" && j.state !== "succeeded") return false;
    if (jobFilter === "failed" && j.state !== "failed" && j.state !== "cancelled") return false;
    if (q) {
      const hay = `${j.id} ${j.project_id} ${j.state} ${j.kind || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  $("all-jobs-list").innerHTML = filtered.length
    ? filtered
        .map((j) => {
          const u = j.token_usage || {};
          const dur = j.started ? ` · ${fmtDuration(j.started, j.finished)}` : "";
          return `
          <div class="row clickable" onclick="window.openProject('${esc(j.project_id)}','${esc(j.id)}')">
            <div style="min-width:0;flex:1;">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <strong style="font-size:0.9rem;">${esc(j.project_id)}</strong>
                <span class="mono muted-xs">${esc(j.id.slice(0, 12))}${esc(dur)}</span>
              </div>
              ${
                !isClientMode
                  ? `<div class="mono muted-xs" style="margin-top:3px;">total ${fmtNum(u.total_tokens)} tok · in ${fmtCompact(u.input_tokens)} / out ${fmtCompact(u.output_tokens)} / think ${fmtCompact(u.thinking_tokens)}</div>`
                  : ""
              }
            </div>
            <div style="display:flex;align-items:center;gap:10px;">
              <span class="muted-xs">${esc(fmtRelative(j.created))}</span>
              ${renderBadge(j.state)}
            </div>
          </div>
        `;
        })
        .join("")
    : '<div class="card empty-state"><strong>Belum ada job yang cocok</strong><span class="muted-xs">Job build yang dijalankan akan muncul beserta telemetri token di sini.</span></div>';
}

function getFilteredEvents() {
  const q = ($("event-search").value || "").trim().toLowerCase();
  return cachedEvents.filter((e) => {
    const kind = String(e.kind || "").toLowerCase();
    if (eventCatFilter === "build" && !kind.includes("build") && !kind.includes("job") && !kind.includes("verify") && !kind.includes("test") && !kind.includes("release")) return false;
    if (eventCatFilter === "plan" && !kind.includes("plan")) return false;
    if (eventCatFilter === "task" && !kind.includes("task") && !kind.includes("worker")) return false;
    if (eventCatFilter === "error" && !kind.includes("fail") && !kind.includes("cancel") && !kind.includes("error")) return false;
    if (q) {
      const hay = `${e.seq} ${e.kind} ${e.project_id || ""} ${JSON.stringify(e.detail || "")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderEvents() {
  const filtered = getFilteredEvents();
  $("events-list").innerHTML = filtered.length
    ? filtered
        .map((e) => {
          const kindState = e.kind.includes("succeeded") || e.kind.includes("passed")
            ? "succeeded"
            : e.kind.includes("failed")
              ? "failed"
              : e.kind.includes("started")
                ? "running"
                : "";
          const detailStr =
            e.detail && typeof e.detail === "object" && Object.keys(e.detail).length
              ? JSON.stringify(e.detail)
              : "";
          return `
          <div class="event-row">
            <div class="event-row-top">
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span class="mono muted-xs">#${e.seq}</span>
                <span class="badge ${kindState}">${esc(e.kind)}</span>
                <span class="mono">${esc(e.project_id || "-")}</span>
              </div>
              <span class="mono muted-xs">${new Date(e.created * 1000).toLocaleString("id-ID")}</span>
            </div>
            ${detailStr ? `<div class="event-detail">${esc(detailStr)}</div>` : ""}
          </div>
        `;
        })
        .join("")
    : '<div class="card empty-state"><strong>Belum ada event tercatat</strong><span class="muted-xs">Audit trail akan muncul saat proyek dibuat atau dijalankan.</span></div>';
}

function triggerDownloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

$("export-events-json").addEventListener("click", () => {
  const items = getFilteredEvents();
  triggerDownloadBlob(JSON.stringify(items, null, 2), "factory-events.json", "application/json");
  showToast(`Mengekspor ${items.length} event (JSON)`, "ok");
});

$("export-events-csv").addEventListener("click", () => {
  const items = getFilteredEvents();
  const csvEsc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = ["seq,kind,project_id,job_id,created_iso,detail"];
  for (const e of items) {
    lines.push(
      [
        e.seq,
        csvEsc(e.kind),
        csvEsc(e.project_id || ""),
        csvEsc(e.job_id || ""),
        csvEsc(new Date(e.created * 1000).toISOString()),
        csvEsc(typeof e.detail === "object" ? JSON.stringify(e.detail) : e.detail || ""),
      ].join(",")
    );
  }
  triggerDownloadBlob(lines.join("\n"), "factory-events.csv", "text/csv;charset=utf-8");
  showToast(`Mengekspor ${items.length} event (CSV)`, "ok");
});

window.cancelJob = async (jobId) => {
  try {
    await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    showToast("Permintaan pembatalan job dikirim", "ok");
    await refreshWorkspace();
  } catch (err) {
    showToast(`Gagal membatalkan job: ${err.message}`, "err");
  }
};

window.downloadArtifact = async (projectId, jobId) => {
  try {
    const res = await fetch(`/api/artifacts/${encodeURIComponent(projectId)}/${encodeURIComponent(jobId)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${projectId}-${jobId.slice(0, 8)}.tar.gz`;
    a.click();
    URL.revokeObjectURL(url);
    showToast(`Mengunduh ${projectId}-${jobId.slice(0, 8)}.tar.gz`, "ok");
  } catch (err) {
    showToast(`Gagal mengunduh artifak: ${err.message}`, "err");
  }
};

/* ==========================================================================
   Edit, Delete & Branding Modals
   ========================================================================== */
$("edit-project-close").addEventListener("click", () => { $("edit-project-modal").hidden = true; });
$("edit-project-cancel").addEventListener("click", () => { $("edit-project-modal").hidden = true; });
$("edit-project-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedProjectId) return;
  const btn = $("ep-save-btn");
  btn.disabled = true;
  try {
    await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("ep-name").value.trim(),
        profile: $("ep-profile").value,
        brief: $("ep-brief").value.trim(),
      }),
    });
    $("edit-project-modal").hidden = true;
    showToast("Detail proyek diperbarui", "ok");
    await refreshWorkspace();
  } catch (err) {
    showToast(`Gagal memperbarui proyek: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

$("delete-project-close").addEventListener("click", () => { $("delete-project-modal").hidden = true; });
$("delete-project-cancel").addEventListener("click", () => { $("delete-project-modal").hidden = true; });
$("delete-project-confirm").addEventListener("click", async () => {
  if (!selectedProjectId) return;
  const btn = $("delete-project-confirm");
  btn.disabled = true;
  try {
    await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}`, { method: "DELETE" });
    $("delete-project-modal").hidden = true;
    selectedProjectId = null;
    selectedJobId = null;
    currentProjectData = null;
    setActiveNav("overview");
    showToast("Proyek berhasil dihapus", "ok");
    await refreshWorkspace();
  } catch (err) {
    showToast(`Gagal menghapus proyek: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

function setBrandingSwatch(hex) {
  $("br-color").value = hex;
  $("br-swatches").querySelectorAll(".color-swatch").forEach((sw) => {
    sw.classList.toggle("active", sw.dataset.color.toLowerCase() === hex.toLowerCase());
  });
}

$("br-swatches").querySelectorAll(".color-swatch").forEach((sw) => {
  sw.addEventListener("click", () => setBrandingSwatch(sw.dataset.color));
});
$("br-color").addEventListener("input", (e) => setBrandingSwatch(e.target.value));

$("branding-btn").addEventListener("click", () => {
  $("br-name").value = uiBranding.brand_name || "acp-factory";
  $("br-sub").value = uiBranding.brand_subtitle || "AUTONOMOUS STUDIO";
  $("br-client").value = uiBranding.client_label || "";
  setBrandingSwatch(uiBranding.accent_color || "#3b82f6");
  $("br-lang").value = uiBranding.default_lang || window.getFactoryLang();
  $("branding-modal").hidden = false;
});
$("branding-close").addEventListener("click", () => { $("branding-modal").hidden = true; });
$("branding-cancel").addEventListener("click", () => { $("branding-modal").hidden = true; });
$("branding-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("br-save-btn");
  btn.disabled = true;
  try {
    const saved = await apiFetch("/api/settings/ui", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        brand_name: $("br-name").value.trim() || "acp-factory",
        brand_subtitle: $("br-sub").value.trim(),
        client_label: $("br-client").value.trim(),
        accent_color: $("br-color").value,
        default_lang: $("br-lang").value,
      }),
    });
    localStorage.setItem(LANG_KEY, saved.default_lang);
    applyBranding(saved);
    applyTranslations();
    $("branding-modal").hidden = true;
    showToast("Pengaturan white-label disimpan", "ok");
  } catch (err) {
    showToast(`Gagal menyimpan branding: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

$("task-modal-close").addEventListener("click", () => { $("task-modal").hidden = true; });

/* ==========================================================================
   Navigation, Filters, Auth & Build Modal
   ========================================================================== */
document.querySelectorAll(".nav").forEach((btn) => {
  btn.addEventListener("click", () => {
    setActiveNav(btn.dataset.tab);
    refreshWorkspace();
  });
});

document.querySelectorAll(".detail-tab").forEach((btn) => {
  btn.addEventListener("click", () => setDetailTab(btn.dataset.detailTab));
});

$("project-search").addEventListener("input", renderProjects);
$("job-search").addEventListener("input", renderAllJobs);
$("event-search").addEventListener("input", renderEvents);

$("project-filters").querySelectorAll(".filter-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    projectFilter = pill.dataset.filter;
    $("project-filters").querySelectorAll(".filter-pill").forEach((p) => p.classList.toggle("active", p === pill));
    renderProjects();
  });
});

$("job-filters").querySelectorAll(".filter-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    jobFilter = pill.dataset.filter;
    $("job-filters").querySelectorAll(".filter-pill").forEach((p) => p.classList.toggle("active", p === pill));
    renderAllJobs();
  });
});

$("event-cat-filters").querySelectorAll(".filter-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    eventCatFilter = pill.dataset.cat;
    $("event-cat-filters").querySelectorAll(".filter-pill").forEach((p) => p.classList.toggle("active", p === pill));
    renderEvents();
  });
});

$("toggle-token-vis").addEventListener("click", () => {
  const inp = $("token-input");
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  $("toggle-token-vis").textContent = show ? tr("login.hide") : tr("login.show");
});

$("connect-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  const btn = $("connect-submit-btn");
  btn.disabled = true;
  try {
    setToken($("token-input").value);
    await refreshWorkspace();
    if (getToken()) showToast("Terhubung ke ACP Software Factory", "ok");
  } finally {
    btn.disabled = false;
  }
});

$("logout-btn").addEventListener("click", () => {
  clearToken();
  disconnectEventStream();
  $("token-input").value = "";
  showToast("Sesi diputuskan");
  refreshWorkspace();
});

$("refresh-btn").addEventListener("click", () => {
  refreshWorkspace();
  showToast("Data workspace diperbarui");
});

$("poll-toggle-btn").addEventListener("click", () => {
  isPollingPaused = !isPollingPaused;
  $("poll-toggle-btn").classList.toggle("paused", isPollingPaused);
  $("poll-label").textContent = isPollingPaused ? tr("top.paused") : tr("top.live");
  if (isPollingPaused) {
    disconnectEventStream();
    showToast("Live update dijeda (mode presentasi)");
  } else {
    connectEventStream();
    refreshWorkspace(true);
    showToast("Live update aktif kembali");
  }
});

$("back-btn").addEventListener("click", () => {
  setActiveNav("overview");
  refreshWorkspace();
});

function setProfileSelection(val) {
  $("f-profile").value = val;
  $("profile-selector").querySelectorAll(".profile-option").forEach((opt) => {
    opt.classList.toggle("active", opt.dataset.value === val);
  });
}

$("profile-selector").querySelectorAll(".profile-option").forEach((opt) => {
  opt.addEventListener("click", () => setProfileSelection(opt.dataset.value));
});

document.querySelectorAll(".preset-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const preset = DEMO_PRESETS[chip.dataset.preset];
    if (!preset) return;
    $("f-product").value = preset.product;
    setProfileSelection(preset.profile);
    $("f-brief").value = preset.brief;
    showToast(`Template "${chip.textContent}" dimuat`);
  });
});

function syncBuildModelField(role) {
  const supportsModel = ["opencode", "pi"].includes($(`f-${role}-harness`).value);
  $(`f-${role}-model-field`).hidden = !supportsModel;
  $(`f-${role}-model-id`).disabled = !supportsModel;
}

for (const role of buildRoles) {
  $(`f-${role}-harness`).addEventListener("change", () => {
    $(`f-${role}-model-id`).value = "";
    syncBuildModelField(role);
  });
}

function populateHarnessSelect(doctor) {
  if (!doctor) return;
  const known = ["agy", "opencode", "codex", "pi", "claude"];
  const avail = doctor.available_harnesses || {};
  const isEn = window.getFactoryLang() === "en";
  const order = [...known];
  if (doctor.harness && !order.includes(doctor.harness)) order.unshift(doctor.harness);

  const options = order
    .map((name) => {
      const detected = Boolean(avail[name]);
      const suffix = detected ? (isEn ? " (detected)" : " (terdeteksi)") : "";
      return `<option value="${esc(name)}">${esc(name)}${esc(suffix)}</option>`;
    })
    .join("");

  for (const role of buildRoles) {
    const sel = $(`f-${role}-harness`);
    const current = sel.dataset.populated ? sel.value : doctor.harness;
    sel.innerHTML = options;
    if (order.includes(current)) sel.value = current;
    sel.dataset.populated = "true";
    syncBuildModelField(role);
  }
}

function closeBuildModal() {
  $("build-modal").hidden = true;
}

function openBuildModal(job = null) {
  const retry = Boolean(job);
  retryBuildJobId = job?.id ?? null;
  for (const id of ["build-eyebrow", "new-build-title", "new-build-submit", "build-presets", "f-product-field", "f-profile-field", "f-brief-field"]) {
    $(id).hidden = retry;
  }
  for (const id of ["retry-build-title", "retry-build-hint", "retry-build-submit"]) {
    $(id).hidden = !retry;
  }
  $("f-brief").disabled = retry;
  if (job) {
    for (const role of buildRoles) {
      const sel = $(`f-${role}-harness`);
      const roleHarness = job.payload?.[`${role}_harness`];
      const harness = roleHarness || job.payload?.harness;
      if ([...sel.options].some((option) => option.value === harness)) sel.value = harness;
      $(`f-${role}-model-id`).value = (roleHarness ? job.payload?.[`${role}_model_id`] : job.payload?.model_id) || "";
      syncBuildModelField(role);
    }
  }
  $("f-build-error").hidden = true;
  $("build-modal").hidden = false;
  setTimeout(() => $(retry ? "f-planner-harness" : "f-product").focus(), 40);
}

$("new-project-btn").addEventListener("click", () => openBuildModal());
$("close-modal-btn").addEventListener("click", closeBuildModal);
$("cancel-modal-btn").addEventListener("click", closeBuildModal);

// Backdrop click & Escape to close any open modal
for (const mId of [
  "build-modal",
  "preview-modal",
  "task-modal",
  "edit-project-modal",
  "delete-project-modal",
  "branding-modal",
]) {
  $(mId).addEventListener("click", (e) => {
    if (e.target === $(mId)) {
      if (mId === "preview-modal") closePreviewModal();
      else $(mId).hidden = true;
    }
  });
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("preview-modal").hidden) closePreviewModal();
  else if (!$("task-modal").hidden) $("task-modal").hidden = true;
  else if (!$("edit-project-modal").hidden) $("edit-project-modal").hidden = true;
  else if (!$("delete-project-modal").hidden) $("delete-project-modal").hidden = true;
  else if (!$("branding-modal").hidden) $("branding-modal").hidden = true;
  else if (!$("build-modal").hidden) closeBuildModal();
});

$("build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("submit-build-btn");
  const retryJobId = retryBuildJobId;
  const errorBox = $("f-build-error");
  errorBox.hidden = true;
  btn.disabled = true;
  try {
    const options = {};
    for (const role of buildRoles) {
      options[`${role}_harness`] = $(`f-${role}-harness`).value;
      const modelInput = $(`f-${role}-model-id`);
      if (!modelInput.disabled && modelInput.value.trim()) {
        options[`${role}_model_id`] = modelInput.value.trim();
      }
    }
    const res = await apiFetch(retryJobId ? `/api/jobs/${encodeURIComponent(retryJobId)}/retry` : "/api/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(retryJobId ? options : {
        product: $("f-product").value.trim() || null,
        profile: $("f-profile").value,
        ...options,
        brief: $("f-brief").value.trim(),
      }),
    });
    closeBuildModal();
    if (!retryJobId) {
      $("f-brief").value = "";
      $("f-product").value = "";
      setProfileSelection("static");
    }
    for (const role of buildRoles) $(`f-${role}-model-id`).value = "";
    showToast(`Build dimulai untuk ${res.project_id}`, "ok");
    await window.openProject(res.project_id, retryJobId ? res.job_id : null);
    selectedJobId = res.job_id;
    await refreshWorkspace();
  } catch (err) {
    if ($("build-modal").hidden) {
      showToast(err.message, "err");
    } else {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    }
  } finally {
    btn.disabled = false;
  }
});

/* Initial Boot */
(async () => {
  await loadUiBranding();
  applyTranslations();
  await refreshWorkspace();
})();

fallbackPollTimer = setInterval(() => {
  if (!isPollingPaused && !sseConnected && getToken()) {
    refreshWorkspace(true);
  }
}, 15000);
