const TOKEN_KEY = "acp_factory_operator_token";
const $ = (id) => document.getElementById(id);

let currentTab = "overview";
let selectedProjectId = null;
let selectedJobId = null;
let pollTimer = null;
let lastAdjustPreview = null;

const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => localStorage.setItem(TOKEN_KEY, t.trim());
const clearToken = () => localStorage.removeItem(TOKEN_KEY);
const fmtNum = (n) => Number(n || 0).toLocaleString("id-ID");

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
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
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  const titles = { overview: "Proyek", detail: selectedProjectId || "Detail Proyek", jobs: "Jobs & Token", events: "Event Ledger" };
  $("crumb-title").textContent = titles[tab] || "Proyek";
}

async function checkHealthAndAuth() {
  try {
    const h = await apiFetch("/health");
    $("sidebar-dot").className = h.status === "ok" ? "dot ok" : "dot err";
    $("sidebar-status").textContent = h.status === "ok" ? "Backend Online" : "Offline";
  } catch {
    $("sidebar-dot").className = "dot err";
    $("sidebar-status").textContent = "Offline";
  }

  if (!getToken()) {
    $("connection-text").textContent = "Belum terhubung";
    $("refresh-btn").hidden = true;
    $("logout-btn").hidden = true;
    showView("login-view");
    return false;
  }

  try {
    await apiFetch("/api/doctor");
    $("connection-text").textContent = "Terhubung";
    $("refresh-btn").hidden = false;
    $("logout-btn").hidden = false;
    return true;
  } catch (err) {
    $("login-error").textContent = `Token ditolak atau server error: ${err.message}`;
    clearToken();
    showView("login-view");
    return false;
  }
}

async function refreshWorkspace() {
  const ok = await checkHealthAndAuth();
  if (!ok) return;

  const [doctor, projects, jobs, events] = await Promise.all([
    apiFetch("/api/doctor"),
    apiFetch("/api/projects"),
    apiFetch("/api/jobs?limit=40"),
    apiFetch("/api/events?limit=40"),
  ]);

  // Metrics
  $("m-projects").textContent = fmtNum(projects.length);
  $("m-harness").textContent = `Harness: ${doctor.harness} (${doctor.status})`;

  const activeJobs = jobs.filter((j) => j.state === "running" || j.state === "queued").length;
  const okJobs = jobs.filter((j) => j.state === "succeeded").length;
  $("m-jobs").textContent = fmtNum(jobs.length);
  $("m-jobs-sub").textContent = `aktif: ${activeJobs} | sukses: ${okJobs}`;

  let totIn = 0, totOut = 0, totThink = 0, totCache = 0, totAll = 0;
  for (const j of jobs) {
    const u = j.token_usage || {};
    totIn += u.input_tokens || 0;
    totOut += u.output_tokens || 0;
    totThink += u.thinking_tokens || 0;
    totCache += u.cache_read_tokens || 0;
    totAll += u.total_tokens || 0;
  }
  $("m-tokens").textContent = fmtNum(totAll);
  $("m-tokens-sub").textContent = `in: ${fmtNum(totIn)} | out: ${fmtNum(totOut)} | think: ${fmtNum(totThink)}`;
  $("m-cache").textContent = `cache read: ${fmtNum(totCache)} tok`;

  if (doctor.memory_used_mb != null && doctor.memory_total_mb != null) {
    $("m-memory").textContent = `${doctor.memory_used_mb} / ${doctor.memory_total_mb} MB`;
  } else {
    $("m-memory").textContent = "N/A";
  }

  if (!doctor.harness_ready) {
    $("readiness-notice").hidden = false;
    $("readiness-notice").textContent = `Peringatan: Harness CLI '${doctor.harness}' belum terdeteksi siap.`;
  } else {
    $("readiness-notice").hidden = true;
  }

  renderProjects(projects, jobs);
  renderAllJobs(jobs);
  renderEvents(events);

  if (currentTab === "overview") showView("overview-view");
  else if (currentTab === "jobs") showView("jobs-view");
  else if (currentTab === "events") showView("events-view");
  else if (currentTab === "detail" && selectedProjectId) {
    showView("detail-view");
    await loadProjectDetail(selectedProjectId);
  }
}

function renderProjects(projects, jobs) {
  const grid = $("project-grid");
  if (!projects.length) {
    grid.innerHTML = '<div class="card" style="color:var(--muted);">Belum ada proyek. Klik <strong>+ Proyek baru</strong> untuk memulai build.</div>';
    return;
  }
  grid.innerHTML = projects.map((p) => {
    const projJobs = jobs.filter((j) => j.project_id === p.id);
    const tok = projJobs.reduce((acc, j) => acc + ((j.token_usage && j.token_usage.total_tokens) || 0), 0);
    return `
      <div class="card proj-card" onclick="window.openProject('${esc(p.id)}')">
        <div class="proj-top">
          <span class="proj-name">${esc(p.name || p.id)}</span>
          <span class="badge ${esc(p.state)}">${esc(p.state)}</span>
        </div>
        <div class="mono" style="color:var(--muted);">${esc(p.profile)} · ${projJobs.length} job · ${fmtNum(tok)} tok</div>
      </div>
    `;
  }).join("");
}

window.openProject = async (projectId) => {
  selectedProjectId = projectId;
  selectedJobId = null;
  setActiveNav("detail");
  showView("detail-view");
  await loadProjectDetail(projectId);
};

async function loadProjectDetail(projectId) {
  const proj = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`);
  $("d-profile").textContent = proj.profile || "static";
  $("d-name").textContent = proj.name || proj.id;
  $("d-brief").textContent = proj.brief || "";
  $("d-state").className = `badge ${esc(proj.state)}`;
  $("d-state").textContent = proj.state;

  const jobs = proj.recent_jobs || [];
  const latest = jobs[0];
  if (!selectedJobId && latest) selectedJobId = latest.id;

  const isBuilt = proj.state === "built" || (latest && latest.state === "succeeded");
  const isRunning = latest && (latest.state === "running" || latest.state === "queued");
  const stages = [
    { name: "1. Plan", status: proj.plan ? "done" : isRunning ? "active" : "-" },
    { name: "2. Build Tasks", status: isBuilt ? "done" : isRunning ? "active" : "-" },
    { name: "3. Verify", status: isBuilt ? "done" : "-" },
    { name: "4. Browser Test", status: isBuilt ? "done" : "-" },
    { name: "5. Release", status: proj.artifact ? "done" : "-" },
  ];
  $("d-pipeline").innerHTML = stages.map((s) => `
    <div class="stage ${s.status === "done" ? "done" : s.status === "active" ? "active" : ""}">
      <strong>${s.name}</strong>
      <span>${s.status === "done" ? "selesai" : s.status === "active" ? "berjalan" : "menunggu"}</span>
    </div>
  `).join("");

  $("d-jobs").innerHTML = jobs.length
    ? jobs.map((j) => `
        <div class="row clickable" onclick="window.inspectJob('${esc(j.id)}')">
          <div class="mono">${esc(j.id.slice(0, 12))}</div>
          <span class="badge ${esc(j.state)}">${esc(j.state)}</span>
        </div>
      `).join("")
    : '<div class="mono" style="color:var(--muted);">Belum ada job.</div>';

  if (selectedJobId) await inspectJob(selectedJobId);

  $("d-rebuild-btn").onclick = async () => {
    const res = await apiFetch("/api/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product: proj.id, profile: proj.profile || "static", brief: proj.brief }),
    });
    selectedJobId = res.job_id;
    await refreshWorkspace();
  };
}

$("adjust-preview-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedProjectId) return;
  const instruction = $("adj-instruction").value.trim();
  if (!instruction) return;
  $("adj-status").textContent = "Menganalisis scope domain...";
  $("adj-preview-btn").disabled = true;
  try {
    const data = await apiFetch(`/api/projects/${encodeURIComponent(selectedProjectId)}/adjust/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    lastAdjustPreview = { instruction, candidates: data.candidates || [] };
    $("adj-candidates").innerHTML = lastAdjustPreview.candidates.map((c, idx) => `
      <label class="row" style="align-items:flex-start;cursor:pointer;">
        <input type="checkbox" class="adj-cand-cb" data-idx="${idx}" ${idx === 0 ? "checked" : ""} style="margin-top:4px;" />
        <div style="flex:1;min-width:0;">
          <div style="font-weight:600;">${esc(c.domain_path)}</div>
          <div class="mono" style="color:var(--muted);">${esc(c.reason)}</div>
          ${c.target_files && c.target_files.length ? `<div class="mono" style="color:var(--accent-hover);margin-top:2px;">Files: ${esc(c.target_files.join(", "))}</div>` : ""}
        </div>
      </label>
    `).join("");
    $("adj-scope-box").hidden = false;
    $("adj-status").textContent = `${lastAdjustPreview.candidates.length} kandidat domain ditemukan.`;
  } catch (err) {
    $("adj-status").textContent = `Gagal cek scope: ${err.message}`;
  } finally {
    $("adj-preview-btn").disabled = false;
  }
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
      for (const f of (cand.target_files || [])) {
        if (!targetFiles.includes(f)) targetFiles.push(f);
      }
    }
  });
  if (!selectedDomains.length) {
    $("adj-status").textContent = "Pilih minimal 1 domain scope.";
    return;
  }
  $("adj-confirm-btn").disabled = true;
  $("adj-status").textContent = "Menjalankan adjust build...";
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
    $("adj-status").textContent = `Job adjust dimulai: ${res.job_id.slice(0, 12)}`;
    selectedJobId = res.job_id;
    await refreshWorkspace();
  } catch (err) {
    $("adj-status").textContent = `Gagal memulai adjust: ${err.message}`;
  } finally {
    $("adj-confirm-btn").disabled = false;
  }
});

