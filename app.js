(() => {
  "use strict";

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";

  function getById(id) {
    return document.getElementById(id);
  }

  function createElement(tagName, className = "", text = null) {
    const element = document.createElement(tagName);

    if (className) {
      element.className = className;
    }

    if (text !== null && text !== undefined) {
      element.textContent = String(text);
    }

    return element;
  }

  function clearElement(element) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  function formatTimestamp(value) {
    if (!value) {
      return "—";
    }

    return String(value)
      .replace("T", " ")
      .replace("Z", "");
  }

  function formatNumber(value) {
    const numericValue = Number(value);

    if (!Number.isFinite(numericValue)) {
      return "—";
    }

    return new Intl.NumberFormat().format(numericValue);
  }

  function safeStatusLabel(status) {
    return status === "backed_up" ? "Backed up" : "Failed";
  }

  function statusBadgeInfo(status) {
    switch (status) {
      case "running":
        return {
          text: "Backup running",
          className: "badge-orange"
        };
      case "success":
        return {
          text: "Last run succeeded",
          className: "badge-green"
        };
      case "failed":
        return {
          text: "Last run failed",
          className: "badge-red"
        };
      default:
        return {
          text: "Idle",
          className: "badge-gray"
        };
    }
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        ...(options.headers || {})
      },
      ...options
    });

    let data = null;

    try {
      data = await response.json();
    } catch {
      data = null;
    }

    if (!response.ok) {
      const message =
        data && typeof data.error === "string"
          ? data.error
          : "The dashboard request could not be completed.";

      throw new Error(message);
    }

    return data;
  }

  // ================================================================
  // Mobile sidebar
  // ================================================================

  const sidebar = getById("sidebar");
  const sidebarToggle = getById("sidebar-toggle");
  const sidebarBackdrop = getById("sidebar-backdrop");

  function openSidebar() {
    if (!sidebar || !sidebarToggle || !sidebarBackdrop) {
      return;
    }

    sidebar.classList.add("open");
    sidebarBackdrop.hidden = false;
    sidebarBackdrop.classList.add("open");
    sidebarBackdrop.setAttribute("aria-hidden", "false");
    sidebarToggle.setAttribute("aria-expanded", "true");
  }

  function closeSidebar() {
    if (!sidebar || !sidebarToggle || !sidebarBackdrop) {
      return;
    }

    sidebar.classList.remove("open");
    sidebarBackdrop.classList.remove("open");
    sidebarBackdrop.hidden = true;
    sidebarBackdrop.setAttribute("aria-hidden", "true");
    sidebarToggle.setAttribute("aria-expanded", "false");
  }

  if (sidebar && sidebarToggle && sidebarBackdrop) {
    sidebarToggle.addEventListener("click", () => {
      if (sidebar.classList.contains("open")) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });

    sidebarBackdrop.addEventListener("click", closeSidebar);

    sidebar.querySelectorAll(".nav-link").forEach((link) => {
      link.addEventListener("click", closeSidebar);
    });

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        sidebar.classList.contains("open")
      ) {
        closeSidebar();
      }
    });
  }

  // ================================================================
  // Dashboard overview page
  // ================================================================

  const recentTable = getById("recent-table");

  if (recentTable) {
    initializeDashboardPage();
  }

  function initializeDashboardPage() {
    const elements = {
      statusBadge: getById("status-badge"),
      totalBackedUp: getById("total-backed-up"),
      totalFailed: getById("total-failed"),
      folderCount: getById("folder-count"),
      latestSuccess: getById("latest-success"),
      runButton: getById("run-btn"),
      refreshButton: getById("refresh-btn"),
      runResult: getById("run-result"),
      foldersBody: document.querySelector("#folders-table tbody"),
      foldersEmpty: getById("folders-empty"),
      recentBody: document.querySelector("#recent-table tbody"),
      recentEmpty: getById("recent-empty"),
      failedBody: document.querySelector("#failed-table tbody"),
      failedEmpty: getById("failed-empty")
    };

    let pollTimer = null;
    let isDemoMode = false;

    function appendCell(row, content, className = "") {
      const cell = createElement("td", className);

      if (content instanceof Node) {
        cell.appendChild(content);
      } else {
        cell.textContent = content ?? "—";
      }

      row.appendChild(cell);
      return cell;
    }

    function createStatusPill(status) {
      return createElement(
        "span",
        `status-pill ${status}`,
        safeStatusLabel(status)
      );
    }

    function setRunButtonState(runState) {
      if (!elements.runButton) {
        return;
      }

      const isRunning = runState.status === "running";
      const label = elements.runButton.querySelector("span");

      elements.runButton.disabled = isRunning || isDemoMode;
      elements.runButton.setAttribute(
        "aria-disabled",
        String(isRunning || isDemoMode)
      );

      if (label) {
        if (isDemoMode) {
          label.textContent = "Demo mode";
        } else if (isRunning) {
          label.textContent = "Running…";
        } else {
          label.textContent = "Run Backup Now";
        }
      }

      if (isDemoMode) {
        elements.runButton.title =
          "Demo mode is active. Real backup execution is disabled.";
      } else {
        elements.runButton.removeAttribute("title");
      }
    }

    function renderRunResult(runState) {
      if (!elements.runResult) {
        return;
      }

      clearElement(elements.runResult);

      if (runState.status === "idle") {
        elements.runResult.hidden = true;
        elements.runResult.className = "run-result";
        return;
      }

      elements.runResult.hidden = false;
      elements.runResult.className = `run-result ${runState.status}`;

      const body = createElement("div", "run-result-body");
      const heading = createElement("strong");

      if (runState.status === "running") {
        heading.textContent =
          "Backup is running. This can take longer for mailboxes with large attachments.";
      } else if (runState.status === "success") {
        const saved = runState.saved ?? "—";
        const failed = runState.failed ?? "0";

        heading.textContent =
          `Backup completed. Saved: ${saved}. Failed: ${failed}.`;
      } else {
        heading.textContent = runState.error
          ? `Backup failed: ${runState.error}`
          : "Backup did not complete successfully.";
      }

      body.appendChild(heading);

      if (runState.started_at) {
        body.appendChild(
          createElement(
            "p",
            "run-result-meta",
            `Started: ${formatTimestamp(runState.started_at)}`
          )
        );
      }

      if (runState.finished_at) {
        body.appendChild(
          createElement(
            "p",
            "run-result-meta",
            `Finished: ${formatTimestamp(runState.finished_at)}`
          )
        );
      }

      elements.runResult.appendChild(body);
    }

    function renderRunState(runState) {
      const status = statusBadgeInfo(runState.status);

      if (elements.statusBadge) {
        elements.statusBadge.textContent = status.text;
        elements.statusBadge.className =
          `badge ${status.className}`;
      }

      setRunButtonState(runState);
      renderRunResult(runState);
    }

    function renderFolders(folders) {
      if (!elements.foldersBody || !elements.foldersEmpty) {
        return;
      }

      clearElement(elements.foldersBody);

      const hasFolders =
        Array.isArray(folders) &&
        folders.length > 0;

      elements.foldersEmpty.hidden = hasFolders;

      if (!hasFolders) {
        return;
      }

      folders.forEach((folder) => {
        const row = document.createElement("tr");

        appendCell(row, folder.folder);
        appendCell(row, formatNumber(folder.backed_up));
        appendCell(row, formatNumber(folder.failed));
        appendCell(row, formatNumber(folder.last_uid));
        appendCell(row, formatNumber(folder.uidvalidity));
        appendCell(row, formatTimestamp(folder.updated_at));

        elements.foldersBody.appendChild(row);
      });
    }

    function renderRecent(records) {
      if (!elements.recentBody || !elements.recentEmpty) {
        return;
      }

      clearElement(elements.recentBody);

      const hasRecords =
        Array.isArray(records) &&
        records.length > 0;

      elements.recentEmpty.hidden = hasRecords;

      if (!hasRecords) {
        return;
      }

      records.forEach((record) => {
        const row = document.createElement("tr");

        appendCell(row, formatTimestamp(record.updated_at));
        appendCell(row, record.folder);
        appendCell(row, formatNumber(record.uid));
        appendCell(row, createStatusPill(record.status));
        appendCell(row, record.filename || "—");

        elements.recentBody.appendChild(row);
      });
    }

    function renderFailures(records) {
      if (!elements.failedBody || !elements.failedEmpty) {
        return;
      }

      clearElement(elements.failedBody);

      const hasFailures =
        Array.isArray(records) &&
        records.length > 0;

      elements.failedEmpty.hidden = hasFailures;

      if (!hasFailures) {
        return;
      }

      records.forEach((record) => {
        const row = document.createElement("tr");

        appendCell(row, formatTimestamp(record.updated_at));
        appendCell(row, record.folder);
        appendCell(row, formatNumber(record.uid));
        appendCell(row, formatNumber(record.attempts));
        appendCell(
          row,
          record.last_error || "—",
          "error-text"
        );

        elements.failedBody.appendChild(row);
      });
    }

    function showDashboardLoadError(message) {
      if (elements.statusBadge) {
        elements.statusBadge.textContent =
          "Could not load dashboard data";

        elements.statusBadge.className =
          "badge badge-red";
      }

      if (!elements.runResult) {
        return;
      }

      clearElement(elements.runResult);
      elements.runResult.hidden = false;
      elements.runResult.className =
        "run-result failed";

      const body = createElement(
        "div",
        "run-result-body"
      );

      body.appendChild(
        createElement(
          "strong",
          "",
          message || "Dashboard data could not be loaded."
        )
      );

      elements.runResult.appendChild(body);
    }

    function stopPolling() {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    async function getRunStatus() {
      try {
        return await fetchJson("/api/run-status");
      } catch {
        return null;
      }
    }

    function startPollingIfNeeded(runState) {
      if (
        runState.status !== "running" ||
        pollTimer
      ) {
        return;
      }

      pollTimer = window.setInterval(async () => {
        const currentState = await getRunStatus();

        if (!currentState) {
          return;
        }

        renderRunState(currentState);

        if (currentState.status !== "running") {
          stopPolling();
          await refreshDashboard();
        }
      }, 1500);
    }

    async function refreshDashboard() {
      try {
        const data = await fetchJson("/api/dashboard");

        isDemoMode = Boolean(data.demo_mode);

        if (elements.totalBackedUp) {
          elements.totalBackedUp.textContent =
            formatNumber(data.status_summary?.backed_up);
        }

        if (elements.totalFailed) {
          elements.totalFailed.textContent =
            formatNumber(data.status_summary?.failed);
        }

        if (elements.folderCount) {
          elements.folderCount.textContent =
            formatNumber(data.configured_folder_count);
        }

        if (elements.latestSuccess) {
          if (data.latest_success_at) {
            elements.latestSuccess.hidden = false;
            elements.latestSuccess.textContent =
              `Latest successful backup: ${formatTimestamp(
                data.latest_success_at
              )}`;
          } else {
            elements.latestSuccess.hidden = true;
            elements.latestSuccess.textContent = "";
          }
        }

        renderFolders(data.folders || []);
        renderRecent(data.recent_records || []);
        renderFailures(data.recent_failures || []);
        renderRunState(
          data.run_state || { status: "idle" }
        );

        if (
          !data.initialized &&
          elements.foldersEmpty
        ) {
          elements.foldersEmpty.hidden = false;
          elements.foldersEmpty.textContent =
            "No backups have run yet. Use Run Backup Now to begin.";
        }

        startPollingIfNeeded(
          data.run_state || { status: "idle" }
        );
      } catch (error) {
        showDashboardLoadError(error.message);
      }
    }

    async function runBackup() {
      if (isDemoMode || !elements.runButton) {
        return;
      }

      elements.runButton.disabled = true;

      try {
        const data = await fetchJson(
          "/api/backup/run",
          {
            method: "POST",
            headers: {
              "X-CSRF-Token": csrfToken
            }
          }
        );

        renderRunState(data);
        startPollingIfNeeded(data);
      } catch (error) {
        renderRunState(
          {
            status: "failed",
            error: error.message,
            started_at: null,
            finished_at: null,
            saved: null,
            failed: null
          }
        );
      }
    }

    if (elements.runButton) {
      elements.runButton.addEventListener(
        "click",
        runBackup
      );
    }

    if (elements.refreshButton) {
      elements.refreshButton.addEventListener(
        "click",
        refreshDashboard
      );
    }

    window.addEventListener(
      "beforeunload",
      stopPolling
    );

    refreshDashboard();
  }

  // ================================================================
  // Backup records page
  // ================================================================

  const recordsTable = getById("records-table");

  if (recordsTable) {
    initializeRecordsPage();
  }

  function initializeRecordsPage() {
    const elements = {
      folder: getById("filter-folder"),
      status: getById("filter-status"),
      search: getById("filter-search"),
      pageSize: getById("filter-page-size"),
      form: getById("filter-form"),
      clearButton: getById("clear-filters-btn"),
      body: document.querySelector(
        "#records-table tbody"
      ),
      empty: getById("records-empty"),
      previousButton: getById("prev-page-btn"),
      nextButton: getById("next-page-btn"),
      pageInfo: getById("page-info")
    };

    const state = {
      page: 1,
      demoMode: false
    };

    function appendRecordCell(row, content) {
      const cell = document.createElement("td");

      if (content instanceof Node) {
        cell.appendChild(content);
      } else {
        cell.textContent = content ?? "—";
      }

      row.appendChild(cell);
    }

    function createStatusPill(status) {
      return createElement(
        "span",
        `status-pill ${status}`,
        safeStatusLabel(status)
      );
    }

    function populateInitialFilters() {
      const parameters = new URLSearchParams(
        window.location.search
      );

      const initialStatus = parameters.get("status");
      const initialSearch = parameters.get("search");

      if (initialStatus) {
        elements.status.value = initialStatus;
      }

      if (initialSearch) {
        elements.search.value = initialSearch;
      }
    }

    async function populateFolderOptions() {
      try {
        const data = await fetchJson("/api/dashboard");

        state.demoMode = Boolean(data.demo_mode);

        const parameters = new URLSearchParams(
          window.location.search
        );

        const selectedFolder =
          parameters.get("folder") ||
          elements.folder.value;

        clearElement(elements.folder);

        const allFoldersOption = createElement(
          "option",
          "",
          "All folders"
        );

        allFoldersOption.value = "";
        elements.folder.appendChild(allFoldersOption);

        const folders = Array.isArray(
          data.available_folders
        )
          ? data.available_folders
          : [];

        folders.forEach((folder) => {
          const option = createElement(
            "option",
            "",
            folder
          );

          option.value = folder;
          elements.folder.appendChild(option);
        });

        if (folders.includes(selectedFolder)) {
          elements.folder.value = selectedFolder;
        }
      } catch {
        // The record page remains usable with the default folder option.
      }
    }

    function buildQuery(page) {
      const parameters = new URLSearchParams();

      if (elements.folder.value) {
        parameters.set(
          "folder",
          elements.folder.value
        );
      }

      if (elements.status.value) {
        parameters.set(
          "status",
          elements.status.value
        );
      }

      const search = elements.search.value.trim();

      if (search) {
        parameters.set("search", search);
      }

      parameters.set(
        "page_size",
        elements.pageSize.value
      );

      parameters.set("page", String(page));

      return parameters;
    }

    function showRecordError(message) {
      clearElement(elements.body);
      elements.empty.hidden = false;
      elements.empty.textContent = message;
    }

    function createOpenButton(record) {
      const button = createElement(
        "button",
        "btn-link open-file-btn",
        "Download"
      );

      button.type = "button";

      button.addEventListener("click", async () => {
        await downloadBackupFile(record, button);
      });

      return button;
    }

    function renderRecordRows(records) {
      clearElement(elements.body);

      const hasRecords =
        Array.isArray(records) &&
        records.length > 0;

      elements.empty.hidden = hasRecords;

      if (!hasRecords) {
        return;
      }

      records.forEach((record) => {
        const row = document.createElement("tr");

        appendRecordCell(
          row,
          formatTimestamp(record.updated_at)
        );

        appendRecordCell(row, record.folder);

        appendRecordCell(
          row,
          formatNumber(record.uid)
        );

        appendRecordCell(
          row,
          createStatusPill(record.status)
        );

        appendRecordCell(
          row,
          formatNumber(record.attempts)
        );

        appendRecordCell(
          row,
          record.filename || "—"
        );

        const actionCell = document.createElement("td");

        if (
          record.status === "backed_up" &&
          !state.demoMode
        ) {
          actionCell.appendChild(
            createOpenButton(record)
          );
        } else if (
          record.status === "backed_up" &&
          state.demoMode
        ) {
          actionCell.textContent = "Demo only";
        }

        row.appendChild(actionCell);
        elements.body.appendChild(row);
      });
    }

    async function downloadBackupFile(record, button) {
      if (state.demoMode) {
        return;
      }

      const originalText = button.textContent;

      button.disabled = true;
      button.textContent = "Preparing…";

      try {
        const response = await fetch(
          "/api/open-file",
          {
            method: "POST",
            headers: {
              Accept: "message/rfc822",
              "Content-Type": "application/json",
              "X-CSRF-Token": csrfToken
            },
            body: JSON.stringify(
              {
                folder: record.folder,
                uidvalidity: record.uidvalidity,
                uid: record.uid
              }
            )
          }
        );

        if (!response.ok) {
          let message =
            "The selected backup file could not be downloaded.";

          try {
            const errorData = await response.json();

            if (
              errorData &&
              typeof errorData.error === "string"
            ) {
              message = errorData.error;
            }
          } catch {
            // Use generic safe error.
          }

          throw new Error(message);
        }

        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);

        const downloadLink = document.createElement("a");

        downloadLink.href = objectUrl;
        downloadLink.download =
          record.filename || "email-backup.eml";

        downloadLink.hidden = true;

        document.body.appendChild(downloadLink);
        downloadLink.click();
        downloadLink.remove();

        window.setTimeout(
          () => URL.revokeObjectURL(objectUrl),
          1000
        );
      } catch (error) {
        window.alert(
          error.message ||
          "The backup file could not be downloaded."
        );
      } finally {
        button.disabled = false;
        button.textContent = originalText;
      }
    }

    async function loadRecords(page) {
      const parameters = buildQuery(page);

      try {
        const data = await fetchJson(
          `/api/records?${parameters.toString()}`
        );

        state.page = data.page;
        state.demoMode = Boolean(data.demo_mode);

        renderRecordRows(data.records || []);

        elements.pageInfo.textContent =
          `Page ${data.page} of ${data.total_pages} — ` +
          `${data.total_count} record` +
          `${data.total_count === 1 ? "" : "s"}`;

        elements.previousButton.disabled =
          data.page <= 1;

        elements.nextButton.disabled =
          data.page >= data.total_pages;
      } catch (error) {
        showRecordError(
          error.message ||
          "Could not load records."
        );

        elements.previousButton.disabled = true;
        elements.nextButton.disabled = true;
      }
    }

    elements.form.addEventListener(
      "submit",
      (event) => {
        event.preventDefault();
        loadRecords(1);
      }
    );

    elements.clearButton.addEventListener(
      "click",
      () => {
        elements.folder.value = "";
        elements.status.value = "";
        elements.search.value = "";
        elements.pageSize.selectedIndex = 0;

        loadRecords(1);
      }
    );

    elements.previousButton.addEventListener(
      "click",
      () => {
        loadRecords(
          Math.max(1, state.page - 1)
        );
      }
    );

    elements.nextButton.addEventListener(
      "click",
      () => {
        loadRecords(state.page + 1);
      }
    );

    populateInitialFilters();

    populateFolderOptions().then(() => {
      loadRecords(1);
    });
  }
})();