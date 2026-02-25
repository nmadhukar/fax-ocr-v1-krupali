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
    const tabButtons = Array.from(document.querySelectorAll(".tab[role='tab']"));
    tabButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const tab = button.dataset.tab;
        tabButtons.forEach((b) => {
          const isActive = b === button;
          b.classList.toggle("active", isActive);
          b.setAttribute("aria-selected", String(isActive));
        });
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
        const data = response.data || {};
        const jobs = data.faxes || [];
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
      let endpoint = `/v1/faxes/${encId(jobId)}`;
      if (action === "results") endpoint = `/v1/faxes/${encId(jobId)}/results`;
      if (action === "ocr") endpoint = `/v1/faxes/${encId(jobId)}/ocr`;

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

        const response = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review/submit`, {
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
      const items = Array.isArray(response.data) ? response.data : [];
      renderReviewQueueTable(items);
      addActivity(`Loaded ${kind} review queue (${items.length} items).`, "info");
    });
  }

  async function claimReviewJob(jobId, button) {
    await withButtonBusy(button, "Claiming...", async () => {
      const payload = {};
      if (state.profile.reviewerId.trim()) {
        payload.reviewer_id = state.profile.reviewerId.trim();
      }
      const response = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review/claim`, {
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
      const response = await apiRequest("review", `/v1/faxes/${encId(jobId)}/review-packet`, { method: "GET" });
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
                aria-label="Corrected value for ${escapeAttr(field.field_key)}"
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
        const templates = Array.isArray(response.data) ? response.data : [];
        renderTemplatesTable(templates);
        renderJson($("templatesOutput"), response.data);
        addActivity(`Loaded ${templates.length} templates.`, "info");
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
        $("tplUpdateId").value = (response.data && response.data.template_id) || "";
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
        const response = await apiRequest("review", `/v1/templates/${encId(templateId)}`, {
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
        await apiRequest("review", `/v1/templates/${encId(templateId)}`, { method: "DELETE" });
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
        const response = await apiRequest("review", `/v1/templates/${encId(templateId)}/versions`, {
          method: "POST",
          body: payload,
        });
        renderJson($("versionsOutput"), response.data);
        const vid = (response.data && response.data.template_version_id) || "";
        $("sampleVersionId").value = vid;
        $("fieldVersionId").value = vid;
        $("listFieldsVersionId").value = vid;
        $("verActivateId").value = vid;
        $("verUpdateId").value = vid;
        $("testExtractVersionId").value = vid;
        addActivity(`Created version ${shortId(vid)}.`, "info");
      }, $("versionsOutput"));
    });

    $("activateVersionForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Activating...", async () => {
        const versionId = $("verActivateId").value.trim();
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}/activate`, {
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
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}`, {
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
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}/samples`, {
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
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}/fields`, {
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
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}/fields`, {
          method: "GET",
        });
        const fields = Array.isArray(response.data) ? response.data : [];
        renderTemplateFieldsTable(fields);
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
        const response = await apiRequest("review", `/v1/templates/versions/${encId(versionId)}/test-extract`, {
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
        const queryData = response.data || {};
        renderQueryResultsTable(queryData.results || []);
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
        const response = await apiRequest("review", `/v1/models/${encId(modelVersionId)}/promote`, {
          method: "POST",
          body: { promoted_by: state.profile.reviewerId || "admin" },
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });

    $("updateMetricsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Updating...", async () => {
        const modelVersionId = $("metricsModelId").value.trim();
        const metrics = parseJsonText($("metricsJson").value.trim(), "Invalid metrics JSON.");
        const response = await apiRequest("review", `/v1/models/${encId(modelVersionId)}/metrics`, {
          method: "PUT",
          body: { metrics },
        });
        renderJson($("modelsOutput"), response.data);
      }, $("modelsOutput"));
    });

    $("deleteModelForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const modelVersionId = $("deleteModelId").value.trim();
      if (!window.confirm(`Delete model version ${modelVersionId}?`)) {
        return;
      }
      await withButtonBusy(event.submitter, "Deleting...", async () => {
        await apiRequest("review", `/v1/models/${encId(modelVersionId)}`, {
          method: "DELETE",
        });
        renderJson($("modelsOutput"), { deleted: modelVersionId });
      }, $("modelsOutput"));
    });
  }

  function bindApiConsole() {
    $("apiService").addEventListener("change", () => {
      const showCustom = $("apiService").value === "custom";
      $("apiCustomBaseWrap").style.display = showCustom ? "flex" : "none";
    });
    $("apiService").dispatchEvent(new Event("change"));

    $("apiConsoleForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await withButtonBusy(event.submitter, "Executing...", async () => {
        const service = $("apiService").value;
        const method = $("apiMethod").value;
        const path = $("apiPath").value.trim();
        const rawQuery = $("apiQueryString").value.trim();
        const rawBody = $("apiBody").value.trim();
        const query = parseQueryString(rawQuery);

        let body = undefined;
        if (rawBody && !["GET", "DELETE"].includes(method)) {
          body = parseJsonText(rawBody, "Invalid API console JSON body.");
        }

        const options = {
          method,
          query,
          body,
        };

        if (service === "custom") {
          options.baseUrl = $("apiCustomBase").value.trim();
          if (!options.baseUrl) {
            throw new Error("Custom base URL is required when service is custom.");
          }
        }

        const response = await apiRequest(service, path, options);
        renderJson($("apiConsoleOutput"), {
          status: response.status,
          url: response.url,
          data: response.data,
        });
      }, $("apiConsoleOutput"));
    });
  }

  function renderJobsTable(jobs) {
    $("jobsTableBody").innerHTML = jobs.map((job) => `
      <tr>
        <td><code>${escapeHtml(job.fax_job_id)}</code></td>
        <td><span class="status-chip ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></td>
        <td>${escapeHtml(job.payer_hint || "-")}</td>
        <td>${escapeHtml(job.doc_type || "-")}</td>
        <td>${job.needs_review ? "true" : "false"}</td>
        <td>${escapeHtml(job.created_at || "-")}</td>
        <td><button class="btn subtle" type="button" data-job-id="${escapeAttr(job.fax_job_id)}" aria-label="Select job ${escapeAttr(shortId(job.fax_job_id))}">Select</button></td>
      </tr>
    `).join("");
  }

  function renderReviewQueueTable(items) {
    $("reviewQueueBody").innerHTML = items.map((item) => `
      <tr>
        <td><code>${escapeHtml(item.fax_job_id)}</code></td>
        <td>${escapeHtml(String(item.priority ?? "-"))}</td>
        <td>${escapeHtml(item.claimed_by || "-")}</td>
        <td>${escapeHtml((item.review_reasons || []).join(", ") || "-")}</td>
        <td class="button-row">
          <button class="btn subtle" type="button" data-review-action="open" data-job-id="${escapeAttr(item.fax_job_id)}" aria-label="Open review for ${escapeAttr(shortId(item.fax_job_id))}">Open</button>
          <button class="btn ghost" type="button" data-review-action="claim" data-job-id="${escapeAttr(item.fax_job_id)}" aria-label="Claim review for ${escapeAttr(shortId(item.fax_job_id))}">Claim</button>
          <button class="btn primary" type="button" data-review-action="claim-open" data-job-id="${escapeAttr(item.fax_job_id)}" aria-label="Claim and open review for ${escapeAttr(shortId(item.fax_job_id))}">Claim + Open</button>
        </td>
      </tr>
    `).join("");
  }

  function renderTemplatesTable(templates) {
    $("templatesBody").innerHTML = templates.map((template) => `
      <tr>
        <td><code>${escapeHtml(template.template_id)}</code></td>
        <td>${escapeHtml(template.template_name)}</td>
        <td>${escapeHtml(template.payer_name)}</td>
        <td>${escapeHtml(template.doc_type)}</td>
        <td>${template.is_active ? "true" : "false"}</td>
      </tr>
    `).join("");
  }

  function renderTemplateFieldsTable(fields) {
    $("fieldsBody").innerHTML = fields.map((field) => `
      <tr>
        <td><code>${escapeHtml(field.field_key)}</code></td>
        <td>${escapeHtml(field.field_label || "-")}</td>
        <td>${escapeHtml(`(${field.roi_x0}, ${field.roi_y0}) - (${field.roi_x1}, ${field.roi_y1})`)}</td>
        <td>${escapeHtml(String(field.target_page))}</td>
        <td>${field.is_required ? "true" : "false"}</td>
      </tr>
    `).join("");
  }

  function renderQueryResultsTable(results) {
    $("queryResultsBody").innerHTML = results.map((result) => `
      <tr>
        <td><code>${escapeHtml(result.fax_job_id || "-")}</code></td>
        <td>${escapeHtml(result.field_key || "-")}</td>
        <td>${escapeHtml(result.value || "-")}</td>
        <td>${result.confidence === null || result.confidence === undefined ? "-" : escapeHtml(String(result.confidence))}</td>
        <td>${result.similarity_score === null || result.similarity_score === undefined ? "-" : escapeHtml(String(result.similarity_score))}</td>
      </tr>
    `).join("");
  }

  async function apiRequest(service, path, options = {}) {
    const method = options.method || "GET";
    const baseUrl = options.baseUrl || getBaseUrlForService(service);
    const serviceName = service === "custom" ? "custom" : service;
    if (!baseUrl) {
      throw withServiceName(new Error(`Missing base URL for service '${serviceName}'.`), serviceName);
    }

    const url = new URL(buildAbsoluteUrl(baseUrl, path));
    if (options.query && typeof options.query === "object") {
      for (const [key, value] of Object.entries(options.query)) {
        if (value === undefined || value === null || value === "") continue;
        url.searchParams.set(key, String(value));
      }
    }

    const headers = new Headers(options.headers || {});
    if (state.profile.token.trim()) {
      headers.set("Authorization", `Bearer ${state.profile.token.trim()}`);
    }

    let body = undefined;
    if (options.formData) {
      body = options.formData;
    } else if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(url.toString(), {
        method,
        headers,
        body,
      });
    } catch (error) {
      throw withServiceName(new Error(`Network error contacting ${serviceName}: ${error.message}`), serviceName);
    }

    const rawText = await response.text();
    const parsed = tryParseJson(rawText);
    const payload = parsed ?? rawText;

    if (!response.ok) {
      const message = extractErrorMessage(payload, response.status);
      const err = new Error(message);
      err.status = response.status;
      err.payload = payload;
      throw withServiceName(err, serviceName);
    }

    return {
      status: response.status,
      data: payload,
      url: url.toString(),
      serviceName,
    };
  }

  function withServiceName(error, serviceName) {
    error.serviceName = serviceName;
    return error;
  }

  function buildAbsoluteUrl(baseUrl, path) {
    const trimmedBase = String(baseUrl).trim().replace(/\/+$/, "");
    const safePath = String(path || "").trim();
    if (!safePath) return trimmedBase;
    if (/^https?:\/\//i.test(safePath)) return safePath;
    const normalizedPath = safePath.startsWith("/") ? safePath : `/${safePath}`;
    return `${trimmedBase}${normalizedPath}`;
  }

  function getBaseUrlForService(service) {
    if (service === "ingress") return state.profile.ingressBaseUrl.trim();
    if (service === "review") return state.profile.reviewBaseUrl.trim();
    if (service === "query") return state.profile.queryBaseUrl.trim();
    if (service === "custom") return null;
    throw new Error(`Unsupported service '${service}'`);
  }

  function tryParseJson(text) {
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  function parseJsonText(text, errorMessage) {
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(errorMessage);
    }
  }

  function extractErrorMessage(payload, fallbackStatus) {
    if (payload && typeof payload === "object") {
      if (typeof payload.detail === "string") return payload.detail;
      if (typeof payload.message === "string") return payload.message;
    }
    if (typeof payload === "string" && payload.trim()) return payload;
    return `Request failed with status ${fallbackStatus}`;
  }

  function appendIfPresent(formData, key, value) {
    if (value) formData.append(key, value);
  }

  function addIfTruthy(target, key, value) {
    if (value !== undefined && value !== null && value !== "") {
      target[key] = value;
    }
  }

  function addOptionalNumber(target, key, value) {
    if (value === "" || value === null || value === undefined) return;
    const number = Number(value);
    if (Number.isNaN(number)) return;
    target[key] = number;
  }

  function parseQueryString(raw) {
    if (!raw) return {};
    const params = new URLSearchParams(raw);
    const result = {};
    for (const [key, value] of params.entries()) {
      result[key] = value;
    }
    return result;
  }

  function renderJson(element, value) {
    if (!element) return;
    element.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function shortId(id) {
    if (!id) return "n/a";
    return String(id).slice(0, 8);
  }

  function encId(id) {
    return encodeURIComponent(String(id));
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  async function withButtonBusy(button, busyLabel, action, errorOutputElement) {
    const btn = button || null;
    const original = btn ? btn.textContent : "";
    if (btn) {
      btn.disabled = true;
      btn.textContent = busyLabel;
    }
    try {
      return await action();
    } catch (error) {
      const details = formatError(error);
      if (errorOutputElement) {
        renderJson(errorOutputElement, details);
      }
      addActivity(details.message, "error");
      toast(details.message, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
  }

  function formatError(error) {
    const details = {
      message: error.message || "Unexpected error",
      status: error.status || null,
      service: error.serviceName || null,
      payload: error.payload || null,
    };
    if (details.service && details.status) {
      details.message = `[${details.service}] ${details.message} (HTTP ${details.status})`;
    } else if (details.service) {
      details.message = `[${details.service}] ${details.message}`;
    }
    return details;
  }

  function toast(message, kind) {
    const host = $("toastHost");
    const node = document.createElement("div");
    node.className = `toast ${kind || "info"}`;
    node.textContent = message;
    host.appendChild(node);
    const duration = kind === "error" ? 7000 : 3600;
    window.setTimeout(() => {
      node.remove();
    }, duration);
  }

  function addActivity(message, kind) {
    const feed = $("activityFeed");
    const item = document.createElement("li");
    item.className = kind || "info";
    item.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    feed.prepend(item);
    while (feed.children.length > 16) {
      feed.removeChild(feed.lastChild);
    }
  }

  function hydrateProfile() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        state.profile = { ...DEFAULT_PROFILE };
        return;
      }
      const parsed = JSON.parse(raw);
      state.profile = {
        ...DEFAULT_PROFILE,
        ...parsed,
      };
    } catch {
      state.profile = { ...DEFAULT_PROFILE };
    }
  }

  function persistProfile() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.profile));
    } catch {
      // localStorage unavailable (private browsing, quota exceeded, etc.)
    }
  }

  function applyProfileToInputs() {
    $("cfgIngress").value = state.profile.ingressBaseUrl;
    $("cfgReview").value = state.profile.reviewBaseUrl;
    $("cfgQuery").value = state.profile.queryBaseUrl;
    $("cfgToken").value = state.profile.token;
    $("cfgReviewer").value = state.profile.reviewerId;
    syncStatusPills();
  }

  function saveProfileFromInputs() {
    state.profile.ingressBaseUrl = $("cfgIngress").value.trim() || DEFAULT_PROFILE.ingressBaseUrl;
    state.profile.reviewBaseUrl = $("cfgReview").value.trim() || DEFAULT_PROFILE.reviewBaseUrl;
    state.profile.queryBaseUrl = $("cfgQuery").value.trim() || DEFAULT_PROFILE.queryBaseUrl;
    state.profile.token = $("cfgToken").value.trim();
    state.profile.reviewerId = $("cfgReviewer").value.trim();
    persistProfile();
    syncStatusPills();
  }

  function syncStatusPills() {
    const usingToken = Boolean(state.profile.token && state.profile.token.trim());
    $("authPill").textContent = usingToken ? "Auth: Bearer token configured" : "Auth: Optional in development";
    $("envPill").textContent = `Review Host: ${state.profile.reviewBaseUrl || DEFAULT_PROFILE.reviewBaseUrl}`;
  }

  window.addEventListener("DOMContentLoaded", init);
})();