window.inspectJob = async (jobId) => {
  selectedJobId = jobId;
  const j = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  const u = (j.result && j.result.token_usage) || {};
  const tasksHtml = (j.tasks || []).map((t) => `
    <div class="row" style="padding:6px 10px;">
      <span class="mono">${esc(t.id)}</span>
      <span class="badge ${esc(t.state)}">${esc(t.state)}</span>
    </div>
  `).join("");

  const btns = [];
  if (j.state === "running" || j.state === "queued") {
    btns.push(`<button class="danger" onclick="window.cancelJob('${esc(j.id)}')">Batalkan Job</button>`);
  }
  if (j.result && j.result.artifact_path) {
    btns.push(`<button class="primary" onclick="window.downloadArtifact('${esc(j.project_id)}','${esc(j.id)}')">Download .tar.gz</button>`);
  }

  $("d-job-panel").innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span class="mono">${esc(j.id)}</span>
      <span class="badge ${esc(j.state)}">${esc(j.state)}</span>
    </div>
    <div class="mono">Token: total=${fmtNum(u.total_tokens)} (in=${fmtNum(u.input_tokens)}, out=${fmtNum(u.output_tokens)}, think=${fmtNum(u.thinking_tokens)}, cache=${fmtNum(u.cache_read_tokens)})</div>
    ${j.error ? `<div class="mono" style="color:var(--err);">Error: ${esc(j.error)}</div>` : ""}
    <div style="display:flex;gap:8px;flex-wrap:wrap;">${btns.join("")}</div>
    <div style="display:grid;gap:6px;">
      <strong style="font-size:0.86rem;">Tasks (${(j.tasks || []).length})</strong>
      ${tasksHtml || '<span class="mono" style="color:var(--muted);">Belum ada task tercatat.</span>'}
    </div>
  `;
};

function renderAllJobs(jobs) {
  $("all-jobs-list").innerHTML = jobs.length
    ? jobs.map((j) => {
        const u = j.token_usage || {};
        return `
          <div class="row clickable" onclick="window.openProject('${esc(j.project_id)}')">
            <div>
              <div style="font-weight:600;">${esc(j.project_id)}</div>
              <div class="mono" style="color:var(--muted);">${esc(j.id.slice(0, 12))} · total ${fmtNum(u.total_tokens)} tok (in ${fmtNum(u.input_tokens)} / out ${fmtNum(u.output_tokens)})</div>
            </div>
            <span class="badge ${esc(j.state)}">${esc(j.state)}</span>
          </div>
        `;
      }).join("")
    : '<div class="card mono" style="color:var(--muted);">Belum ada job.</div>';
}

function renderEvents(events) {
  $("events-list").innerHTML = events.length
    ? events.map((e) => `
        <div class="row">
          <span class="mono">#${e.seq} ${esc(e.kind)} · ${esc(e.project_id || "-")}</span>
          <span class="mono" style="color:var(--muted);">${new Date(e.created * 1000).toLocaleTimeString("id-ID")}</span>
        </div>
      `).join("")
    : '<div class="card mono" style="color:var(--muted);">Belum ada event.</div>';
}

window.cancelJob = async (jobId) => {
  await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  await refreshWorkspace();
};

window.downloadArtifact = async (projectId, jobId) => {
  const res = await fetch(`/api/artifacts/${encodeURIComponent(projectId)}/${encodeURIComponent(jobId)}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) return alert("Gagal mengunduh artifact");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${projectId}-${jobId.slice(0, 8)}.tar.gz`;
  a.click();
  URL.revokeObjectURL(url);
};

document.querySelectorAll(".nav").forEach((btn) => {
  btn.addEventListener("click", () => {
    setActiveNav(btn.dataset.tab);
    refreshWorkspace();
  });
});

$("connect-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  setToken($("token-input").value);
  await refreshWorkspace();
});

$("logout-btn").addEventListener("click", () => {
  clearToken();
  $("token-input").value = "";
  refreshWorkspace();
});

$("refresh-btn").addEventListener("click", refreshWorkspace);
$("back-btn").addEventListener("click", () => {
  setActiveNav("overview");
  refreshWorkspace();
});

$("new-project-btn").addEventListener("click", () => { $("build-modal").hidden = false; });
$("close-modal-btn").addEventListener("click", () => { $("build-modal").hidden = true; });
$("cancel-modal-btn").addEventListener("click", () => { $("build-modal").hidden = true; });

$("build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await apiFetch("/api/build", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      product: $("f-product").value.trim() || null,
      profile: $("f-profile").value,
      brief: $("f-brief").value.trim(),
    }),
  });
  $("build-modal").hidden = true;
  $("f-brief").value = "";
  $("f-product").value = "";
  await window.openProject(res.project_id);
  selectedJobId = res.job_id;
  await refreshWorkspace();
});

refreshWorkspace();
pollTimer = setInterval(refreshWorkspace, 6000);
