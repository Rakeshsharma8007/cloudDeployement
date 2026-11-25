let selectedMonitorId = null;
let currentMonitors = [];
let responseChart = null;
let distributionChart = null;

const monitorSelect = document.getElementById("monitorSelect");
const checkNowBtn = document.getElementById("checkNowBtn");
const editTargetBtn = document.getElementById("editTargetBtn");
const addTargetBtn = document.getElementById("addTargetBtn");
const syncWorkflowsBtn = document.getElementById("syncWorkflowsBtn");

const modal = document.getElementById("monitorModal");
const modalTitle = document.getElementById("modalTitle");
const monitorForm = document.getElementById("monitorForm");
const formMonitorId = document.getElementById("formMonitorId");
const formName = document.getElementById("formName");
const formUrl = document.getElementById("formUrl");
const formInterval = document.getElementById("formInterval");
const formGithubRepo = document.getElementById("formGithubRepo");
const formError = document.getElementById("formError");

function formatTime(isoStr) {
  if (!isoStr) return "Never";
  return isoStr.replace("T", " ").replace("Z", " UTC");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function renderCharts(recentChecks, summary) {
  const chronological = [...recentChecks].reverse();
  const labels = chronological.map((c) => {
    const t = c.checked_at || "";
    return t.length >= 19 ? t.slice(11, 19) : t;
  });
  const latencies = chronological.map((c) => c.response_time_ms ?? 0);
  const pointColors = chronological.map((c) =>
    c.status === "UP" ? "#10b981" : "#ef4444"
  );

  const rtCtx = document.getElementById("responseTimeChart").getContext("2d");
  if (responseChart) {
    responseChart.data.labels = labels;
    responseChart.data.datasets[0].data = latencies;
    responseChart.data.datasets[0].pointBackgroundColor = pointColors;
    responseChart.update();
  } else {
    responseChart = new Chart(rtCtx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Response Time (ms)",
            data: latencies,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59, 130, 246, 0.14)",
            borderWidth: 2,
            fill: true,
            tension: 0.3,
            pointRadius: 3.5,
            pointBackgroundColor: pointColors,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => ` ${ctx.parsed.y} ms`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#9ca3af", maxTicksLimit: 8 },
            grid: { color: "rgba(255,255,255,0.04)" },
          },
          y: {
            beginAtZero: true,
            ticks: { color: "#9ca3af" },
            grid: { color: "rgba(255,255,255,0.06)" },
          },
        },
      },
    });
  }

  const distCtx = document
    .getElementById("statusDistributionChart")
    .getContext("2d");
  const passed = summary.successful_checks || 0;
  const failed = summary.failed_checks || 0;
  const distData = passed + failed > 0 ? [passed, failed] : [1, 0];
  const distColors =
    passed + failed > 0 ? ["#10b981", "#ef4444"] : ["#374151", "#1f2937"];

  if (distributionChart) {
    distributionChart.data.datasets[0].data = distData;
    distributionChart.data.datasets[0].backgroundColor = distColors;
    distributionChart.update();
  } else {
    distributionChart = new Chart(distCtx, {
      type: "doughnut",
      data: {
        labels: ["Successful (2xx/3xx)", "Failed / Offline"],
        datasets: [
          {
            data: distData,
            backgroundColor: distColors,
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: "#d1d5db", boxWidth: 12, padding: 14 },
          },
        },
      },
    });
  }
}

