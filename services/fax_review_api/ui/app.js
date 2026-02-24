(function () {
  "use strict";

  const STORAGE_KEY = "fax_ui_profile_v1";

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
  };

  const $ = (id) => document.getElementById(id);

  function init() {
    hydrateProfile();
    applyProfileToInputs();
    bindTabs();
    bindConfigPanel();
    bindWorkflow();
    bindTemplates();
    bindIntelligence();
    bindApiConsole();
    animateReveals();
    syncStatusPills();
    addActivity("UI initialized.", "info");
  }

  function animateReveals() {
    const nodes = Array.from(document.querySelectorAll(".reveal"));
    nodes.forEach((node, index) => {
      node.style.animationDelay = `${Math.min(index * 70, 450)}ms`;
    });
  }

  function bindTabs() {
    const tabButtons = Array.from(document.querySelectorAll(".tab"));
    tabButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const tab = button.dataset.tab;
        tabButtons.forEach((b) => b.classList.toggle("active", b === button));
        Array.from(document.querySelectorAll(".tab-panel")).forEach((panel) => {
          panel.classList.toggle("active", panel.id === `tab-${tab}`);
        });
      });
    });
  }

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
      addActivity("Connection profile reset.", "info");
    });

    $("testHealthBtn").addEventListener("click", async (event) => {
      const btn = event.currentTarget;
      await withButtonBusy(btn, "Testing...", async () => {
        saveProfileFromInputs();
        const results = await Promise.allSettled([
          apiRequest("ingress", "/health", { method: "GET" }),
          apiRequest("review", "/health", { method: "GET" }),
          apiRequest("query", "/health", { method: "GET" }),
        ]);

        const summary = {
          ingress: formatHealthResult(results[0]),
          review: formatHealthResult(results[1]),
          query: formatHealthResult(results[2]),
        };
        addActivity(`Health check: ${summary.ingress}, ${summary.review}, ${summary.query}`, "info");
        toast("Health check completed.", "info");
      });
    });
  }

  function formatHealthResult(settledResult) {
    if (settledResult.status === "fulfilled") {
      return `${settledResult.value.serviceName}=OK`;
    }
    const err = settledResult.reason;
    return `${err.serviceName || "service"}=FAIL(${err.status || "n/a"})`;
  }

  function bindWorkflow() {
    $("uploadForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitBtn = event.submitter;
      await withButtonBusy(submitBtn, "Uploading...", async () => {
        saveProfileFromInputs();
        const file = $("uploadFile").files[0];
        if (!file) {
          throw new Error("Select a file before uploading.");
        }

        const formData = new FormData();
        formData.append("file", file);
        formData.append("tenant_id", $("uploadTenant").value.trim());
        appendIfPresent(formData, "payer_hint", $("uploadPayer").value.trim());
        appendIfPresent(formData, "external_fax_id", $("uploadExternalId").value.trim());

        const response = await apiRequest("ingress", "/v1/faxes/upload", {
          method: "POST",
          formData,
        });
        renderJson($("uploadOutput"), response.data);
        if (response.data && response.data.fax_job_id) {
          $("inspectJobId").value = response.data.fax_job_id;
          $("reviewJobId").value = response.data.fax_job_id;
        }
        addActivity("Fax upload request completed.", "info");
        toast("Fax uploaded or deduplicated successfully.", "success");
      }, $("uploadOutput"));
    });

    $("listJobsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitBtn = event.submitter;
      await withButtonBusy(submitBtn, "Loading...", async () => {
        saveProfileFromInputs();
        const query = {
          skip: Number($("jobsSkip").value || 0),
          limit: Number($("jobsLimit").value || 25),
        };
        addIfTruthy(query, "status_filter", $("jobsStatus").value);
        addIfTruthy(query, "tenant_id", $("jobsTenant").value.trim());

        const response = await apiRequest("ingress", "/v1/faxes", { query });
        const jobs = response.data.faxes || [];
        renderJobsTable(jobs);
        addActivity(`Loaded ${jobs.length} jobs.`, "info");
      });
    });

    $("jobsTableBody").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-job-id]");
      if (!button) {
        return;
      }
      const jobId = button.dataset.jobId;
      $("inspectJobId").value = jobId;
      $("reviewJobId").value = jobId;
      addActivity(`Selected job ${shortId(jobId)} from table.`, "info");
    });

    $("jobInspectForm").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-inspect-action]");
      if (!button) {
        return;
      }
      const jobId = $("inspectJobId").value.trim();
      if (!jobId) {
        toast("Enter a fax job ID first.", "error");
        return;
      }
      const action = button.dataset.inspectAction;
      let endpoint = `/v1/faxes/${jobId}`;
      if (action === "results") endpoint = `/v1/faxes/${jobId}/results`;
      if (action === "ocr") endpoint = `/v1/faxes/${jobId}/ocr`;

      await withButtonBusy(button, "Loading...", async () => {
        const response = await apiRequest("ingress", endpoint, { method: "GET" });
        renderJson($("inspectOutput"), response.data);
        addActivity(`Fetched ${action} for ${shortId(jobId)}.`, "info");
      }, $("inspectOutput"));
    });

    $("loadUnclaimedBtn").addEventListener("click", async (event) => {
      await loadReviewQueue("unclaimed", event.currentTarget);
    });
    $("loadPendingBtn").addEventListener("click", async (event) => {
      await loadReviewQueue("pending", event.currentTarget);
    });

    $("releaseExpiredBtn").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      await withButtonBusy(button, "Releasing...", async () => {
        const response = await apiRequest("review", "/v1/faxes/reviews/release-expired", {
          method: "POST",
        });
        renderJson($("reviewOutput"), response.data);
        toast("Expired claims released.", "success");
      }, $("reviewOutput"));
    });

    $("reviewQueueBody").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-review-action]");
      if (!button) {
        return;
      }
      const action = button.dataset.reviewAction;
      const jobId = button.dataset.jobId;
      $("reviewJobId").value = jobId;

      if (action === "claim") {
        await claimReviewJob(jobId, button);
      } else if (action === "open") {
        await openReviewPacket(jobId, button);
      } else if (action === "claim-open") {
        await claimReviewJob(jobId, button);
        await openReviewPacket(jobId, button);
      }
    });

    $("claimReviewBtn").addEventListener("click", async (event) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) {
        toast("Enter a job ID to claim.", "error");
        return;
      }
      await claimReviewJob(jobId, event.currentTarget);
    });

    $("loadPacketBtn").addEventListener("click", async (event) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) {
        toast("Enter a job ID to load packet.", "error");
        return;
      }
      await openReviewPacket(jobId, event.currentTarget);
    });

    $("submitCorrectionsBtn").addEventListener("click", async (event) => {
      const jobId = $("reviewJobId").value.trim();
      if (!jobId) {
        toast("Enter a job ID before submitting corrections.", "error");
        return;
      }
      await withButtonBusy(event.currentTarget, "Submitting...", async () => {
        const corrections = collectCorrections();
        if (!corrections.length) {
          toast("No corrected values entered.", "info");
          return;
        }

        const payload = {
          corrected_fields: corrections,
        };
        if (state.profile.reviewerId.trim()) {
          payload.reviewer_id = state.profile.reviewerId.trim();
        }

        const response = await apiRequest("review", `/v1/faxes/${jobId}/review/submit`, {
          method: "POST",
          body: payload,
        });
        renderJson($("reviewOutput"), response.data);
        addActivity(`Submitted ${corrections.length} corrections for ${shortId(jobId)}.`, "info");
        toast("Corrections submitted.", "success");
      }, $("reviewOutput"));
    });
  }

  async function loadReviewQueue(kind, button) {
    const endpoint = kind === "pending" ? "/v1/faxes/reviews/pending" : "/v1/faxes/reviews/unclaimed";
    await withButtonBusy(button, "Loading...", async () => {
      const response = await apiRequest("review", endpoint, {
        method: "GET",
        query: { limit: 100, skip: 0 },
      });
      renderReviewQueueTable(response.data || []);
      addActivity(`Loaded ${kind} review queue (${(response.data || []).length} items).`, "info");
    });
  }

  async function claimReviewJob(jobId, button) {
    await withButtonBusy(button, "Claiming...", async () => {
      const payload = {};
      if (state.profile.reviewerId.trim()) {
        payload.reviewer_id = state.profile.reviewerId.trim();
      }
      const response = await apiRequest("review", `/v1/faxes/${jobId}/review/claim`, {
        method: "POST",
        body: payload,
      });
      renderJson($("reviewOutput"), response.data);
      addActivity(`Claimed review for ${shortId(jobId)}.`, "info");
      toast("Review claimed.", "success");
    }, $("reviewOutput"));
  }

  async function openReviewPacket(jobId, button) {
    await withButtonBusy(button, "Opening...", async () => {
      const response = await apiRequest("review", `/v1/faxes/${jobId}/review-packet`, { method: "GET" });
      state.reviewPacket = response.data;
      renderReviewPacket(response.data);
      renderJson($("reviewOutput"), response.data);
      addActivity(`Opened review packet for ${shortId(jobId)}.`, "info");
      toast("Review packet loaded.", "success");
    }, $("reviewOutput"));
  }

  function renderReviewPacket(packet) {
    const metaContainer = $("reviewMeta");
    const pagesContainer = $("reviewPages");
    const fieldsBody = $("reviewFieldsBody");

    $("reviewJobId").value = packet.fax_job_id || "";

    const tags = [
      `Reasons: ${(packet.review_reasons || []).join(", ") || "none"}`,
      `Fields: ${(packet.extracted_fields || []).length}`,
      `Pages: ${(packet.pages || []).length}`,
      `Created: ${packet.created_at || "n/a"}`,
    ];
    metaContainer.innerHTML = tags.map((value) => `<span class="meta-item">${escapeHtml(value)}</span>`).join("");

    pagesContainer.innerHTML = (packet.pages || [])
      .map((page) => {
        const safeUrl = escapeAttr(page.image_url || "");
        return `
          <article class="page-card">
            <img src="${safeUrl}" alt="Review page ${escapeAttr(String(page.page_number))}" loading="lazy">
            <div class="page-caption">Page ${escapeHtml(String(page.page_number))} | ${escapeHtml(String(page.width_px))}x${escapeHtml(String(page.height_px))}</div>
          </article>
        `;
      })
      .join("");

    fieldsBody.innerHTML = (packet.extracted_fields || [])
      .map((field) => {
        const confidence = field.confidence === null || field.confidence === undefined
          ? "n/a"
          : Number(field.confidence).toFixed(3);
        const originalValue = field.value === null || field.value === undefined ? "" : String(field.value);
        const flag = field.is_flagged
          ? `<span class="flag-pill warn">${escapeHtml(field.flag_reason || "FLAGGED")}</span>`
          : `<span class="flag-pill ok">OK</span>`;
        return `
          <tr>
            <td><code>${escapeHtml(field.field_key)}</code></td>
            <td>${escapeHtml(originalValue || "(empty)")}</td>
            <td>${escapeHtml(confidence)}</td>
            <td>${flag}</td>
            <td>
              <input
                type="text"
                class="correction-input"
                data-field-key="${escapeAttr(field.field_key)}"
                data-original-value="${escapeAttr(originalValue)}"
                placeholder="Enter corrected value"
              >
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function collectCorrections() {
    return Array.from(document.querySelectorAll(".correction-input"))
      .map((input) => ({
        field_key: input.dataset.fieldKey,
        corrected_value: input.value.trim(),
        original_value: input.dataset.originalValue || "",
      }))
      .filter((item) => item.corrected_value && item.corrected_value !== item.original_value)
      .map((item) => ({
        field_key: item.field_key,
        corrected_value: item.corrected_value,
      }));
  }

  function bindTemplates() {
    $("listTemplatesForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      await withButtonBusy(button, "Loading...", async () => {
        const query = {};
        addIfTruthy(query, "payer_name", $("tplPayerFilter").value.trim());
        query.active_only = $("tplActiveOnly").value;
        const response = await apiRequest("review", "/v1/templates", { method: "GET", query });
        renderTemplatesTable(response.data || []);
        renderJson($("templatesOutput"), response.data);
        addActivity(`Loaded ${(response.data || []).length} templates.`, "info");
      }, $("templatesOutput"));
    });

    $("createTemplateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      await withButtonBusy(button, "Creating...", async () => {
        const payload = {
          template_name: $("tplName").value.trim(),
          payer_name: $("tplPayer").value.trim(),
          doc_type: $("tplDocType").value.trim(),
          description: $("tplDescription").value.trim() || null,
        };
        const response = await apiRequest("review", "/v1/templates", { method: "POST", body: payload });
        renderJson($("templatesOutput"), response.data);
        $("tplUpdateId").value = response.data.template_id || "";
        addActivity(`Created template ${shortId(response.data.template_id)}.`, "info");
        toast("Template created.", "success");
      }, $("templatesOutput"));
    });

    $("updateTemplateForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      await withButtonBusy(button, "Updating...", async () => {
        const templateId = $("tplUpdateId").value.trim();
        const payload = {};
        addIfTruthy(payload, "template_name", $("tplUpdateName").value.trim());
        addIfTruthy(payload, "description", $("tplUpdateDescription").value.trim());
        if ($("tplUpdateActive").value) {
          payload.is_active = $("tplUpdateActive").value === "true";
        }
        const response = await apiRequest("review", `/v1/templates/${templateId}`, {
          method: "PUT",
          body: payload,
        });
        renderJson($("templatesOutput"), response.data);
        toast("Template updated.", "success");
      }, $("templatesOutput"));
    });

    $("deleteTemplateBtn").addEventListener("click", async (event) => {
      const templateId = $("tplUpdateId").value.trim();
      if (!templateId) {
        toast("Enter template ID to delete.", "error");
        return;
      }
      if (!window.confirm(`Delete template ${templateId}? This cannot be undone.`)) {
        return;
      }
      await withButtonBusy(event.currentTarget, "Deleting...", async () => {
        await apiRequest("review", `/v1/templates/${templateId}`, { method: "DELETE" });
        renderJson($("templatesOutput"), { deleted: templateId });
        toast("Template deleted.", "success");
      }, $("templatesOutput"));
    });

    $("createVersionForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Creating...", async () => {
        const templateId = $("verTemplateId").value.trim();
        const payload = {
          version_label: $("verLabel").value.trim(),
          match_min_score: Number($("verMinScore").value),
          match_phash_threshold: Number($("verPhash").value),
          match_orb_min_matches: Number($("verOrb").value),
        };
        const response = await apiRequest("review", `/v1/templates/${templateId}/versions`, {
          method: "POST",
          body: payload,
        });
        renderJson($("versionsOutput"), response.data);
        $("sampleVersionId").value = response.data.template_version_id || "";
        $("fieldVersionId").value = response.data.template_version_id || "";
        $("listFieldsVersionId").value = response.data.template_version_id || "";
        $("verActivateId").value = response.data.template_version_id || "";
        $("verUpdateId").value = response.data.template_version_id || "";
        $("testExtractVersionId").value = response.data.template_version_id || "";
        addActivity(`Created version ${shortId(response.data.template_version_id)}.`, "info");
      }, $("versionsOutput"));
    });

    $("activateVersionForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Activating...", async () => {
        const versionId = $("verActivateId").value.trim();
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}/activate`, {
          method: "POST",
        });
        renderJson($("versionsOutput"), response.data);
        toast("Version activated.", "success");
      }, $("versionsOutput"));
    });

    $("updateVersionForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Updating...", async () => {
        const versionId = $("verUpdateId").value.trim();
        const payload = {};
        addOptionalNumber(payload, "match_min_score", $("verUpdateMinScore").value);
        addOptionalNumber(payload, "match_phash_threshold", $("verUpdatePhash").value);
        addOptionalNumber(payload, "match_orb_min_matches", $("verUpdateOrb").value);
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}`, {
          method: "PUT",
          body: payload,
        });
        renderJson($("versionsOutput"), response.data);
        toast("Version updated.", "success");
      }, $("versionsOutput"));
    });

    $("uploadSampleForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Uploading...", async () => {
        const versionId = $("sampleVersionId").value.trim();
        const file = $("sampleFile").files[0];
        if (!file) throw new Error("Select a sample image.");
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}/samples`, {
          method: "POST",
          formData,
        });
        renderJson($("fieldOpsOutput"), response.data);
        toast("Sample uploaded.", "success");
      }, $("fieldOpsOutput"));
    });

    $("createFieldForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Creating...", async () => {
        const versionId = $("fieldVersionId").value.trim();
        const payload = {
          field_key: $("fieldKey").value.trim(),
          field_label: $("fieldLabel").value.trim() || null,
          is_required: $("fieldRequired").checked,
          roi_x0: Number($("fieldX0").value),
          roi_y0: Number($("fieldY0").value),
          roi_x1: Number($("fieldX1").value),
          roi_y1: Number($("fieldY1").value),
          target_page: Number($("fieldTargetPage").value || 1),
          validation_regex: $("fieldRegex").value.trim() || null,
          expected_type: $("fieldExpectedType").value.trim() || "text",
        };
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}/fields`, {
          method: "POST",
          body: payload,
        });
        renderJson($("fieldOpsOutput"), response.data);
        toast("Field created.", "success");
      }, $("fieldOpsOutput"));
    });

    $("listFieldsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Loading...", async () => {
        const versionId = $("listFieldsVersionId").value.trim();
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}/fields`, {
          method: "GET",
        });
        renderTemplateFieldsTable(response.data || []);
        renderJson($("fieldOpsOutput"), response.data);
      }, $("fieldOpsOutput"));
    });

    $("testMatchForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Running...", async () => {
        const file = $("testMatchFile").files[0];
        if (!file) throw new Error("Select an image.");
        const query = {};
        addIfTruthy(query, "payer_hint", $("testMatchPayer").value.trim());
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiRequest("review", "/v1/templates/test-match", {
          method: "POST",
          query,
          formData,
        });
        renderJson($("testOpsOutput"), response.data);
      }, $("testOpsOutput"));
    });

    $("testExtractForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Running...", async () => {
        const versionId = $("testExtractVersionId").value.trim();
        const file = $("testExtractFile").files[0];
        if (!file) throw new Error("Select an image.");
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiRequest("review", `/v1/templates/versions/${versionId}/test-extract`, {
          method: "POST",
          formData,
        });
        renderJson($("testOpsOutput"), response.data);
      }, $("testOpsOutput"));
    });

    $("suggestRoiForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Analyzing...", async () => {
        const payload = {
          fax_job_id: $("roiFaxJobId").value.trim(),
          page_number: Number($("roiPageNumber").value || 1),
          field_key: $("roiFieldKey").value.trim(),
          correct_value: $("roiCorrectValue").value.trim() || null,
        };
        const response = await apiRequest("review", "/v1/templates/suggest-roi", {
          method: "POST",
          body: payload,
        });
        renderJson($("testOpsOutput"), response.data);
      }, $("testOpsOutput"));
    });
  }

  function bindIntelligence() {
    $("queryForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Searching...", async () => {
        const payload = {
          query: $("queryText").value.trim(),
          tier: Number($("queryTier").value || 1),
          limit: Number($("queryLimit").value || 10),
          fax_job_id: $("queryFaxJobId").value.trim() || null,
        };
        const response = await apiRequest("query", "/v1/query", {
          method: "POST",
          body: payload,
        });
        renderQueryResultsTable(response.data.results || []);
        renderJson($("queryOutput"), response.data);
      }, $("queryOutput"));
    });

    $("analyticsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Running...", async () => {
        const report = $("analyticsReport").value;
        const days = Number($("analyticsDays").value || 30);
        const payer = $("analyticsPayer").value.trim();
        let response;

        if (report === "quality") {
          response = await apiRequest("review", "/v1/analytics/quality", { method: "GET", query: { days } });
        } else if (report === "payers") {
          response = await apiRequest("review", "/v1/analytics/payers", { method: "GET", query: { days } });
        } else if (report === "payer") {
          if (!payer) throw new Error("Payer name is required for single payer report.");
          response = await apiRequest("review", `/v1/analytics/payer/${encodeURIComponent(payer)}`, {
            method: "GET",
            query: { days },
          });
        } else if (report === "feedback") {
          response = await apiRequest("review", "/v1/analytics/feedback-summary", { method: "GET", query: { days } });
        } else {
          response = await apiRequest("review", "/v1/analytics/recalibrate", { method: "POST", query: { days } });
        }

        renderJson($("analyticsOutput"), response.data);
      }, $("analyticsOutput"));
    });

    $("listModelsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Loading...", async () => {
        const modelType = $("modelTypeFilter").value.trim();
        const query = {};
        addIfTruthy(query, "model_type", modelType);
        const response = await apiRequest("review", "/v1/models", {
          method: "GET",
          query,
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });

    $("activeModelForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Loading...", async () => {
        const modelType = $("activeModelType").value.trim();
        const response = await apiRequest("review", `/v1/models/active/${encodeURIComponent(modelType)}`, {
          method: "GET",
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });

    $("registerModelForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Registering...", async () => {
        const payload = {
          model_type: $("newModelType").value.trim(),
          version_tag: $("newModelVersionTag").value.trim(),
          model_path: $("newModelPath").value.trim() || null,
          notes: $("newModelNotes").value.trim() || null,
        };
        const configText = $("newModelConfig").value.trim();
        if (configText) payload.config = parseJsonText(configText, "Invalid model config JSON.");
        const response = await apiRequest("review", "/v1/models", {
          method: "POST",
          body: payload,
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });

    $("promoteModelForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Promoting...", async () => {
        const modelVersionId = $("promoteModelId").value.trim();
        const response = await apiRequest("review", `/v1/models/${modelVersionId}/promote`, {
          method: "POST",
          body: { promoted_by: state.profile.reviewerId || "admin" },
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });
