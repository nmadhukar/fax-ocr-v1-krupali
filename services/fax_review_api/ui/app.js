(function () {
  "use strict";

  const STORAGE_KEY = "fax_ui_profile_v1";
  const THEME_KEY = "fax_ui_theme";

  const DEFAULT_PROFILE = {
    ingressBaseUrl: "http://localhost:8001",
    reviewBaseUrl: "http://localhost:8002",
    queryBaseUrl: "http://localhost:8003",
    token: "",
    reviewerId: "",
  };

  const state = {
    profile: { ...DEFAULT_PROFILE },
    reviewPacket: null,
    jobsPage: 0,
    jobsLimit: 25,
    jobsTotal: 0,
    pollingTimer: null,
    dashCharts: {},
  };

  const $ = (id) => document.getElementById(id);

  /* ════════════════════════════════════════
     INIT
     ════════════════════════════════════════ */
  function init() {
    hydrateProfile();
    applyProfileToInputs();
    initTheme();
    bindTabs();
    bindConfigPanel();
    bindWorkflow();
    bindTemplates();
    bindIntelligence();
    bindApiConsole();
    bindLightbox();
    bindDashboard();
    animateReveals();
    syncStatusPills();
    addActivity("UI initialized.", "info");
  }

  function animateReveals() {
    document.querySelectorAll(".reveal").forEach((n, i) => {
      n.style.animationDelay = `${Math.min(i * 70, 450)}ms`;
    });
  }

  /* ════════════════════════════════════════
     DARK MODE
     ════════════════════════════════════════ */
  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "dark") applyTheme("dark");
    else applyTheme("light");
    $("themeToggle").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      localStorage.setItem(THEME_KEY, next);
    });
  }

  function applyTheme(mode) {
    document.documentElement.dataset.theme = mode;
    $("themeIcon").textContent = mode === "dark" ? "☀️" : "🌙";
    $("themeLabel").textContent = mode === "dark" ? "Light Mode" : "Dark Mode";
  }

  /* ════════════════════════════════════════
     TABS
     ════════════════════════════════════════ */
  function bindTabs() {
    const btns = Array.from(document.querySelectorAll(".tab[role='tab']"));
    btns.forEach((b) => {
      b.addEventListener("click", () => {
        btns.forEach((x) => {
          const on = x === b;
          x.classList.toggle("active", on);
          x.setAttribute("aria-selected", String(on));
        });
        document.querySelectorAll(".tab-panel").forEach((p) => {
          p.classList.toggle("active", p.id === `tab-${b.dataset.tab}`);
        });
      });
    });
  }

  /* ════════════════════════════════════════
     CONFIG PANEL
     ════════════════════════════════════════ */
  function bindConfigPanel() {
    $("saveConfigBtn").addEventListener("click", () => {
      saveProfileFromInputs();
      toast("Profile saved.", "success");
      addActivity("Connection profile saved.", "info");
    });
    $("resetConfigBtn").addEventListener("click", () => {
      state.profile = { ...DEFAULT_PROFILE };
      applyProfileToInputs();
      persistProfile();
      syncStatusPills();
      toast("Profile reset to defaults.", "info");
    });
    $("testHealthBtn").addEventListener("click", async (e) => {
      await withButtonBusy(e.currentTarget, "Testing...", async () => {
        saveProfileFromInputs();
        const res = await Promise.allSettled([
          apiRequest("ingress", "/health", { method: "GET" }),
          apiRequest("review", "/health", { method: "GET" }),
          apiRequest("query", "/health", { method: "GET" }),
        ]);
        const s = res.map((r, i) => {
          const name = ["ingress", "review", "query"][i];
          return r.status === "fulfilled" ? `${name}=OK` : `${name}=FAIL`;
        });
        addActivity(`Health: ${s.join(", ")}`, "info");
        toast("Health check completed.", "info");
      });
    });
  }

  /* ════════════════════════════════════════
     FORM VALIDATION
     ════════════════════════════════════════ */
  function validateField(inputId, errorId) {
    const input = $(inputId);
    const err = $(errorId);
    if (!input || !err) return true;
    if (!input.value.trim() && input.required) {
      input.classList.add("invalid");
      err.classList.add("visible");
      return false;
    }
    input.classList.remove("invalid");
    err.classList.remove("visible");
    return true;
  }

  function clearValidation(inputId, errorId) {
    const input = $(inputId);
    const err = $(errorId);
    if (input) input.addEventListener("input", () => {
      input.classList.remove("invalid");
      if (err) err.classList.remove("visible");
    });
  }

  function initValidationListeners() {
    clearValidation("uploadFile", "uploadFileError");
    clearValidation("uploadTenant", "uploadTenantError");
    clearValidation("inspectJobId", "inspectJobIdError");
    clearValidation("tplName", "tplNameError");
    clearValidation("tplPayer", "tplPayerError");
    clearValidation("tplDocType", "tplDocTypeError");
    clearValidation("queryText", "queryTextError");
  }

  /* ════════════════════════════════════════
     LIGHTBOX
     ════════════════════════════════════════ */
  function bindLightbox() {
    const overlay = $("lightboxOverlay");
    const img = $("lightboxImg");
    const caption = $("lightboxCaption");
    const close = () => overlay.classList.remove("active");

    $("lightboxClose").addEventListener("click", close);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

    window.openLightbox = function (src, text) {
      img.src = src;
      caption.textContent = text || "";
      overlay.classList.add("active");
    };
  }

  /* ════════════════════════════════════════
     DASHBOARD
     ════════════════════════════════════════ */
  function bindDashboard() {
    $("dashRefreshBtn").addEventListener("click", async (e) => {
      await withButtonBusy(e.currentTarget, "Loading...", async () => {
        saveProfileFromInputs();
        const days = Number($("dashDays").value || 30);
        const [qualityRes, payersRes, feedbackRes] = await Promise.allSettled([
          apiRequest("review", "/v1/analytics/quality", { method: "GET", query: { days } }),
          apiRequest("review", "/v1/analytics/payers", { method: "GET", query: { days } }),
          apiRequest("review", "/v1/analytics/feedback-summary", { method: "GET", query: { days } }),
        ]);

        const quality = qualityRes.status === "fulfilled" ? qualityRes.value.data : null;
        const payers = payersRes.status === "fulfilled" ? payersRes.value.data : null;
        const feedback = feedbackRes.status === "fulfilled" ? feedbackRes.value.data : null;

        renderDashboardStats(quality, payers, feedback);
        renderDashboardCharts(quality, payers, feedback);
        addActivity("Dashboard refreshed.", "info");
        toast("Dashboard loaded.", "success");
      });
    });
  }

  function renderDashboardStats(quality, payers, feedback) {
    const stats = $("dashStats");
    const items = [];
    if (quality) {
      items.push({ value: quality.total_faxes ?? quality.total_jobs ?? "-", label: "Total Faxes" });
      items.push({ value: quality.avg_confidence != null ? (quality.avg_confidence * 100).toFixed(1) + "%" : "-", label: "Avg Confidence" });
      items.push({ value: quality.review_rate != null ? (quality.review_rate * 100).toFixed(1) + "%" : "-", label: "Review Rate" });
      items.push({ value: quality.avg_processing_time_s != null ? quality.avg_processing_time_s.toFixed(1) + "s" : "-", label: "Avg Process Time" });
    }
    if (feedback) {
      items.push({ value: feedback.total_corrections ?? "-", label: "Total Corrections" });
      items.push({ value: feedback.correction_rate != null ? (feedback.correction_rate * 100).toFixed(1) + "%" : "-", label: "Correction Rate" });
    }
    if (payers && Array.isArray(payers)) {
      items.push({ value: payers.length, label: "Active Payers" });
    }
    if (items.length === 0) {
      items.push({ value: "—", label: "No data available" });
    }
    stats.innerHTML = items.map((s) => `
      <div class="stat-card">
        <div class="stat-value">${escapeHtml(String(s.value))}</div>
        <div class="stat-label">${escapeHtml(s.label)}</div>
      </div>
    `).join("");
  }

  function renderDashboardCharts(quality, payers, feedback) {
    const row1 = $("dashCharts");
    const row2 = $("dashChartsRow2");
    if (!window.Chart) { row1.style.display = "none"; row2.style.display = "none"; return; }
    row1.style.display = "grid";
    row2.style.display = "grid";
    const isDark = document.documentElement.dataset.theme === "dark";
    const textColor = isDark ? "#a0bcc9" : "#4b6470";
    const gridColor = isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)";
    const defaults = { color: textColor, borderColor: gridColor };
    Chart.defaults.color = defaults.color;

    // Destroy previous charts
    Object.values(state.dashCharts).forEach((c) => c.destroy && c.destroy());
    state.dashCharts = {};

    // Chart 1: Volume (mock timeline from quality data)
    const volCtx = $("chartVolume").getContext("2d");
    const volLabels = quality?.daily_volumes?.map((d) => d.date) || ["Today"];
    const volData = quality?.daily_volumes?.map((d) => d.count) || [quality?.total_faxes || 0];
    state.dashCharts.volume = new Chart(volCtx, {
      type: "bar",
      data: { labels: volLabels, datasets: [{ label: "Faxes Processed", data: volData, backgroundColor: isDark ? "rgba(45,212,191,0.5)" : "rgba(15,118,110,0.6)", borderRadius: 6 }] },
      options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: gridColor } }, x: { grid: { display: false } } } },
    });

    // Chart 2: Doc types (doughnut from payers or quality)
    const dtCtx = $("chartDocTypes").getContext("2d");
    const dtLabels = quality?.doc_type_breakdown?.map((d) => d.doc_type) || ["Unknown"];
    const dtData = quality?.doc_type_breakdown?.map((d) => d.count) || [1];
    const colors = ["#0f766e", "#2dd4bf", "#fbbf24", "#f87171", "#818cf8", "#a78bfa", "#34d399"];
    state.dashCharts.docTypes = new Chart(dtCtx, {
      type: "doughnut",
      data: { labels: dtLabels, datasets: [{ data: dtData, backgroundColor: colors.slice(0, dtLabels.length), borderWidth: 0 }] },
      options: { responsive: true, plugins: { legend: { position: "bottom" } } },
    });

    // Chart 3: Payer bar chart
    const pCtx = $("chartPayers").getContext("2d");
    const pLabels = (payers || []).slice(0, 10).map((p) => p.payer_name || p.name || "?");
    const pData = (payers || []).slice(0, 10).map((p) => p.total_faxes || p.count || 0);
    state.dashCharts.payers = new Chart(pCtx, {
      type: "bar",
      data: { labels: pLabels, datasets: [{ label: "Faxes by Payer", data: pData, backgroundColor: isDark ? "rgba(251,191,36,0.5)" : "rgba(154,96,3,0.5)", borderRadius: 6 }] },
      options: { indexAxis: "y", responsive: true, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, grid: { color: gridColor } }, y: { grid: { display: false } } } },
    });

    // Chart 4: Confidence distribution (pie)
    const cCtx = $("chartConfidence").getContext("2d");
    const cLabels = quality?.confidence_buckets?.map((b) => b.range) || ["0-50%", "50-80%", "80-100%"];
    const cData = quality?.confidence_buckets?.map((b) => b.count) || [0, 0, quality?.total_faxes || 0];
    state.dashCharts.confidence = new Chart(cCtx, {
      type: "pie",
      data: { labels: cLabels, datasets: [{ data: cData, backgroundColor: ["#f87171", "#fbbf24", "#4ade80"], borderWidth: 0 }] },
      options: { responsive: true, plugins: { legend: { position: "bottom" } } },
    });
  }

  /* ════════════════════════════════════════
     WORKFLOW
     ════════════════════════════════════════ */
  function bindWorkflow() {
    initValidationListeners();

    // Upload
    $("uploadForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!validateField("uploadFile", "uploadFileError")) return;
      if (!validateField("uploadTenant", "uploadTenantError")) return;
      await withButtonBusy(e.submitter, "Uploading...", async () => {
        saveProfileFromInputs();
        const file = $("uploadFile").files[0];
        if (!file) throw new Error("Select a file.");
        const fd = new FormData();
        fd.append("file", file);
        fd.append("tenant_id", $("uploadTenant").value.trim());
        appendIfPresent(fd, "payer_hint", $("uploadPayer").value.trim());
        appendIfPresent(fd, "external_fax_id", $("uploadExternalId").value.trim());
        const r = await apiRequest("ingress", "/v1/faxes/upload", { method: "POST", formData: fd });
        renderJson($("uploadOutput"), r.data);
        if (r.data?.fax_job_id) {
          $("inspectJobId").value = r.data.fax_job_id;
          $("reviewJobId").value = r.data.fax_job_id;
        }
        toast("Fax uploaded successfully.", "success");
      }, $("uploadOutput"));
    });

    // List jobs with pagination
    $("listJobsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      state.jobsPage = 0;
      state.jobsLimit = Number($("jobsLimit").value || 25);
      await loadJobsPage(e.submitter);
    });

    $("jobsPrevBtn").addEventListener("click", async (e) => {
      if (state.jobsPage > 0) { state.jobsPage--; await loadJobsPage(e.currentTarget); }
    });
    $("jobsNextBtn").addEventListener("click", async (e) => {
      state.jobsPage++;
      await loadJobsPage(e.currentTarget);
    });

    // Table row actions
    $("jobsTableBody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-job-id]");
      if (!btn) return;
      const jobId = btn.dataset.jobId;
      const action = btn.dataset.action;
      if (action === "delete") {
        deleteJob(jobId, btn);
        return;
      }
      $("inspectJobId").value = jobId;
      $("reviewJobId").value = jobId;
      addActivity(`Selected job ${shortId(jobId)}.`, "info");
    });

    // Inspector actions
    $("jobInspectForm").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-inspect-action]");
      if (!btn) return;
      if (!validateField("inspectJobId", "inspectJobIdError")) return;
      const jobId = $("inspectJobId").value.trim();
      const action = btn.dataset.inspectAction;
      let ep = `/v1/faxes/${encId(jobId)}`;
      if (action === "results") ep += "/results";
      if (action === "ocr") ep += "/ocr";
      await withButtonBusy(btn, "Loading...", async () => {
        const r = await apiRequest("ingress", ep, { method: "GET" });
        renderJson($("inspectOutput"), r.data);
        addActivity(`Fetched ${action} for ${shortId(jobId)}.`, "info");
      }, $("inspectOutput"));
    });

    // Delete job button
    $("deleteJobBtn").addEventListener("click", async (e) => {
      const jobId = $("inspectJobId").value.trim();
      if (!jobId) { toast("Enter a job ID.", "error"); return; }
      await deleteJob(jobId, e.currentTarget);
    });

    // Review queue
    $("loadUnclaimedBtn").addEventListener("click", (e) => loadReviewQueue("unclaimed", e.currentTarget));
    $("loadPendingBtn").addEventListener("click", (e) => loadReviewQueue("pending", e.currentTarget));
    $("releaseExpiredBtn").addEventListener("click", async (e) => {
      await withButtonBusy(e.currentTarget, "Releasing...", async () => {
        const r = await apiRequest("review", "/v1/faxes/reviews/release-expired", { method: "POST" });
        renderJson($("reviewOutput"), r.data);
        toast("Expired claims released.", "success");
      }, $("reviewOutput"));
    });

    $("reviewQueueBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-review-action]");
      if (!btn) return;
      const action = btn.dataset.reviewAction;
      const jobId = btn.dataset.jobId;
      $("reviewJobId").value = jobId;
      if (action === "claim") await claimReviewJob(jobId, btn);
      else if (action === "open") await openReviewPacket(jobId, btn);
      else if (action === "claim-open") { await claimReviewJob(jobId, btn); await openReviewPacket(jobId, btn); }
    });

    $("claimReviewBtn").addEventListener("click", async (e) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) { toast("Enter a job ID.", "error"); return; }
      await claimReviewJob(jobId, e.currentTarget);
    });
    $("loadPacketBtn").addEventListener("click", async (e) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) { toast("Enter a job ID.", "error"); return; }
      await openReviewPacket(jobId, e.currentTarget);
    });
    $("submitCorrectionsBtn").addEventListener("click", async (e) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) { toast("Enter a job ID.", "error"); return; }
      await withButtonBusy(e.currentTarget, "Submitting...", async () => {
        const corrections = collectCorrections();
        if (!corrections.length) { toast("No corrected values entered.", "info"); return; }
        const payload = { corrected_fields: corrections };
        if (state.profile.reviewerId.trim()) payload.reviewer_id = state.profile.reviewerId.trim();
        const r = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review/submit`, { method: "POST", body: payload });
        renderJson($("reviewOutput"), r.data);
        toast(`${corrections.length} corrections submitted.`, "success");
      }, $("reviewOutput"));
    });
  }

  async function deleteJob(jobId, btn) {
    if (!window.confirm(`Delete fax job ${jobId}? This cannot be undone.`)) return;
    await withButtonBusy(btn, "Deleting...", async () => {
      await apiRequest("ingress", `/v1/faxes/${encId(jobId)}`, { method: "DELETE" });
      renderJson($("inspectOutput"), { deleted: jobId });
      toast("Job deleted.", "success");
      addActivity(`Deleted job ${shortId(jobId)}.`, "info");
    }, $("inspectOutput"));
  }

  async function loadJobsPage(btn) {
    await withButtonBusy(btn, "Loading...", async () => {
      saveProfileFromInputs();
      const query = { skip: state.jobsPage * state.jobsLimit, limit: state.jobsLimit };
      addIfTruthy(query, "status_filter", $("jobsStatus").value);
      addIfTruthy(query, "tenant_id", $("jobsTenant").value.trim());
      const r = await apiRequest("ingress", "/v1/faxes", { query });
      const data = r.data || {};
      const jobs = data.faxes || [];
      state.jobsTotal = data.total || jobs.length;
      renderJobsTable(jobs);
      updatePagination(jobs.length);
      startPollingIfNeeded(jobs);
      addActivity(`Loaded ${jobs.length} jobs (page ${state.jobsPage + 1}).`, "info");
    });
  }

  function updatePagination(count) {
    const bar = $("jobsPagination");
    bar.style.display = "flex";
    $("jobsPrevBtn").disabled = state.jobsPage === 0;
    $("jobsNextBtn").disabled = count < state.jobsLimit;
    const totalPages = Math.max(1, Math.ceil(state.jobsTotal / state.jobsLimit));
    $("jobsPageIndicator").textContent = `Page ${state.jobsPage + 1} of ${totalPages}`;
  }

  /* ════════════════════════════════════════
     POLLING
     ════════════════════════════════════════ */
  function startPollingIfNeeded(jobs) {
    stopPolling();
    const hasProcessing = jobs.some((j) => j.status === "PROCESSING" || j.status === "PENDING");
    if (!hasProcessing) { $("pollingBadge").style.display = "none"; return; }
    $("pollingBadge").style.display = "inline-flex";
    state.pollingTimer = setInterval(async () => {
      try {
        const query = { skip: state.jobsPage * state.jobsLimit, limit: state.jobsLimit };
        addIfTruthy(query, "status_filter", $("jobsStatus").value);
        addIfTruthy(query, "tenant_id", $("jobsTenant").value.trim());
        const r = await apiRequest("ingress", "/v1/faxes", { query });
        const jobs = r.data?.faxes || [];
        state.jobsTotal = r.data?.total || jobs.length;
        renderJobsTable(jobs);
        updatePagination(jobs.length);
        if (!jobs.some((j) => j.status === "PROCESSING" || j.status === "PENDING")) stopPolling();
      } catch (_) { stopPolling(); }
    }, 5000);
  }

  function stopPolling() {
    if (state.pollingTimer) { clearInterval(state.pollingTimer); state.pollingTimer = null; }
    const badge = $("pollingBadge");
    if (badge) badge.style.display = "none";
  }

  /* ════════════════════════════════════════
     REVIEW HELPERS
     ════════════════════════════════════════ */
  async function loadReviewQueue(kind, btn) {
    const ep = kind === "pending" ? "/v1/faxes/reviews/pending" : "/v1/faxes/reviews/unclaimed";
    await withButtonBusy(btn, "Loading...", async () => {
      const r = await apiRequest("review", ep, { method: "GET", query: { limit: 100, skip: 0 } });
      renderReviewQueueTable(Array.isArray(r.data) ? r.data : []);
    });
  }

  async function claimReviewJob(jobId, btn) {
    await withButtonBusy(btn, "Claiming...", async () => {
      const payload = {};
      if (state.profile.reviewerId.trim()) payload.reviewer_id = state.profile.reviewerId.trim();
      const r = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review/claim`, { method: "POST", body: payload });
      renderJson($("reviewOutput"), r.data);
      toast("Review claimed.", "success");
    }, $("reviewOutput"));
  }

  async function openReviewPacket(jobId, btn) {
    await withButtonBusy(btn, "Opening...", async () => {
      const r = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review-packet`, { method: "GET" });
      state.reviewPacket = r.data;
      renderReviewPacket(r.data);
      renderJson($("reviewOutput"), r.data);
      toast("Review packet loaded.", "success");
    }, $("reviewOutput"));
  }

  function collectCorrections() {
    return Array.from(document.querySelectorAll(".correction-input"))
      .map((i) => ({ field_key: i.dataset.fieldKey, corrected_value: i.value.trim(), original_value: i.dataset.originalValue || "" }))
      .filter((x) => x.corrected_value && x.corrected_value !== x.original_value)
      .map((x) => ({ field_key: x.field_key, corrected_value: x.corrected_value }));
  }

  /* ════════════════════════════════════════
     TEMPLATES
     ════════════════════════════════════════ */
  function bindTemplates() {
    $("listTemplatesForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Loading...", async () => {
        const query = { active_only: $("tplActiveOnly").value };
        addIfTruthy(query, "payer_name", $("tplPayerFilter").value.trim());
        const r = await apiRequest("review", "/v1/templates", { method: "GET", query });
        renderTemplatesTable(Array.isArray(r.data) ? r.data : []);
        renderJson($("templatesOutput"), r.data);
      }, $("templatesOutput"));
    });

    $("createTemplateForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!validateField("tplName", "tplNameError")) return;
      if (!validateField("tplPayer", "tplPayerError")) return;
      if (!validateField("tplDocType", "tplDocTypeError")) return;
      await withButtonBusy(e.submitter, "Creating...", async () => {
        const payload = { template_name: $("tplName").value.trim(), payer_name: $("tplPayer").value.trim(), doc_type: $("tplDocType").value.trim(), description: $("tplDescription").value.trim() || null };
        const r = await apiRequest("review", "/v1/templates", { method: "POST", body: payload });
        renderJson($("templatesOutput"), r.data);
        if (r.data?.template_id) $("tplUpdateId").value = r.data.template_id;
        toast("Template created.", "success");
      }, $("templatesOutput"));
    });

    $("updateTemplateForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Updating...", async () => {
        const tid = $("tplUpdateId").value.trim();
        const payload = {};
        addIfTruthy(payload, "template_name", $("tplUpdateName").value.trim());
        addIfTruthy(payload, "description", $("tplUpdateDescription").value.trim());
        if ($("tplUpdateActive").value) payload.is_active = $("tplUpdateActive").value === "true";
        const r = await apiRequest("review", `/v1/templates/${encId(tid)}`, { method: "PUT", body: payload });
        renderJson($("templatesOutput"), r.data);
        toast("Template updated.", "success");
      }, $("templatesOutput"));
    });

    $("deleteTemplateBtn").addEventListener("click", async (e) => {
      const tid = $("tplUpdateId").value.trim();
      if (!tid) { toast("Enter template ID.", "error"); return; }
      if (!window.confirm(`Delete template ${tid}?`)) return;
      await withButtonBusy(e.currentTarget, "Deleting...", async () => {
        await apiRequest("review", `/v1/templates/${encId(tid)}`, { method: "DELETE" });
        renderJson($("templatesOutput"), { deleted: tid });
        toast("Template deleted.", "success");
      }, $("templatesOutput"));
    });

    // Versions
    $("createVersionForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Creating...", async () => {
        const tid = $("verTemplateId").value.trim();
        const payload = { version_label: $("verLabel").value.trim(), match_min_score: Number($("verMinScore").value), match_phash_threshold: Number($("verPhash").value), match_orb_min_matches: Number($("verOrb").value) };
        const r = await apiRequest("review", `/v1/templates/${encId(tid)}/versions`, { method: "POST", body: payload });
        renderJson($("versionsOutput"), r.data);
        const vid = r.data?.template_version_id || "";
        ["sampleVersionId", "fieldVersionId", "listFieldsVersionId", "verActivateId", "verUpdateId", "testExtractVersionId"].forEach((id) => $(id).value = vid);
        toast("Version created.", "success");
      }, $("versionsOutput"));
    });

    $("activateVersionForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Activating...", async () => {
        const vid = $("verActivateId").value.trim();
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}/activate`, { method: "POST" });
        renderJson($("versionsOutput"), r.data);
        toast("Version activated.", "success");
      }, $("versionsOutput"));
    });

    $("updateVersionForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Updating...", async () => {
        const vid = $("verUpdateId").value.trim();
        const payload = {};
        addOptionalNumber(payload, "match_min_score", $("verUpdateMinScore").value);
        addOptionalNumber(payload, "match_phash_threshold", $("verUpdatePhash").value);
        addOptionalNumber(payload, "match_orb_min_matches", $("verUpdateOrb").value);
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}`, { method: "PUT", body: payload });
        renderJson($("versionsOutput"), r.data);
        toast("Version updated.", "success");
      }, $("versionsOutput"));
    });

    // Samples & Fields
    $("uploadSampleForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Uploading...", async () => {
        const vid = $("sampleVersionId").value.trim();
        const file = $("sampleFile").files[0];
        if (!file) throw new Error("Select a sample image.");
        const fd = new FormData();
        fd.append("file", file);
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}/samples`, { method: "POST", formData: fd });
        renderJson($("fieldOpsOutput"), r.data);
        toast("Sample uploaded.", "success");
      }, $("fieldOpsOutput"));
    });

    $("createFieldForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Creating...", async () => {
        const vid = $("fieldVersionId").value.trim();
        const payload = {
          field_key: $("fieldKey").value.trim(), field_label: $("fieldLabel").value.trim() || null,
          is_required: $("fieldRequired").checked,
          roi_x0: Number($("fieldX0").value), roi_y0: Number($("fieldY0").value),
          roi_x1: Number($("fieldX1").value), roi_y1: Number($("fieldY1").value),
          target_page: Number($("fieldTargetPage").value || 1),
          validation_regex: $("fieldRegex").value.trim() || null,
          expected_type: $("fieldExpectedType").value.trim() || "text",
        };
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}/fields`, { method: "POST", body: payload });
        renderJson($("fieldOpsOutput"), r.data);
        toast("Field created.", "success");
      }, $("fieldOpsOutput"));
    });

    $("listFieldsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Loading...", async () => {
        const vid = $("listFieldsVersionId").value.trim();
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}/fields`, { method: "GET" });
        renderTemplateFieldsTable(Array.isArray(r.data) ? r.data : []);
        renderJson($("fieldOpsOutput"), r.data);
      }, $("fieldOpsOutput"));
    });

    // Test match & extract
    $("testMatchForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Running...", async () => {
        const file = $("testMatchFile").files[0];
        if (!file) throw new Error("Select an image.");
        const query = {};
        addIfTruthy(query, "payer_hint", $("testMatchPayer").value.trim());
        const fd = new FormData();
        fd.append("file", file);
        const r = await apiRequest("review", "/v1/templates/test-match", { method: "POST", query, formData: fd });
        renderJson($("testOpsOutput"), r.data);
      }, $("testOpsOutput"));
    });

    $("testExtractForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Running...", async () => {
        const vid = $("testExtractVersionId").value.trim();
        const file = $("testExtractFile").files[0];
        if (!file) throw new Error("Select an image.");
        const fd = new FormData();
        fd.append("file", file);
        const r = await apiRequest("review", `/v1/templates/versions/${encId(vid)}/test-extract`, { method: "POST", formData: fd });
        renderJson($("testOpsOutput"), r.data);
      }, $("testOpsOutput"));
    });

    $("suggestRoiForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Analyzing...", async () => {
        const payload = { fax_job_id: $("roiFaxJobId").value.trim(), page_number: Number($("roiPageNumber").value || 1), field_key: $("roiFieldKey").value.trim(), correct_value: $("roiCorrectValue").value.trim() || null };
        const r = await apiRequest("review", "/v1/templates/suggest-roi", { method: "POST", body: payload });
        renderJson($("testOpsOutput"), r.data);
      }, $("testOpsOutput"));
    });
  }

  /* ════════════════════════════════════════
     INTELLIGENCE
     ════════════════════════════════════════ */
  function bindIntelligence() {
    $("queryForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!validateField("queryText", "queryTextError")) return;
      await withButtonBusy(e.submitter, "Searching...", async () => {
        const payload = { query: $("queryText").value.trim(), tier: Number($("queryTier").value || 1), limit: Number($("queryLimit").value || 10), fax_job_id: $("queryFaxJobId").value.trim() || null };
        const r = await apiRequest("query", "/v1/query", { method: "POST", body: payload });
        renderQueryResultsTable(r.data?.results || []);
        renderJson($("queryOutput"), r.data);
      }, $("queryOutput"));
    });

    $("analyticsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Running...", async () => {
        const report = $("analyticsReport").value;
        const days = Number($("analyticsDays").value || 30);
        const payer = $("analyticsPayer").value.trim();
        let r;
        if (report === "quality") r = await apiRequest("review", "/v1/analytics/quality", { method: "GET", query: { days } });
        else if (report === "payers") r = await apiRequest("review", "/v1/analytics/payers", { method: "GET", query: { days } });
        else if (report === "payer") {
          if (!payer) throw new Error("Payer name required.");
          r = await apiRequest("review", `/v1/analytics/payer/${encodeURIComponent(payer)}`, { method: "GET", query: { days } });
        } else if (report === "feedback") r = await apiRequest("review", "/v1/analytics/feedback-summary", { method: "GET", query: { days } });
        else r = await apiRequest("review", "/v1/analytics/recalibrate", { method: "POST", query: { days } });
        renderJson($("analyticsOutput"), r.data);
      }, $("analyticsOutput"));
    });

    // Models
    $("listModelsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Loading...", async () => {
        const query = {};
        addIfTruthy(query, "model_type", $("modelTypeFilter").value.trim());
        const r = await apiRequest("review", "/v1/models", { method: "GET", query });
        renderJson($("modelsOutput"), r.data);
      }, $("modelsOutput"));
    });

    $("activeModelForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Loading...", async () => {
        const r = await apiRequest("review", `/v1/models/active/${encodeURIComponent($("activeModelType").value.trim())}`, { method: "GET" });
        renderJson($("modelsOutput"), r.data);
      }, $("modelsOutput"));
    });

    $("registerModelForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Registering...", async () => {
        const payload = { model_type: $("newModelType").value.trim(), version_tag: $("newModelVersionTag").value.trim(), model_path: $("newModelPath").value.trim() || null, notes: $("newModelNotes").value.trim() || null };
        const ct = $("newModelConfig").value.trim();
        if (ct) payload.config = parseJsonText(ct, "Invalid config JSON.");
        const r = await apiRequest("review", "/v1/models", { method: "POST", body: payload });
        renderJson($("modelsOutput"), r.data);
      }, $("modelsOutput"));
    });

    $("promoteModelForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Promoting...", async () => {
        const mid = $("promoteModelId").value.trim();
        const r = await apiRequest("review", `/v1/models/${encId(mid)}/promote`, { method: "POST", body: { promoted_by: state.profile.reviewerId || "admin" } });
        renderJson($("modelsOutput"), r.data);
      }, $("modelsOutput"));
    });

    $("updateMetricsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Updating...", async () => {
        const mid = $("metricsModelId").value.trim();
        const metrics = parseJsonText($("metricsJson").value.trim(), "Invalid metrics JSON.");
        const r = await apiRequest("review", `/v1/models/${encId(mid)}/metrics`, { method: "PUT", body: { metrics } });
        renderJson($("modelsOutput"), r.data);
      }, $("modelsOutput"));
    });

    $("deleteModelForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const mid = $("deleteModelId").value.trim();
      if (!window.confirm(`Delete model ${mid}?`)) return;
      await withButtonBusy(e.submitter, "Deleting...", async () => {
        await apiRequest("review", `/v1/models/${encId(mid)}`, { method: "DELETE" });
        renderJson($("modelsOutput"), { deleted: mid });
      }, $("modelsOutput"));
    });
  }

  /* ════════════════════════════════════════
     API CONSOLE
     ════════════════════════════════════════ */
  function bindApiConsole() {
    $("apiService").addEventListener("change", () => {
      $("apiCustomBaseWrap").style.display = $("apiService").value === "custom" ? "flex" : "none";
    });
    $("apiService").dispatchEvent(new Event("change"));

    $("apiConsoleForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      await withButtonBusy(e.submitter, "Executing...", async () => {
        const svc = $("apiService").value;
        const method = $("apiMethod").value;
        const path = $("apiPath").value.trim();
        const query = parseQueryString($("apiQueryString").value.trim());
        let body;
        const rawBody = $("apiBody").value.trim();
        if (rawBody && !["GET", "DELETE"].includes(method)) body = parseJsonText(rawBody, "Invalid JSON body.");
        const opts = { method, query, body };
        if (svc === "custom") {
          opts.baseUrl = $("apiCustomBase").value.trim();
          if (!opts.baseUrl) throw new Error("Custom base URL required.");
        }
        const r = await apiRequest(svc, path, opts);
        renderJson($("apiConsoleOutput"), { status: r.status, url: r.url, data: r.data });
      }, $("apiConsoleOutput"));
    });
  }

  /* ════════════════════════════════════════
     RENDERERS
     ════════════════════════════════════════ */
  function renderJobsTable(jobs) {
    $("jobsTableBody").innerHTML = jobs.map((j) => `
      <tr>
        <td><code>${escapeHtml(j.fax_job_id)}</code></td>
        <td><span class="status-chip ${escapeHtml(j.status)}">${escapeHtml(j.status)}</span></td>
        <td>${escapeHtml(j.payer_hint || "-")}</td>
        <td>${escapeHtml(j.doc_type || "-")}</td>
        <td>${j.needs_review ? "Yes" : "No"}</td>
        <td>${escapeHtml(formatDate(j.created_at))}</td>
        <td>
          <button class="btn sm ghost" data-job-id="${escapeAttr(j.fax_job_id)}" data-action="select">Select</button>
          <button class="btn sm danger" data-job-id="${escapeAttr(j.fax_job_id)}" data-action="delete">Delete</button>
        </td>
      </tr>
    `).join("");
  }

  function renderReviewQueueTable(items) {
    $("reviewQueueBody").innerHTML = items.map((item) => {
      const jobId = item.fax_job_id || item.job_id || "";
      const reasons = (item.review_reasons || []).join(", ");
      return `
        <tr>
          <td><code>${escapeHtml(jobId)}</code></td>
          <td>${escapeHtml(String(item.priority || "-"))}</td>
          <td>${escapeHtml(item.claimed_by || "-")}</td>
          <td>${escapeHtml(reasons || "-")}</td>
          <td>
            <button class="btn sm primary" data-review-action="claim-open" data-job-id="${escapeAttr(jobId)}">Claim & Open</button>
            <button class="btn sm ghost" data-review-action="open" data-job-id="${escapeAttr(jobId)}">Open</button>
          </td>
        </tr>`;
    }).join("");
  }

  function renderReviewPacket(packet) {
    $("reviewJobId").value = packet.fax_job_id || "";
    const tags = [
      `Reasons: ${(packet.review_reasons || []).join(", ") || "none"}`,
      `Fields: ${(packet.extracted_fields || []).length}`,
      `Pages: ${(packet.pages || []).length}`,
      `Created: ${packet.created_at || "n/a"}`,
    ];
    $("reviewMeta").innerHTML = tags.map((v) => `<span class="meta-item">${escapeHtml(v)}</span>`).join("");

    $("reviewPages").innerHTML = (packet.pages || []).map((page) => {
      const url = escapeAttr(page.image_url || "");
      const cap = `Page ${page.page_number} | ${page.width_px}x${page.height_px}`;
      return `
        <article class="page-card" onclick="openLightbox('${url}', '${escapeAttr(cap)}')">
          <img src="${url}" alt="Page ${escapeAttr(String(page.page_number))}" loading="lazy">
          <div class="page-caption">${escapeHtml(cap)}</div>
        </article>`;
    }).join("");

    $("reviewFieldsBody").innerHTML = (packet.extracted_fields || []).map((f) => {
      const conf = f.confidence == null ? "n/a" : Number(f.confidence).toFixed(3);
      const val = f.value == null ? "" : String(f.value);
      const flag = f.is_flagged
        ? `<span class="flag-pill warn">${escapeHtml(f.flag_reason || "FLAGGED")}</span>`
        : `<span class="flag-pill ok">OK</span>`;
      return `
        <tr>
          <td><code>${escapeHtml(f.field_key)}</code></td>
          <td>${escapeHtml(val || "(empty)")}</td>
          <td>${escapeHtml(conf)}</td>
          <td>${flag}</td>
          <td><input type="text" class="correction-input" data-field-key="${escapeAttr(f.field_key)}" data-original-value="${escapeAttr(val)}" placeholder="Corrected value" aria-label="Corrected value for ${escapeAttr(f.field_key)}"></td>
        </tr>`;
    }).join("");
  }

  function renderTemplatesTable(templates) {
    $("templatesBody").innerHTML = templates.map((t) => `
      <tr>
        <td><code>${escapeHtml(t.template_id)}</code></td>
        <td>${escapeHtml(t.template_name || t.name || "-")}</td>
        <td>${escapeHtml(t.payer_name || "-")}</td>
        <td>${escapeHtml(t.doc_type || "-")}</td>
        <td>${t.is_active ? "✓" : "✗"}</td>
      </tr>
    `).join("");
  }

  function renderTemplateFieldsTable(fields) {
    $("fieldsBody").innerHTML = fields.map((f) => `
      <tr>
        <td><code>${escapeHtml(f.field_key)}</code></td>
        <td>${escapeHtml(f.field_label || "-")}</td>
        <td>${[f.roi_x0, f.roi_y0, f.roi_x1, f.roi_y1].map((v) => v?.toFixed(3) || "?").join(", ")}</td>
        <td>${f.target_page || 1}</td>
        <td>${f.is_required ? "✓" : "✗"}</td>
      </tr>
    `).join("");
  }

  function renderQueryResultsTable(results) {
    $("queryResultsBody").innerHTML = results.map((r) => `
      <tr>
        <td><code>${escapeHtml(r.fax_job_id || "-")}</code></td>
        <td>${escapeHtml(r.field_key || "-")}</td>
        <td>${escapeHtml(String(r.value ?? "-"))}</td>
        <td>${r.confidence != null ? Number(r.confidence).toFixed(3) : "-"}</td>
        <td>${r.similarity_score != null ? Number(r.similarity_score).toFixed(3) : "-"}</td>
      </tr>
    `).join("");
  }

  /* ════════════════════════════════════════
     UTILITIES
     ════════════════════════════════════════ */
  function renderJson(el, data) {
    try { el.textContent = JSON.stringify(data, null, 2); }
    catch { el.textContent = String(data); }
  }

  function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function escapeAttr(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;"); }
  function encId(id) { return encodeURIComponent(id); }
  function shortId(id) { return id?.length > 12 ? id.slice(0, 8) + "…" : id; }
  function formatDate(d) { if (!d) return "-"; try { return new Date(d).toLocaleString(); } catch { return d; } }

  function appendIfPresent(fd, key, val) { if (val) fd.append(key, val); }
  function addIfTruthy(obj, key, val) { if (val) obj[key] = val; }
  function addOptionalNumber(obj, key, val) { if (val !== "" && val != null) obj[key] = Number(val); }

  function parseJsonText(text, errMsg) {
    try { return JSON.parse(text); }
    catch (e) { throw new Error(errMsg || "Invalid JSON: " + e.message); }
  }

  function parseQueryString(qs) {
    if (!qs) return {};
    const params = {};
    qs.split("&").forEach((pair) => {
      const [k, v] = pair.split("=").map(decodeURIComponent);
      if (k) params[k] = v ?? "";
    });
    return params;
  }

  // Profile
  function hydrateProfile() {
    try { const s = localStorage.getItem(STORAGE_KEY); if (s) Object.assign(state.profile, JSON.parse(s)); } catch { }
  }
  function persistProfile() { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.profile)); }
  function applyProfileToInputs() {
    $("cfgIngress").value = state.profile.ingressBaseUrl;
    $("cfgReview").value = state.profile.reviewBaseUrl;
    $("cfgQuery").value = state.profile.queryBaseUrl;
    $("cfgToken").value = state.profile.token;
    $("cfgReviewer").value = state.profile.reviewerId;
  }
  function saveProfileFromInputs() {
    state.profile.ingressBaseUrl = $("cfgIngress").value.trim();
    state.profile.reviewBaseUrl = $("cfgReview").value.trim();
    state.profile.queryBaseUrl = $("cfgQuery").value.trim();
    state.profile.token = $("cfgToken").value.trim();
    state.profile.reviewerId = $("cfgReviewer").value.trim();
    persistProfile();
    syncStatusPills();
  }

  function syncStatusPills() {
    $("authPill").textContent = state.profile.token ? "Auth: Bearer ••••" : "Auth: Optional";
  }

  // Toast
  function toast(msg, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    $("toastHost").appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  // Activity
  function addActivity(msg, type = "info") {
    const feed = $("activityFeed");
    const li = document.createElement("li");
    li.className = type;
    const ts = new Date().toLocaleTimeString();
    li.textContent = `${ts} — ${msg}`;
    feed.prepend(li);
    while (feed.children.length > 40) feed.lastChild.remove();
  }

  // Button busy state
  async function withButtonBusy(btn, label, fn, outputEl) {
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
    try { await fn(); }
    catch (err) {
      toast(err.message || "Request failed.", "error");
      addActivity(err.message || "Error.", "error");
      if (outputEl) renderJson(outputEl, { error: err.message });
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // API request
  async function apiRequest(service, path, opts = {}) {
    let base;
    if (opts.baseUrl) base = opts.baseUrl;
    else if (service === "ingress") base = state.profile.ingressBaseUrl;
    else if (service === "review") base = state.profile.reviewBaseUrl;
    else if (service === "query") base = state.profile.queryBaseUrl;
    else base = state.profile.reviewBaseUrl;

    let url = base.replace(/\/+$/, "") + path;
    if (opts.query && Object.keys(opts.query).length) {
      url += "?" + Object.entries(opts.query).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
    }

    const headers = {};
    if (state.profile.token) headers["Authorization"] = `Bearer ${state.profile.token}`;

    const fetchOpts = { method: opts.method || "GET", headers };

    if (opts.formData) {
      fetchOpts.body = opts.formData;
    } else if (opts.body) {
      headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify(opts.body);
    }

    const resp = await fetch(url, fetchOpts);
    let data;
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("json")) data = await resp.json();
    else data = await resp.text();

    if (!resp.ok) {
      const err = new Error(data?.detail || data?.message || `HTTP ${resp.status}`);
      err.status = resp.status;
      err.serviceName = service;
      throw err;
    }
    return { data, status: resp.status, url };
  }

  // Boot
  document.addEventListener("DOMContentLoaded", init);
})();