function renderDashboard(data) {
  currentMonitors = data.monitors || [];
  const active = data.active_monitor;
  selectedMonitorId = active.id;

  // Populate monitor selector
  monitorSelect.innerHTML = currentMonitors
    .map(
      (m) =>
        `<option value="${m.id}" ${m.id === active.id ? "selected" : ""}>${escapeHtml(m.name)}</option>`
    )
    .join("");

  // Target bar
  const urlLink = document.getElementById("targetUrlLink");
  urlLink.href = active.url;
  urlLink.textContent = active.url;
  document.getElementById("targetInterval").textContent = `${active.check_interval}s`;
  document.getElementById("lastCheckedAt").textContent = formatTime(
    data.summary.last_checked_at
  );

  // KPI cards
  const s = data.summary;
  const badge = document.getElementById("statusBadge");
  const banner = document.getElementById("downtimeBanner");
  const bannerText = document.getElementById("downtimeBannerText");

  if (s.current_status === "UP") {
    badge.className = "status-badge up";
    badge.textContent = "ONLINE";
    document.getElementById("statusSubtext").textContent = `HTTP ${s.last_status_code || 200} • ${s.last_response_time_ms ?? 0} ms`;
    banner.classList.add("hidden");
  } else if (s.current_status === "DOWN") {
    badge.className = "status-badge down";
    badge.textContent = "OFFLINE";
    document.getElementById("statusSubtext").textContent = s.last_status_code
      ? `HTTP ${s.last_status_code}`
      : "Connection failed / Timeout";
    bannerText.textContent = `${active.name} (${active.url}) is currently OFFLINE.`;
    banner.classList.remove("hidden");
  } else {
    badge.className = "status-badge pending";
    badge.textContent = "PENDING";
    document.getElementById("statusSubtext").textContent = "Click 'Check Now' or wait for scheduler";
    banner.classList.add("hidden");
  }

  document.getElementById("uptimeValue").textContent = `${s.uptime_percentage.toFixed(2)}%`;
  document.getElementById("avgResponseValue").textContent = `${s.avg_response_time_ms} ms`;
  document.getElementById("responseRangeSub").textContent = `Min: ${s.min_response_time_ms} ms • Max: ${s.max_response_time_ms} ms`;
  document.getElementById("totalChecksValue").textContent = s.total_checks;
  document.getElementById("checksBreakdownSub").innerHTML =
    `<span class="text-up">${s.successful_checks} passed</span> &bull; <span class="text-down">${s.failed_checks} failed</span>`;

  // Charts
  renderCharts(data.recent_checks || [], s);

  // Alerts
  const alertsEl = document.getElementById("alertsList");
  const alerts = data.alerts || [];
  if (alerts.length === 0) {
    alertsEl.innerHTML = '<div class="empty-state">No alerts recorded yet.</div>';
  } else {
    alertsEl.innerHTML = alerts
      .map((a) => {
        const badgeCls =
          a.alert_type === "RECOVERY"
            ? "up"
            : a.alert_type === "DOWNTIME"
              ? "down"
              : "pending";
        return `
          <div class="alert-item ${a.acknowledged ? "ack" : ""}">
            <div>
              <span class="status-badge ${badgeCls}">${escapeHtml(a.alert_type)}</span>
              <div style="margin-top:6px;">${escapeHtml(a.message)}</div>
              <div class="alert-meta">${formatTime(a.created_at)}</div>
            </div>
            ${a.acknowledged
            ? '<span class="alert-meta">Acked</span>'
            : `<button class="btn btn-ghost btn-sm" onclick="ackAlert(${a.id})">Acknowledge</button>`
          }
          </div>
        `;
      })
      .join("");
  }

  // GitHub Workflows
  const repoLabel = document.getElementById("workflowRepoLabel");
  repoLabel.textContent = active.github_repo
    ? `Tracking repository: ${active.github_repo}`
    : "Configure a GitHub repo (owner/repo) to track CI/CD runs";

  const wfBody = document.getElementById("workflowsTableBody");
  const workflows = data.workflows || [];
  if (workflows.length === 0) {
    wfBody.innerHTML =
      '<tr><td colspan="4" class="empty-cell">No workflow runs synced yet.</td></tr>';
  } else {
    wfBody.innerHTML = workflows
      .map((w) => {
        const conclusion = w.conclusion || w.status;
        const cls =
          conclusion === "success"
            ? "up"
            : conclusion === "failure" || conclusion === "cancelled"
              ? "down"
              : "pending";
        return `
          <tr>
            <td><a class="table-link" href="${escapeHtml(w.html_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(w.workflow_name)}</a></td>
            <td class="mono-val">#${w.run_number ?? "-"}</td>
            <td class="mono-val">${escapeHtml(w.branch || "-")}</td>
            <td><span class="status-badge ${cls}">${escapeHtml(conclusion)}</span></td>
          </tr>
        `;
      })
      .join("");
  }

  // Error history
  const errBody = document.getElementById("errorsTableBody");
  const errors = data.error_history || [];
  if (errors.length === 0) {
    errBody.innerHTML =
      '<tr><td colspan="3" class="empty-cell">No HTTP errors recorded.</td></tr>';
  } else {
    errBody.innerHTML = errors
      .map(
        (e) => `
        <tr>
          <td class="mono-val">${formatTime(e.checked_at)}</td>
          <td class="mono-val">${e.status_code ?? "ERR"}</td>
          <td>${escapeHtml(e.error_message || "Request failed")}</td>
        </tr>
      `
      )
      .join("");
  }

  // Activity table
  const actBody = document.getElementById("activityTableBody");
  const checks = data.recent_checks || [];
  if (checks.length === 0) {
    actBody.innerHTML =
      '<tr><td colspan="4" class="empty-cell">No health checks recorded yet.</td></tr>';
  } else {
    actBody.innerHTML = checks
      .slice(0, 20)
      .map((c) => {
        const cls = c.status === "UP" ? "up" : "down";
        return `
          <tr>
            <td class="mono-val">${formatTime(c.checked_at)}</td>
            <td><span class="status-badge ${cls}">${escapeHtml(c.status)}</span></td>
            <td class="mono-val">${c.status_code ?? "-"}</td>
            <td class="mono-val">${c.response_time_ms != null ? `${c.response_time_ms} ms` : "-"}</td>
          </tr>
        `;
      })
      .join("");
  }
}

