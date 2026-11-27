/* ==========================================================================
   ACP Software Factory — Bilingual Translation Dictionary (ID / EN)
   ========================================================================== */

(function () {
  const FACTORY_I18N = {
    id: {
      // Navigation & Topbar
      "nav.workspace": "WORKSPACE",
      "nav.projects": "Proyek",
      "nav.jobs": "Jobs & Telemetri",
      "nav.events": "Event Ledger",
      "nav.crumb_root": "Workspace",
      "top.client_view": "Client View",
      "top.operator_view": "Operator View",
      "top.live": "Live",
      "top.paused": "Dijeda",
      "top.refresh": "Refresh",
      "top.disconnect": "Putuskan",
      "top.connected": "Terhubung",
      "top.disconnected": "Belum terhubung",
      "top.backend_online": "Backend Online",
      "top.backend_offline": "Offline",
      "top.connecting": "Menghubungkan...",
      "top.branding_title": "Pengaturan Branding White-Label",

      // Login / Welcome
      "login.eyebrow": "AUTONOMOUS SOFTWARE FACTORY",
      "login.title": "Dari brief produk ke rilis terverifikasi, secara otonom.",
      "login.sub":
        "Orkestrasi perencanaan arsitektur DAG, eksekusi worker paralel di Git worktree terisolasi, review diff otomatis, hingga pengujian Playwright dan pengemasan rilis dalam satu ruang kerja.",
      "login.p1_title": "DAG Task Planner",
      "login.p1_desc": "Dekomposisi brief menjadi graf dependensi & kriteria penerimaan terstruktur.",
      "login.p2_title": "Isolated Git Worktrees",
      "login.p2_desc": "Setiap worker berjalan di cabang terisolasi dengan wave barrier & auto-repair.",
      "login.p3_title": "ReviewGuard & Secret Gate",
      "login.p3_desc": "Audit diff objektif dan pemindaian kebocoran kredensial sebelum rilis.",
      "login.p4_title": "Playwright & Release Bundle",
      "login.p4_desc": "Uji alur API & browser otomatis, lalu kemas .tar.gz ber-SHA256.",
      "login.auth_eyebrow": "OTENTIKASI OPERATOR",
      "login.auth_title": "Masuk ke Workspace",
      "login.auth_sub":
        "Gunakan Operator Token dari data/.operator_token untuk mengakses orkestrator, memantau worker, dan mengunduh artifak rilis.",
      "login.token_label": "Operator Token",
      "login.token_placeholder": "Tempel operator token Anda...",
      "login.show": "Tampil",
      "login.hide": "Sembunyi",
      "login.submit": "Hubungkan ke Factory",

      // Overview
      "ov.eyebrow": "PORTFOLIO PRODUK",
      "ov.title": "Proyek & Rilis",
      "ov.sub": "Kelola siklus hidup build otonom, revisi inkremental, dan artifak terverifikasi.",
      "ov.new_project": "Proyek Baru",
      "ov.kpi_projects": "TOTAL PROYEK",
      "ov.kpi_jobs": "WORKER BUILD",
      "ov.kpi_tokens": "KONSUMSI TOKEN",
      "ov.kpi_memory": "MEMORI HOST",
      "ov.kpi_releases": "RILIS TERVERIFIKASI",
      "ov.kpi_releases_sub": "siap preview & unduh",
      "ov.kpi_success_rate": "TINGKAT KEBERHASILAN",
      "ov.kpi_success_sub": "terverifikasi otomatis",
      "ov.search_placeholder": "Cari nama proyek, profile, atau brief...",
      "ov.filter_all": "Semua",
      "ov.filter_built": "Built",
      "ov.filter_building": "Building",
      "ov.filter_other": "Lainnya",
      "ov.empty_title": "Belum ada proyek di workspace ini",
      "ov.empty_desc": "Klik tombol + Proyek Baru untuk membuat build otonom pertama Anda.",
      "ov.no_match": "Tidak ada proyek yang cocok",
      "ov.no_match_desc": "Coba ubah kata kunci pencarian atau pilih filter status lainnya.",

      // Detail Hero & Pipeline
      "det.back": "Kembali ke Semua Proyek",
      "det.preview": "Live Preview",
      "det.edit": "Edit Brief",
      "det.download": "Unduh Rilis (.tar.gz)",
      "det.rebuild": "Build Ulang",
      "det.delete": "Hapus",
      "det.pipeline_label": "DELIVERY PIPELINE",
      "det.tab_overview": "Ringkasan & Plan DAG",
      "det.tab_jobs": "Jobs & Telemetri",
      "det.tab_code": "Kode & Hasil Tes",
      "det.tab_adjust": "Revisi / Adjust",

      // Pipeline stages
      "stage.1": "DAG Plan",
      "stage.2": "Build & Review",
      "stage.3": "Verify & Secret Gate",
      "stage.4": "Acceptance Test",
      "stage.5": "Release Bundle",
      "stage.done": "selesai",
      "stage.active": "berjalan",
      "stage.failed": "gagal",
      "stage.idle": "menunggu",
      "stage.skipped": "dilewati",

      // Plan & Tasks
      "plan.eyebrow": "ARSITEKTUR & EKSEKUSI",
      "plan.title": "Plan DAG per Gelombang (Waves)",
      "plan.flows_title": "Acceptance Test Flows",
      "plan.empty": "Rencana arsitektur DAG akan tampil di sini setelah tahap perencanaan selesai.",

      // Artifact
      "art.eyebrow": "VERIFIED RELEASE ARTIFACT",
      "art.title": "Paket rilis siap didistribusikan",
      "art.download": "Unduh .tar.gz",

      // Adjust
      "adj.eyebrow": "INCREMENTAL DELIVERY",
      "adj.title": "Revisi / Tambah Fitur Setelah Build",
      "adj.badge": "2-Step Domain Guard",
      "adj.placeholder":
        "Jelaskan perubahan atau fitur tambahan, misal: Tambahkan tombol export CSV pada tabel transaksi dan filter tanggal...",
      "adj.analyze": "1. Analisis Scope Domain",
      "adj.confirm_title": "2. Konfirmasi Modul & File Target",
      "adj.confirm_sub": "Pilih satu atau lebih kandidat domain sebelum worker mulai bekerja",
      "adj.cancel": "Batal",
      "adj.run": "Konfirmasi & Jalankan Adjust →",

      // Jobs & Inspector
      "job.history": "Riwayat Job",
      "job.inspector": "Inspektor Job",
      "job.select_hint":
        "Pilih salah satu job di sebelah kiri untuk melihat telemetri token, status task, dan artifak.",
      "job.cancel": "Batalkan Job",
      "job.tasks_title": "Tasks Eksekusi",
      "jobs.eyebrow": "TELEMETRI & ARTIFAK",
      "jobs.title": "Jobs & Akumulasi Token",
      "jobs.sub": "Pantau seluruh antrean build, durasi eksekusi, dan distribusi penggunaan token.",
      "jobs.search_placeholder": "Cari project ID atau job ID...",
      "jobs.filter_active": "Aktif",
      "jobs.filter_succeeded": "Sukses",
      "jobs.filter_failed": "Gagal",

      // Code & Test Report
      "code.tests_eyebrow": "VERIFIKASI & ACCEPTANCE",
      "code.tests_title": "Laporan Hasil Pengujian",
      "code.files_eyebrow": "SOURCE REPOSITORY",
      "code.files_title": "Penjelajah Kode Produk",
      "code.files_search": "Filter nama file...",
      "code.copy_file": "Salin Kode",
      "code.select_file": "Pilih file di sebelah kiri untuk melihat isi kode sumber.",
      "code.no_files": "Belum ada file kode pada proyek ini.",
      "code.no_tests": "Belum ada laporan pengujian (jalankan build hingga selesai terlebih dahulu).",

      // Events
      "evt.eyebrow": "AUDIT TRAIL",
      "evt.title": "Event Ledger",
      "evt.sub": "Catatan kronologis setiap peristiwa perencanaan, eksekusi worker, dan rilis.",
      "evt.search_placeholder": "Filter jenis event atau project ID...",
      "evt.cat_all": "Semua",
      "evt.cat_build": "Build",
      "evt.cat_plan": "Plan",
      "evt.cat_task": "Worker/Task",
      "evt.cat_error": "Error",
      "evt.export_json": "Export JSON",
      "evt.export_csv": "Export CSV",

      // Modals
      "mod.new_eyebrow": "NEW AUTONOMOUS BUILD",
      "mod.new_title": "Buat & Bangun Proyek Baru",
      "mod.preset_label": "Contoh Cepat (Demo):",
      "mod.product_name": "Nama Produk",
      "mod.optional": "(opsional — otomatis jika dikosongkan)",
      "mod.arch_profile": "Arsitektur Profile",
      "mod.harness": "ACP Harness",
      "mod.brief": "Brief Produk",
      "mod.cancel": "Batal",
      "mod.start_build": "Mulai Autonomous Build",

      "mod.edit_eyebrow": "EDIT PROYEK",
      "mod.edit_title": "Perbarui Detail & Brief Proyek",
      "mod.save": "Simpan Perubahan",

      "mod.del_eyebrow": "KONFIRMASI PENGHAPUSAN",
      "mod.del_title": "Hapus Proyek Secara Permanen?",
      "mod.del_desc":
        "Seluruh riwayat job, task, event, repositori Git produk, dan artifak unduhan untuk proyek ini akan dihapus.",
      "mod.del_confirm": "Ya, Hapus Proyek",

      "mod.brand_eyebrow": "WHITE-LABEL STUDIO",
      "mod.brand_title": "Pengaturan Branding & Tampilan Klien",
      "mod.brand_name": "Nama Brand / Studio",
      "mod.brand_sub": "Subjudul Studio",
      "mod.client_label": "Label Klien (Opsional, tampil di topbar)",
      "mod.accent_color": "Warna Aksen Utama",
      "mod.default_lang": "Bahasa Default",
      "mod.brand_save": "Simpan Branding",

      "mod.preview_reload": "Muat Ulang",
      "mod.preview_newtab": "Tab Baru",
      "mod.preview_stop": "Hentikan Server",

      "mod.task_eyebrow": "DETAIL TASK DAG",
      "mod.task_deps": "Dependensi",
      "mod.task_skills": "Design Skills",
      "mod.task_instructions": "Instruksi Worker",
      "mod.task_acceptance": "Kriteria Penerimaan",
    },

    en: {
      // Navigation & Topbar
      "nav.workspace": "WORKSPACE",
      "nav.projects": "Projects",
      "nav.jobs": "Jobs & Telemetry",
      "nav.events": "Event Ledger",
      "nav.crumb_root": "Workspace",
      "top.client_view": "Client View",
      "top.operator_view": "Operator View",
      "top.live": "Live",
      "top.paused": "Paused",
      "top.refresh": "Refresh",
      "top.disconnect": "Disconnect",
      "top.connected": "Connected",
      "top.disconnected": "Disconnected",
      "top.backend_online": "Backend Online",
      "top.backend_offline": "Offline",
      "top.connecting": "Connecting...",
      "top.branding_title": "White-Label Branding Settings",

      // Login / Welcome
      "login.eyebrow": "AUTONOMOUS SOFTWARE FACTORY",
      "login.title": "From product brief to verified release, autonomously.",
      "login.sub":
        "Orchestrate DAG architecture planning, parallel execution inside isolated Git worktrees, automated diff review, Playwright verification, and release packaging in one studio.",
      "login.p1_title": "DAG Task Planner",
      "login.p1_desc": "Decompose product briefs into dependency graphs with strict acceptance criteria.",
      "login.p2_title": "Isolated Git Worktrees",
      "login.p2_desc": "Every worker runs in an isolated branch with wave barriers and auto-repair.",
      "login.p3_title": "ReviewGuard & Secret Gate",
      "login.p3_desc": "Objective diff auditing and secret leak scanning before any release.",
      "login.p4_title": "Playwright & Release Bundle",
      "login.p4_desc": "Automated API & browser acceptance checks, packaged into SHA-256 tarballs.",
      "login.auth_eyebrow": "OPERATOR AUTHENTICATION",
      "login.auth_title": "Sign In to Workspace",
      "login.auth_sub":
        "Enter the Operator Token from data/.operator_token to manage builds, inspect workers, and download verified artifacts.",
      "login.token_label": "Operator Token",
      "login.token_placeholder": "Paste your operator token...",
      "login.show": "Show",
      "login.hide": "Hide",
      "login.submit": "Connect to Factory",

      // Overview
      "ov.eyebrow": "PRODUCT PORTFOLIO",
      "ov.title": "Projects & Releases",
      "ov.sub": "Manage autonomous build lifecycles, incremental adjustments, and verified releases.",
      "ov.new_project": "New Project",
      "ov.kpi_projects": "TOTAL PROJECTS",
      "ov.kpi_jobs": "WORKER BUILDS",
      "ov.kpi_tokens": "TOKEN CONSUMPTION",
      "ov.kpi_memory": "HOST MEMORY",
      "ov.kpi_releases": "VERIFIED RELEASES",
      "ov.kpi_releases_sub": "ready for preview & download",
      "ov.kpi_success_rate": "DELIVERY SUCCESS RATE",
      "ov.kpi_success_sub": "automatically verified",
      "ov.search_placeholder": "Search project name, profile, or brief...",
      "ov.filter_all": "All",
      "ov.filter_built": "Built",
      "ov.filter_building": "Building",
      "ov.filter_other": "Other",
      "ov.empty_title": "No projects in this workspace yet",
      "ov.empty_desc": "Click + New Project to kick off your first autonomous build.",
      "ov.no_match": "No matching projects",
      "ov.no_match_desc": "Try adjusting your search query or status filter.",

      // Detail Hero & Pipeline
      "det.back": "Back to All Projects",
      "det.preview": "Live Preview",
      "det.edit": "Edit Brief",
      "det.download": "Download Release (.tar.gz)",
      "det.rebuild": "Rebuild",
      "det.delete": "Delete",
      "det.pipeline_label": "DELIVERY PIPELINE",
      "det.tab_overview": "Overview & DAG Plan",
      "det.tab_jobs": "Jobs & Telemetry",
      "det.tab_code": "Code & Test Report",
      "det.tab_adjust": "Adjust / Revise",

      // Pipeline stages
      "stage.1": "DAG Plan",
      "stage.2": "Build & Review",
      "stage.3": "Verify & Secret Gate",
      "stage.4": "Acceptance Test",
      "stage.5": "Release Bundle",
      "stage.done": "completed",
      "stage.active": "running",
      "stage.failed": "failed",
      "stage.idle": "pending",
      "stage.skipped": "skipped",

      // Plan & Tasks
      "plan.eyebrow": "ARCHITECTURE & EXECUTION",
      "plan.title": "Wave-Grouped Plan DAG",
      "plan.flows_title": "Acceptance Test Flows",
      "plan.empty": "The DAG architecture plan will appear here once planning completes.",

      // Artifact
      "art.eyebrow": "VERIFIED RELEASE ARTIFACT",
      "art.title": "Verified release bundle ready for delivery",
      "art.download": "Download .tar.gz",

      // Adjust
      "adj.eyebrow": "INCREMENTAL DELIVERY",
      "adj.title": "Revise / Add Features After Build",
      "adj.badge": "2-Step Domain Guard",
      "adj.placeholder":
        "Describe your requested change, e.g. Add CSV export button on the transactions table with date range filters...",
      "adj.analyze": "1. Analyze Domain Scope",
      "adj.confirm_title": "2. Confirm Affected Modules & Target Files",
      "adj.confirm_sub": "Select one or more domain candidates before workers begin editing",
      "adj.cancel": "Cancel",
      "adj.run": "Confirm & Run Adjust →",

      // Jobs & Inspector
      "job.history": "Job History",
      "job.inspector": "Job Inspector",
      "job.select_hint":
        "Select a job on the left to inspect token telemetry, task execution states, and artifacts.",
      "job.cancel": "Cancel Job",
      "job.tasks_title": "Execution Tasks",
      "jobs.eyebrow": "TELEMETRY & ARTIFACTS",
      "jobs.title": "Jobs & Token Accumulation",
      "jobs.sub": "Monitor build queues, execution durations, and token usage distribution.",
      "jobs.search_placeholder": "Search project ID or job ID...",
      "jobs.filter_active": "Active",
      "jobs.filter_succeeded": "Succeeded",
      "jobs.filter_failed": "Failed",

      // Code & Test Report
      "code.tests_eyebrow": "VERIFICATION & ACCEPTANCE",
      "code.tests_title": "Test Execution Report",
      "code.files_eyebrow": "SOURCE REPOSITORY",
      "code.files_title": "Product File Explorer",
      "code.files_search": "Filter filenames...",
      "code.copy_file": "Copy Code",
      "code.select_file": "Select a file on the left to inspect its source code.",
      "code.no_files": "No generated source files found for this project yet.",
      "code.no_tests": "No test results recorded yet (complete a build first).",

      // Events
      "evt.eyebrow": "AUDIT TRAIL",
      "evt.title": "Event Ledger",
      "evt.sub": "Chronological audit trail of planning, parallel worker execution, and release milestones.",
      "evt.search_placeholder": "Filter event kind or project ID...",
      "evt.cat_all": "All",
      "evt.cat_build": "Build",
      "evt.cat_plan": "Plan",
      "evt.cat_task": "Worker/Task",
      "evt.cat_error": "Error",
      "evt.export_json": "Export JSON",
      "evt.export_csv": "Export CSV",

      // Modals
      "mod.new_eyebrow": "NEW AUTONOMOUS BUILD",
      "mod.new_title": "Create & Build New Project",
      "mod.preset_label": "Quick Demo Templates:",
      "mod.product_name": "Product Name",
      "mod.optional": "(optional — auto-generated if left blank)",
      "mod.arch_profile": "Architecture Profile",
      "mod.harness": "ACP Harness",
      "mod.brief": "Product Brief",
      "mod.cancel": "Cancel",
      "mod.start_build": "Start Autonomous Build",

      "mod.edit_eyebrow": "EDIT PROJECT",
      "mod.edit_title": "Update Project Details & Brief",
      "mod.save": "Save Changes",

      "mod.del_eyebrow": "CONFIRM DELETION",
      "mod.del_title": "Permanently Delete Project?",
      "mod.del_desc":
        "All job history, tasks, events, product Git repository files, and release artifacts for this project will be removed.",
      "mod.del_confirm": "Yes, Delete Project",

      "mod.brand_eyebrow": "WHITE-LABEL STUDIO",
      "mod.brand_title": "Client Branding & Appearance",
      "mod.brand_name": "Studio / Brand Name",
      "mod.brand_sub": "Studio Subtitle",
      "mod.client_label": "Client Badge (Optional, shown in topbar)",
      "mod.accent_color": "Primary Accent Color",
      "mod.default_lang": "Default Language",
      "mod.brand_save": "Save Branding",

      "mod.preview_reload": "Reload",
      "mod.preview_newtab": "New Tab",
      "mod.preview_stop": "Stop Server",

      "mod.task_eyebrow": "DAG TASK DETAIL",
      "mod.task_deps": "Dependencies",
      "mod.task_skills": "Design Skills",
      "mod.task_instructions": "Worker Instructions",
      "mod.task_acceptance": "Acceptance Criteria",
    },
  };

  window.FACTORY_I18N = FACTORY_I18N;
  window.getFactoryLang = () => localStorage.getItem("acp_factory_lang") || "id";
  window.t = (key) => {
    const lang = window.getFactoryLang();
    return (FACTORY_I18N[lang] && FACTORY_I18N[lang][key]) || FACTORY_I18N.id[key] || key;
  };
})();