async function fetchDashboard() {
  const query = selectedMonitorId ? `?monitor_id=${selectedMonitorId}` : "";
  const resp = await fetch(`/api/dashboard${query}`);
  if (!resp.ok) return;
  const data = await resp.json();
  renderDashboard(data);
}

async function triggerCheckNow() {
  checkNowBtn.disabled = true;
  checkNowBtn.textContent = "Checking...";
  try {
    await fetch("/api/check-now", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ monitor_id: selectedMonitorId }),
    });
    await fetchDashboard();
  } finally {
    checkNowBtn.disabled = false;
    checkNowBtn.innerHTML = '<span class="btn-dot"></span> Check Now';
  }
}

async function syncWorkflows() {
  if (!selectedMonitorId) return;
  syncWorkflowsBtn.disabled = true;
  syncWorkflowsBtn.textContent = "Syncing...";
  try {
    const resp = await fetch("/api/workflows/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ monitor_id: selectedMonitorId }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      alert(err.error || "Failed to sync GitHub Actions workflows.");
    }
    await fetchDashboard();
  } finally {
    syncWorkflowsBtn.disabled = false;
    syncWorkflowsBtn.textContent = "Sync Workflows";
  }
}

async function ackAlert(alertId) {
  await fetch(`/api/alerts/${alertId}/ack`, { method: "POST" });
  await fetchDashboard();
}
window.ackAlert = ackAlert;

function openModal(mode) {
  formError.classList.add("hidden");
  if (mode === "edit") {
    const active = currentMonitors.find((m) => m.id === selectedMonitorId);
    if (!active) return;
    modalTitle.textContent = "Configure Target Application";
    formMonitorId.value = active.id;
    formName.value = active.name;
    formUrl.value = active.url;
    formInterval.value = active.check_interval;
    formGithubRepo.value = active.github_repo || "";
  } else {
    modalTitle.textContent = "Add New Monitored Target";
    formMonitorId.value = "";
    formName.value = "";
    formUrl.value = "";
    formInterval.value = 60;
    formGithubRepo.value = "";
  }
  modal.classList.remove("hidden");
}

function closeModal() {
  modal.classList.add("hidden");
}

monitorSelect.addEventListener("change", (e) => {
  selectedMonitorId = Number(e.target.value);
  fetchDashboard();
});
checkNowBtn.addEventListener("click", triggerCheckNow);
syncWorkflowsBtn.addEventListener("click", syncWorkflows);
editTargetBtn.addEventListener("click", () => openModal("edit"));
addTargetBtn.addEventListener("click", () => openModal("add"));
document.getElementById("closeModalBtn").addEventListener("click", closeModal);
document.getElementById("cancelModalBtn").addEventListener("click", closeModal);

monitorForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  formError.classList.add("hidden");
  const id = formMonitorId.value;
  const payload = {
    name: formName.value.trim(),
    url: formUrl.value.trim(),
    check_interval: Number(formInterval.value),
    github_repo: formGithubRepo.value.trim(),
  };

  const endpoint = id ? `/api/monitors/${id}` : "/api/monitors";
  const method = id ? "PUT" : "POST";

  const resp = await fetch(endpoint, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const err = await resp.json();
    formError.textContent = err.error || "Failed to save monitor configuration.";
    formError.classList.remove("hidden");
    return;
  }

  const saved = await resp.json();
  selectedMonitorId = saved.id;
  closeModal();
  await fetchDashboard();
});

// Initial load and 15-second auto-refresh
fetchDashboard();
setInterval(fetchDashboard, 15000);
