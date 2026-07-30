/**
 * AI Job Apply Agent — Dashboard
 */

// ── Known job board hostnames (mirrors ingestion/url_utils.py) ──────────────
const JOB_HOSTS = [
    'greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'workday.com',
    'linkedin.com', 'indeed.com', 'glassdoor.com', 'ziprecruiter.com',
    'angel.co', 'wellfound.com', 'otta.com', 'remote.co',
    'weworkremotely.com', 'jobvite.com', 'icims.com', 'smartrecruiters.com',
    'ashbyhq.com', 'rippling.com', 'bamboohr.com', 'workable.com',
    'recruitee.com', 'teamtailor.com', 'amazon.jobs', 'careers.google.com',
    'careers.microsoft.com', 'comeet.com', 'comeet.co',
];

const SHORT_HOSTS = [
    'bit.ly', 't.co', 'goo.gl', 'tinyurl.com', 'ow.ly', 'lnkd.in',
    'rb.gy', 'cutt.ly', 'buff.ly', 'tiny.cc', 'is.gd', 's.id',
];

function runtimeMeta(name) {
    return document.querySelector(`meta[name="${name}"]`)?.content || '';
}

const LOADED_DASHBOARD_RELEASE = Object.freeze({
    build_sha: runtimeMeta('job-agent-build-sha'),
    ui_asset_digest: runtimeMeta('job-agent-ui-digest'),
    source_digest: runtimeMeta('job-agent-source-digest'),
    protocol_version: runtimeMeta('job-agent-protocol'),
    boot_id: runtimeMeta('job-agent-boot-id'),
});
const QUALIFIED_MATERIAL_PROMPT_VERSION = 'application-materials-v1';
const QUALIFIED_FORM_PROMPT_VERSION = 'form-resolution-v1';
const OPERATIONS_ARRAY_LIMIT = 16;
const OPERATION_DEPENDENCY_NAMES = Object.freeze([
    'database',
    'redis',
    'migration',
    'shared_storage',
    'worker',
    'beat',
    'browser',
    'llm',
]);

// ── State ────────────────────────────────────────────────────────────────────
const state = {
    currentTab: 'dashboard',
    authToken: '',
    dashboardData: null,
    applications: [],
    selectedApplications: new Set(),
    jobs: [],
    urls: [],
    messages: [],
    jobSearch: '',
    filters: {
        applications: 'draft',
        jobs: '',
        urls: '',
    },
    autoRefresh: false,
    autoRefreshTimer: null,
    runtimeCapabilities: null,
    readiness: null,
    runtimeProbeStatus: 'loading',
    adapterCapabilities: null,
    adapterProbeStatus: 'loading',
    operationsData: null,
    operationsProbeStatus: 'loading',
    onboardingProfile: null,
    onboardingDirty: false,
    sendIdempotencyKeys: new Map(),
};

const reviewModalState = {
    applicationId: null,
    requestToken: 0,
};

function beginReviewModalRequest(applicationId) {
    reviewModalState.applicationId = applicationId;
    reviewModalState.requestToken += 1;
    return reviewModalState.requestToken;
}

function isCurrentReviewModalRequest(applicationId, requestToken) {
    return reviewModalState.applicationId === applicationId
        && reviewModalState.requestToken === requestToken;
}

function invalidateReviewModalRequest() {
    reviewModalState.applicationId = null;
    reviewModalState.requestToken += 1;
}

function hideModal(modal) {
    if (!modal) return;
    if (modal.id === 'review-modal') invalidateReviewModalRequest();
    modal.classList.remove('visible');
}

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const tabs = () => document.querySelectorAll('.stat-item[data-tab]');
const views = () => document.querySelectorAll('.view');
const appFilters = () => document.querySelectorAll('#view-applications .filter-btn');
const jobFilters = () => document.querySelectorAll('#view-jobs .filter-btn');
const urlFilters = () => document.querySelectorAll('#view-urls .filter-btn');

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();

    // Keep the bearer token only for this browser tab. The server must never
    // embed SECRET_KEY in HTML, and persistent localStorage unnecessarily
    // extends the impact of a browser-profile or XSS compromise.
    localStorage.removeItem('job_agent_token');
    const saved = sessionStorage.getItem('job_agent_token');
    if (saved) {
        state.authToken = saved;
        $('api-secret').value = saved;
    }

    // Set full webhook URL + bridge agent URL in WhatsApp view
    $('wa-webhook-url').textContent = `${location.origin}/webhook/whatsapp`;
    $('wa-bridge-agent-url').textContent = location.origin;

    setupListeners();
    setupResumeUpload();
    setupOnboarding();
    refreshAllData();
});

// ── Event Wiring ──────────────────────────────────────────────────────────────
function setupListeners() {
    // Tab switching
    tabs().forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

    // App filters
    appFilters().forEach(btn => btn.addEventListener('click', e => {
        appFilters().forEach(b => b.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.filters.applications = e.currentTarget.dataset.status;
        state.selectedApplications.clear();
        renderApplications();
    }));
    const batchApproveBtn = $('btn-batch-approve');
    if (batchApproveBtn) batchApproveBtn.addEventListener('click', handleBatchApprove);

    // Job filters
    jobFilters().forEach(btn => btn.addEventListener('click', e => {
        jobFilters().forEach(b => b.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.filters.jobs = e.currentTarget.dataset.status;
        fetchJobs();
    }));

    // URL Queue filters
    urlFilters().forEach(btn => btn.addEventListener('click', e => {
        urlFilters().forEach(b => b.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.filters.urls = e.currentTarget.dataset.urlStatus;
        renderUrls();
    }));

    // Job search
    $('jobs-search').addEventListener('input', e => {
        state.jobSearch = e.target.value.toLowerCase();
        renderJobs();
    });

    // Auth token
    $('api-secret').addEventListener('change', e => {
        state.authToken = e.target.value;
        sessionStorage.setItem('job_agent_token', state.authToken);
        refreshAllData();
    });

    // Governor kill / resume
    $('btn-kill').addEventListener('click', handleKill);
    $('btn-resume').addEventListener('click', handleResume);

    // Refresh button
    $('btn-refresh').addEventListener('click', () => {
        const icon = $('btn-refresh');
        icon.classList.add('spinning');
        refreshAllData().finally(() => setTimeout(() => icon.classList.remove('spinning'), 600));
    });

    // Auto-refresh toggle
    $('btn-auto-refresh').addEventListener('click', toggleAutoRefresh);

    // Export CSV
    $('btn-export-csv').addEventListener('click', exportJobsCSV);

    // Close modals
    document.querySelectorAll('.close-btn').forEach(btn => {
        btn.addEventListener('click', e => hideModal(e.target.closest('.modal')));
    });
    document.querySelectorAll('.modal').forEach(m => {
        m.addEventListener('click', e => { if (e.target === m) hideModal(m); });
    });

    // Ingest modal open
    $('btn-ingest-modal').addEventListener('click', openIngestModal);
    $('btn-cancel-ingest').addEventListener('click', () => $('ingest-modal').classList.remove('visible'));

    // Paste button
    $('btn-paste').addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            $('ingest-url').value = text;
            validateIngestInput();
        } catch {
            showToast('Clipboard access denied — paste manually', 'warning');
        }
    });

    // URL hint as user types
    $('ingest-url').addEventListener('input', validateIngestInput);
    $('ingest-url').addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitIngest();
    });

    // Submit ingest
    $('btn-submit-url').addEventListener('click', submitIngest);

    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
        if (e.key === 'r' && !e.ctrlKey && !e.metaKey && !isInputFocused()) refreshAllData();
        if ((e.key === 'k') && (e.ctrlKey || e.metaKey)) { e.preventDefault(); openIngestModal(); }
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.visible').forEach(hideModal);
        }
    });
}

function isInputFocused() {
    const tag = document.activeElement?.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA';
}

// ── Tab Switching ─────────────────────────────────────────────────────────────
const TAB_TITLES = {
    dashboard: 'Dashboard',
    applications: 'Preparation',
    jobs: 'Job Pipeline',
    urls: 'URL Queue',
    whatsapp: 'WhatsApp',
};

function switchTab(tabId) {
    state.currentTab = tabId;
    tabs().forEach(t => t.classList.remove('active'));
    document.querySelector(`.stat-item[data-tab="${tabId}"]`).classList.add('active');
    views().forEach(v => v.classList.remove('active'));
    $(`view-${tabId}`).classList.add('active');
    $('page-title').textContent = TAB_TITLES[tabId] || tabId;

    if (tabId === 'applications' && state.applications.length === 0) fetchApplications();
    if (tabId === 'jobs' && state.jobs.length === 0) fetchJobs();
    if (tabId === 'urls') fetchUrls();
    if (tabId === 'whatsapp') { fetchMessages(); fetchBridgeStatus(); }
}

// ── API Layer ──────────────────────────────────────────────────────────────────
function boundedApiError(body, fallback) {
    const detail = body?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (detail && typeof detail === 'object') {
        if (typeof detail.message === 'string' && detail.message.trim()) return detail.message;
        if (typeof detail.code === 'string' && detail.code.trim()) return detail.code;
    }
    return fallback;
}

async function apiCall(endpoint, method = 'GET', body = null) {
    const headers = { 'Content-Type': 'application/json' };
    if (state.authToken) headers['Authorization'] = `Bearer ${state.authToken}`;
    const config = { method, headers };
    if (body) config.body = JSON.stringify(body);
    try {
        const res = await fetch(endpoint, config);
        if (res.status === 401 || res.status === 403) {
            if (state.authToken) {
                showToast('Authentication failed. Check API Secret.', 'error');
            } else {
                console.warn("Backend requires auth, but no token provided.");
            }
            return null;
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(boundedApiError(err, `HTTP ${res.status}`));
        }
        return await res.json();
    } catch (err) {
        showToast(err.message, 'error');
        return null;
    }
}

async function probeJson(endpoint, method = 'GET', body = null) {
    const headers = { 'Content-Type': 'application/json' };
    if (state.authToken) headers['Authorization'] = `Bearer ${state.authToken}`;
    try {
        const config = { method, headers };
        if (body) config.body = JSON.stringify(body);
        const response = await fetch(endpoint, config);
        const data = await response.json().catch(() => null);
        return { ok: response.ok, status: response.status, data };
    } catch {
        return { ok: false, status: 0, data: null };
    }
}

async function refreshAllData() {
    await fetchRuntimeStatus();
    await fetchDashboard();
    await fetchOperationsDashboard();
    await fetchOverview();
    // Always keep jobs current (used by dashboard histogram + CSV export)
    await fetchJobs();
    await fetchProfileSummary();
    await fetchOnboardingProfile();
    if (state.currentTab === 'applications') await fetchApplications();
    if (state.currentTab === 'urls') await fetchUrls();
    if (state.currentTab === 'whatsapp') { await fetchMessages(); await fetchBridgeStatus(); }
}

// ── Runtime safety / effective mode ──────────────────────────────────────────
async function fetchRuntimeStatus() {
    const [capabilities, readiness, adapters] = await Promise.all([
        probeJson('/api/runtime/capabilities'),
        probeJson('/health/ready'),
        probeJson('/api/ats/adapters'),
    ]);

    state.runtimeCapabilities = capabilities.ok ? capabilities.data : null;
    state.readiness = state.runtimeCapabilities?.readiness || readiness.data || null;
    state.adapterCapabilities = adapters.ok && Array.isArray(adapters.data)
        ? adapters.data
        : null;
    state.adapterProbeStatus = adapters.ok
        ? 'available'
        : adapters.status === 401 || adapters.status === 403
            ? 'authentication_required'
            : 'unavailable';
    state.runtimeProbeStatus = capabilities.ok
        ? 'available'
        : capabilities.status === 401 || capabilities.status === 403
            ? 'authentication_required'
            : 'unavailable';
    renderRuntimeModeBanner();

    if (state.applications.length) renderApplications();
}

function humanizeReasonCode(value) {
    return String(value || '')
        .toLowerCase()
        .replace(/_/g, ' ')
        .replace(/\b\w/g, char => char.toUpperCase());
}

function runtimeSubmissionState() {
    const capabilities = state.runtimeCapabilities;
    const mode = capabilities?.mode || {};
    const readiness = capabilities?.readiness || state.readiness || {};
    const submission = capabilities?.submission || {};
    const worker = capabilities?.worker || {};
    const release = capabilities?.release || {};
    const automation = capabilities?.automation || {};
    const reasons = [];

    if (!capabilities) {
        reasons.push(
            state.runtimeProbeStatus === 'authentication_required'
                ? 'Enter the API Secret to verify submission safety'
                : 'Runtime capabilities are unavailable'
        );
    }
    if (mode.dry_run === true) reasons.push('DRY_RUN is enabled');
    if (mode.draft_only === true) reasons.push('Draft-only mode is enabled');
    if (mode.live_submit_enabled !== true) reasons.push('Live submission is disabled');
    if (readiness.status !== 'ready') {
        const failedChecks = Object.entries(readiness.checks || {})
            .filter(([, result]) => result !== true && result?.ok !== true)
            .map(([name]) => name.replace(/_/g, ' '));
        reasons.push(
            failedChecks.length
                ? `Dependencies unavailable: ${failedChecks.join(', ')}`
                : 'Runtime readiness is degraded'
        );
    }
    if (capabilities && automation.submission_ready !== true) {
        const stageReasons = automation.stages?.submission?.reason_codes || [];
        reasons.push(
            stageReasons.length
                ? `Submission onboarding: ${stageReasons.map(humanizeReasonCode).join(', ')}`
                : 'Submission onboarding is incomplete'
        );
    }
    if (worker.compatible === false) reasons.push('Runner build is incompatible');
    const loadedReleaseValues = Object.values(LOADED_DASHBOARD_RELEASE);
    const loadedReleaseKnown = !loadedReleaseValues.some(
        value => !value || value === 'unavailable'
    );
    const releaseMatches = Boolean(
        capabilities
        && loadedReleaseKnown
        && release.build_sha === LOADED_DASHBOARD_RELEASE.build_sha
        && release.ui_asset_digest === LOADED_DASHBOARD_RELEASE.ui_asset_digest
        && release.source_digest === LOADED_DASHBOARD_RELEASE.source_digest
        && release.protocol_version === LOADED_DASHBOARD_RELEASE.protocol_version
        && release.boot_id === LOADED_DASHBOARD_RELEASE.boot_id
    );
    if (!loadedReleaseKnown) {
        reasons.push('Dashboard release identity is unavailable');
    } else if (capabilities && !releaseMatches) {
        reasons.push('Dashboard and API releases do not match; reload this page');
    }
    for (const reason of submission.reasons || []) {
        const text = typeof reason === 'string'
            ? reason
            : reason?.message || reason?.code;
        if (text) reasons.push(String(text).replace(/_/g, ' '));
    }
    if (capabilities && submission.allowed !== true && reasons.length === 0) {
        reasons.push('Runtime did not authorize live submission');
    }

    const allowed = capabilities !== null
        && submission.allowed === true
        && mode.live_submit_enabled === true
        && mode.dry_run !== true
        && mode.draft_only !== true
        && readiness.status === 'ready'
        && automation.submission_ready === true
        && worker.compatible !== false
        && releaseMatches;

    return {
        allowed,
        reasons: [...new Set(reasons)],
        modeName: mode.name || (mode.dry_run ? 'dry run' : mode.draft_only ? 'draft only' : 'unknown'),
        release,
    };
}

function renderRuntimeModeBanner() {
    const banner = $('runtime-mode-banner');
    if (!banner) return;

    const runtime = runtimeSubmissionState();
    const capabilities = state.runtimeCapabilities;
    const mode = capabilities?.mode || {};
    const automation = capabilities?.automation || {};
    const isSafeMode = mode.dry_run === true || mode.draft_only === true;
    const variant = runtime.allowed ? 'live' : isSafeMode ? 'safe' : 'blocked';
    const title = runtime.allowed
        ? 'Live Send is available — final submission is always explicit'
        : isSafeMode
            ? 'Preparation only — this mode cannot submit an application'
            : 'Send application is disabled';
    const detail = runtime.allowed
        ? 'A green result appears only after the employer confirms this exact attempt.'
        : runtime.reasons.join(' · ') || 'Submission safety could not be verified.';
    const build = runtime.release.build_sha
        ? String(runtime.release.build_sha).slice(0, 10)
        : 'unknown';
    const protocol = runtime.release.protocol_version || 'unknown';
    const llm = capabilities?.llm;
    const llmLabel = llm
        ? `${llm.provider} ${llm.model} · ${llm.ready && llm.local ? 'local ready' : llm.reason_code || 'not ready'}`
        : 'local model unavailable';
    const modeStates = [
        {
            label: 'Discovery',
            enabled: mode.discovery_enabled === true,
            ready: automation.discovery_ready === true,
        },
        {
            label: 'Auto-prepare',
            enabled: mode.auto_prepare_enabled === true,
            ready: automation.preparation_ready === true,
        },
        {
            label: 'Qualified autopilot',
            enabled: mode.qualified_autopilot_enabled === true,
            ready: automation.submission_ready === true,
        },
    ];
    const modeStateHtml = modeStates.map(item => {
        const status = !item.enabled ? 'off' : item.ready ? 'ready' : 'blocked';
        return `<span class="automation-mode-state is-${status}">${esc(item.label)}: ${status}</span>`;
    }).join('');

    banner.className = `runtime-mode-banner runtime-mode-${variant}`;
    banner.innerHTML = `
        <i data-lucide="${runtime.allowed ? 'shield-check' : isSafeMode ? 'file-clock' : 'shield-alert'}"></i>
        <div class="runtime-mode-copy">
            <div class="runtime-mode-title">${esc(title)}</div>
            <div class="runtime-mode-detail">${esc(detail)}</div>
            <div class="automation-mode-states" aria-label="Automation modes">
                ${modeStateHtml}
            </div>
        </div>
        <div class="runtime-mode-meta" aria-label="Runtime release information">
            <span>${esc(runtime.modeName)}</span>
            <span>build ${esc(build)}</span>
            <span>protocol ${esc(protocol)}</span>
            <span>${esc(llmLabel)}</span>
        </div>`;

    const indicator = $('runtime-status-indicator');
    const indicatorText = $('runtime-status-text');
    const indicatorDot = $('runtime-status-dot');
    if (indicator) indicator.title = detail;
    if (indicatorText) {
        indicatorText.textContent = runtime.allowed
            ? 'Live Send Ready'
            : isSafeMode
                ? 'Preparation Only'
                : 'Send Disabled';
    }
    if (indicatorDot) {
        indicatorDot.className = `dot ${runtime.allowed ? 'mode-live' : isSafeMode ? 'mode-safe' : 'mode-blocked'}`;
    }
    lucide.createIcons();
}

// ── Rendering: Automation Control (governor / budget / needs-review) ──────────
async function fetchOverview() {
    const data = await apiCall('/api/control/overview');
    if (!data) return;
    state.overviewData = data;
    renderOverview(data);
}

function renderOverview(data) {
    const gov = data.governor || {};
    const counts = data.counts || {};
    const needsReview = data.needs_review || [];
    const discovery = data.discovery || [];

    // Gauge: remaining vs. today's total (remaining + already used)
    const remaining = gov.remaining ?? 0;
    const used = gov.applications_today ?? 0;
    const total = remaining + used;
    const pct = total > 0 ? Math.round((remaining / total) * 100) : 100;

    const fill = $('governor-gauge-fill');
    if (fill) {
        fill.style.width = `${pct}%`;
        fill.style.background = pct <= 20 ? 'var(--danger)' : pct <= 50 ? 'var(--warning)' : 'var(--success)';
    }

    const remainingText = $('governor-remaining-text');
    if (remainingText) {
        remainingText.textContent = total > 0
            ? `${remaining} of ${total} remaining today`
            : `${remaining} remaining today`;
    }

    const killedBadge = $('governor-killed-badge');
    if (killedBadge) killedBadge.style.display = gov.killed ? 'inline-flex' : 'none';

    const cooldownEl = $('governor-cooldown');
    if (cooldownEl) {
        if (gov.in_cooldown) {
            cooldownEl.style.display = 'inline-flex';
            cooldownEl.innerHTML = `<i data-lucide="timer" style="width:12px;height:12px;"></i> Cooldown: ${fmtDuration(gov.cooldown_remaining_s || 0)}`;
        } else {
            cooldownEl.style.display = 'none';
            cooldownEl.innerHTML = '';
        }
    }

    const btnKill = $('btn-kill');
    const btnResume = $('btn-resume');
    if (btnKill) btnKill.disabled = !!gov.killed;
    if (btnResume) btnResume.disabled = !gov.killed;

    // Counts row
    const countsRow = $('overview-counts-row');
    if (countsRow) {
        const items = [
            { label: 'Applied Today', value: counts.applied ?? 0, icon: 'send' },
            { label: 'Needs Review', value: counts.needs_review ?? 0, icon: 'alert-triangle' },
            { label: 'Failed', value: counts.failed ?? 0, icon: 'x-circle' },
            { label: 'Outbound Sent', value: counts.outbound_sent ?? 0, icon: 'message-circle' },
        ];
        countsRow.innerHTML = items.map(it => `
            <div class="count-chip">
                <i data-lucide="${it.icon}" style="width:14px;height:14px;"></i>
                <span class="count-chip-value">${it.value}</span>
                <span class="count-chip-label">${esc(it.label)}</span>
            </div>`).join('');
    }

    const discoveryRow = $('discovery-status-row');
    if (discoveryRow) {
        discoveryRow.innerHTML = discovery.length
            ? discovery.map(run => {
                const label = run.source === 'linkedin_search' ? 'LinkedIn' : 'Public Remote Jobs';
                const blocked = ['challenge', 'failed', 'blocked'].includes(run.status);
                const detail = run.reason_code === 'CHALLENGE_DETECTED'
                    ? 'Sign-in/security check required'
                    : run.reason_code === 'PROFILE_INCOMPLETE'
                        ? 'Complete your real candidate profile'
                        : String(run.reason_code || '').startsWith('PROFILE_')
                            ? humanizeReasonCode(run.reason_code)
                        : `${run.inserted || 0} new jobs`;
                return `<div class="discovery-source-card ${blocked ? 'is-blocked' : ''}">
                    <div class="discovery-source-title">${esc(label)}</div>
                    <span class="status-badge status-${esc(run.status)}">${esc(run.status)}</span>
                    <div class="discovery-source-detail">${esc(detail)}</div>
                </div>`;
            }).join('')
            : '<div class="text-sm text-dim">Discovery has not run yet.</div>';
    }

    // Needs-review table
    const tbody = $('needs-review-table-body');
    if (tbody) {
        if (!needsReview.length) {
            tbody.innerHTML = `<tr><td colspan="2" style="text-align:center;padding:24px;color:var(--text-muted);">No applications need review</td></tr>`;
        } else {
            tbody.innerHTML = needsReview.map(r => `
                <tr>
                    <td>${esc(r.title || '—')}</td>
                    <td style="color:var(--text-dim);">${esc(r.reason || '—')}</td>
                </tr>`).join('');
        }
    }

    lucide.createIcons();
}

function fmtDuration(secs) {
    secs = Math.max(0, Math.floor(secs));
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

async function handleKill() {
    const btn = $('btn-kill');
    btn.disabled = true;
    const res = await apiCall('/api/control/kill', 'POST');
    if (res) {
        showToast('Kill switch activated — automation paused', 'warning');
        await fetchOverview();
    } else {
        btn.disabled = false;
    }
}

async function handleResume() {
    const btn = $('btn-resume');
    btn.disabled = true;
    const res = await apiCall('/api/control/resume', 'POST');
    if (res) {
        showToast('Automation resumed', 'info');
        await fetchOverview();
    } else {
        btn.disabled = false;
    }
}

async function fetchDashboard() {
    const data = await apiCall('/api/dashboard');
    if (!data) return;
    state.dashboardData = data;
    renderDashboard();
    const pending = data.applications_pending;
    const badge = $('nav-pending-count');
    badge.textContent = pending;
    badge.style.display = pending > 0 ? 'inline-block' : 'none';
}

async function fetchOperationsDashboard() {
    const result = await probeJson('/api/dashboard/operations');
    state.operationsData = result.ok && result.data && typeof result.data === 'object'
        ? result.data
        : null;
    state.operationsProbeStatus = result.ok
        ? 'available'
        : result.status === 401 || result.status === 403
            ? 'authentication_required'
            : 'unavailable';
    renderOperationsDashboard();
}

async function fetchApplications() {
    const data = await apiCall('/api/applications');
    if (!data) return;
    state.applications = data;
    renderApplications();
}

async function fetchJobs() {
    const s = state.filters.jobs;
    const url = s ? `/api/jobs?status=${s}&limit=200` : '/api/jobs?limit=200';
    const data = await apiCall(url);
    if (!data) return;
    state.jobs = data;
    renderJobs();
}

async function fetchMessages() {
    const data = await apiCall('/api/messages?limit=30');
    if (!data) return;
    state.messages = Array.isArray(data) ? data : (data.items || []);
    renderMessages();
    const waBadge = $('nav-wa-count');
    waBadge.textContent = state.messages.length;
    waBadge.style.display = state.messages.length > 0 ? 'inline-block' : 'none';
}

// ── Rendering: Dashboard ───────────────────────────────────────────────────────
function renderDashboard() {
    if (!state.dashboardData) return;
    const d = state.dashboardData;

    const successRate = d.submissions_total > 0
        ? Math.round((d.submissions_success / d.submissions_total) * 100)
        : null;
    const skipRate = d.total_jobs > 0
        ? Math.round((d.jobs_skipped / d.total_jobs) * 100)
        : null;

    $('dashboard-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="inbox"></i> Messages Received</div>
            <div class="stat-value count-anim">${d.total_messages ?? 0}</div>
            <div class="stat-sub">${d.total_urls ?? 0} URLs extracted</div>
        </div>
        <div class="stat-card clickable" onclick="switchTab('jobs')" title="View all jobs">
            <div class="stat-header"><i data-lucide="briefcase"></i> Jobs Found</div>
            <div class="stat-value count-anim">${d.total_jobs ?? 0}</div>
            <div class="stat-sub">+${d.jobs_last_7d ?? 0} this week</div>
        </div>
        <div class="stat-card warning-card clickable" onclick="switchTab('applications')" title="Review drafts">
            <div class="stat-header"><i data-lucide="file-check-2" style="color:var(--warning)"></i> Awaiting Review</div>
            <div class="stat-value count-anim text-warning">${d.applications_pending ?? 0}</div>
            <div class="stat-sub">Draft applications</div>
        </div>
        <div class="stat-card success-card">
            <div class="stat-header"><i data-lucide="badge-check" style="color:var(--success)"></i> Employer Verified</div>
            <div class="stat-value count-anim text-success">${d.submissions_success ?? 0}</div>
            <div class="stat-sub">confirmed of ${d.submissions_total ?? 0} attempts</div>
        </div>

        <div class="stat-card">
            <div class="stat-header"><i data-lucide="target"></i> Avg Match Score</div>
            <div class="stat-value count-anim">${d.avg_job_score ?? '—'}</div>
            <div class="stat-sub">Best: ${d.top_job_score ?? '—'} / 100</div>
        </div>
        <div class="stat-card clickable" onclick="switchTab('jobs'); applyJobFilter('skipped');" title="View skipped jobs">
            <div class="stat-header"><i data-lucide="skip-forward"></i> Jobs Skipped</div>
            <div class="stat-value count-anim">${d.jobs_skipped ?? 0}</div>
            <div class="stat-sub">${skipRate !== null ? skipRate + '% skip rate' : 'No jobs yet'}</div>
        </div>
        <div class="stat-card ${successRate !== null && successRate < 50 ? 'warning-card' : ''}">
            <div class="stat-header"><i data-lucide="percent"></i> Verified Submit Rate</div>
            <div class="stat-value count-anim ${successRate !== null && successRate < 50 ? 'text-warning' : 'text-success'}">${successRate !== null ? successRate + '%' : '—'}</div>
            <div class="stat-sub">${d.submission_failures ?? 0} failure${d.submission_failures !== 1 ? 's' : ''}</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="message-square-text"></i> Cover Letter Edits</div>
            <div class="stat-value count-anim">${d.feedback_count ?? 0}</div>
            <div class="stat-sub">Human corrections used as prompt examples</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="scan-search"></i> Browser Qualification</div>
            <div class="stat-value count-anim">${d.browser_qualification_runs ?? 0}</div>
            <div class="stat-sub">See the bounded operational failure clusters below</div>
        </div>
    `;

    lucide.createIcons();
    renderUrlHealthBanner(d);
    renderPipelineFunnel(d);
    renderScoreHistogram(d);
    renderActivityFeed();
}

function boundedOperationalItems(value, limit = OPERATIONS_ARRAY_LIMIT) {
    if (!Array.isArray(value)) return [];
    return value
        .filter(item => item && typeof item === 'object' && !Array.isArray(item))
        .slice(0, Math.max(0, Math.min(limit, OPERATIONS_ARRAY_LIMIT)));
}

function operationalCount(value) {
    const count = Number(value);
    return Number.isFinite(count) && count >= 0 ? Math.floor(count) : 0;
}

function operationalToken(value, fallback = 'unknown') {
    if (value === null || value === undefined || value === '') return fallback;
    return String(value).replace(/_/g, ' ');
}

function shortOperationalToken(value, maxLength = 18) {
    if (value === null || value === undefined || value === '') return 'unknown';
    const token = String(value);
    return token.length > maxLength ? `${token.slice(0, maxLength)}…` : token;
}

function dependencyFallbackFromReadiness() {
    const checks = state.runtimeCapabilities?.readiness?.checks
        || state.readiness?.checks
        || {};
    return OPERATION_DEPENDENCY_NAMES
        .filter(name => Object.prototype.hasOwnProperty.call(checks, name))
        .map(name => {
            const check = checks[name];
            const ok = check === true || check?.ok === true;
            return {
                name,
                status: ok ? 'ready' : 'degraded',
                ok,
                reason_code: ok ? null : check?.reason_code || null,
                last_seen_at: check?.last_seen_at || check?.seen_at || null,
            };
        });
}

function adapterFallbackFromRuntime() {
    return boundedOperationalItems(state.adapterCapabilities).map(adapter => ({
        ats: adapter.ats,
        adapter_version: adapter.adapter_version,
        selector_version: adapter.selector_version,
        qualification_tier: adapter.qualification_tier,
        final_execution_enabled: adapter.final_execution_enabled === true,
    }));
}

function runtimeIdentityFallback() {
    const capabilities = state.runtimeCapabilities || {};
    const release = capabilities.release || {};
    const worker = capabilities.worker || {};
    return {
        build_sha: release.build_sha,
        protocol_version: release.protocol_version,
        boot_id: release.boot_id,
        runner_release: worker.release_id || worker.runner_release,
    };
}

function operationalStatusClass(value, ok = null) {
    const status = String(value || '').toLowerCase();
    if (ok === false || ['down', 'failed', 'unavailable', 'offline'].includes(status)) {
        return 'is-danger';
    }
    if (['degraded', 'warning', 'stale', 'blocked'].includes(status)) {
        return 'is-warning';
    }
    if (ok === true || ['ready', 'healthy', 'available', 'online'].includes(status)) {
        return 'is-ready';
    }
    return 'is-neutral';
}

function operationalList(items, {
    labelKeys,
    secondaryKeys = [],
    emptyText,
    countKeys = ['count', 'value'],
}) {
    const rows = boundedOperationalItems(items);
    if (!rows.length) {
        return `<div class="operations-empty">${esc(emptyText)}</div>`;
    }
    return `<div class="operations-list">${rows.map(item => {
        const labelValue = labelKeys
            .map(key => item[key])
            .find(value => value !== null && value !== undefined && value !== '');
        const secondary = secondaryKeys
            .map(key => item[key])
            .filter(value => value !== null && value !== undefined && value !== '')
            .map(value => operationalToken(value))
            .join(' · ');
        const countValue = countKeys
            .map(key => item[key])
            .find(value => value !== null && value !== undefined && value !== '');
        return `<div class="operations-list-row">
            <div>
                <div class="operations-list-label">${esc(operationalToken(labelValue))}</div>
                ${secondary ? `<div class="operations-list-detail">${esc(secondary)}</div>` : ''}
            </div>
            <span class="operations-count">${esc(operationalCount(countValue))}</span>
        </div>`;
    }).join('')}</div>`;
}

function renderOperationsDashboard() {
    const container = $('operations-dashboard');
    const generatedAt = $('operations-generated-at');
    if (!container || !generatedAt) return;

    if (!state.operationsData) {
        generatedAt.textContent = 'Protected snapshot unavailable';
        const message = state.operationsProbeStatus === 'authentication_required'
            ? 'Enter the API Secret to load protected operational evidence.'
            : state.operationsProbeStatus === 'loading'
                ? 'Loading protected operational evidence…'
                : 'Operational evidence is unavailable. Submission safety remains unchanged.';
        container.innerHTML = `<div class="operations-empty">${esc(message)}</div>`;
        return;
    }

    const data = state.operationsData;
    const dependencies = boundedOperationalItems(data.dependencies);
    const effectiveDependencies = dependencies.length
        ? dependencies
        : dependencyFallbackFromReadiness();
    const adapterMatrix = boundedOperationalItems(data.adapter_matrix);
    const effectiveAdapters = adapterMatrix.length
        ? adapterMatrix
        : adapterFallbackFromRuntime();
    const runtimeIdentity = data.runtime_identity && typeof data.runtime_identity === 'object'
        && !Array.isArray(data.runtime_identity)
        ? data.runtime_identity
        : runtimeIdentityFallback();
    const windowDays = operationalCount(data.window_days);
    const snapshotTime = fmtDateTime(data.generated_at);
    generatedAt.textContent = `${snapshotTime} · ${windowDays || 0}-day bounded window`;

    const lastDiscoveryValue = data.last_successful_discovery;
    const lastDiscoveryAt = lastDiscoveryValue && typeof lastDiscoveryValue === 'object'
        ? lastDiscoveryValue.finished_at || lastDiscoveryValue.occurred_at || null
        : lastDiscoveryValue;
    const readyDependencies = effectiveDependencies.filter(item => (
        item.ok === true
        || ['ready', 'healthy', 'available', 'online'].includes(String(item.status || '').toLowerCase())
    )).length;

    const dependencyHtml = effectiveDependencies.length
        ? `<div class="operations-dependency-grid">${effectiveDependencies.map(item => {
            const name = item.name || item.dependency || 'dependency';
            const status = item.status || (item.ok === true ? 'ready' : item.ok === false ? 'degraded' : 'unknown');
            const reason = item.reason_code
                ? `<span class="operations-dependency-reason">${esc(operationalToken(item.reason_code))}</span>`
                : '';
            const seenAt = item.last_seen_at || item.seen_at;
            return `<div class="operations-dependency">
                <span class="operations-status-dot ${operationalStatusClass(status, item.ok)}" aria-hidden="true"></span>
                <div>
                    <div class="operations-dependency-name">${esc(operationalToken(name))}</div>
                    <div class="operations-dependency-status">
                        ${esc(operationalToken(status))}${reason}
                        ${seenAt ? ` · seen ${esc(fmtDateTime(seenAt))}` : ''}
                    </div>
                </div>
            </div>`;
        }).join('')}</div>`
        : '<div class="operations-empty">No dependency observations are available.</div>';

    const adapterHtml = effectiveAdapters.length
        ? `<div class="operations-table-wrap"><table class="operations-table">
            <thead><tr>
                <th>ATS</th>
                <th>Adapter</th>
                <th>Selector</th>
                <th>Qualification</th>
                <th>Final action</th>
            </tr></thead>
            <tbody>${effectiveAdapters.map(adapter => {
                const finalAction = adapter.final_execution_enabled === true
                    ? 'enabled for qualified scope'
                    : 'inspection only';
                return `<tr>
                    <td>${esc(operationalToken(adapter.ats))}</td>
                    <td>${esc(shortOperationalToken(adapter.adapter_version))}</td>
                    <td>${esc(shortOperationalToken(adapter.selector_version))}</td>
                    <td><span class="operations-tier">${esc(operationalToken(adapter.qualification_tier))}</span></td>
                    <td>${esc(finalAction)}</td>
                </tr>`;
            }).join('')}</tbody>
        </table></div>`
        : '<div class="operations-empty">No versioned adapter inventory is available.</div>';

    container.innerHTML = `
        <div class="operations-summary-grid">
            <div class="operations-summary-card">
                <div class="operations-summary-label">Dependencies ready</div>
                <div class="operations-summary-value">${esc(readyDependencies)} / ${esc(effectiveDependencies.length)}</div>
                <div class="operations-summary-detail">A degraded dependency keeps Send disabled.</div>
            </div>
            <div class="operations-summary-card">
                <div class="operations-summary-label">Last successful discovery</div>
                <div class="operations-summary-value operations-summary-date">${esc(fmtDateTime(lastDiscoveryAt))}</div>
                <div class="operations-summary-detail">Derived from a completed successful discovery run.</div>
            </div>
            <div class="operations-summary-card">
                <div class="operations-summary-label">Runtime identity</div>
                <div class="operations-summary-value operations-summary-runtime">
                    build ${esc(shortOperationalToken(runtimeIdentity.build_sha, 12))}
                </div>
                <div class="operations-summary-detail">
                    protocol ${esc(shortOperationalToken(runtimeIdentity.protocol_version, 16))}
                    · boot ${esc(shortOperationalToken(runtimeIdentity.boot_id, 12))}
                    · runner ${esc(shortOperationalToken(runtimeIdentity.runner_release, 12))}
                </div>
            </div>
        </div>

        <div class="operations-card operations-card-wide">
            <div class="operations-card-heading">
                <h4>Dependency health</h4>
                <span>${esc(effectiveDependencies.length)} bounded checks</span>
            </div>
            ${dependencyHtml}
        </div>

        <div class="operations-card operations-card-wide">
            <div class="operations-card-heading">
                <h4>Versioned adapter matrix</h4>
                <span>Qualification is scoped to the exact version and selector set</span>
            </div>
            ${adapterHtml}
        </div>

        <div class="operations-card-grid">
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Queue depth</h4></div>
                ${operationalList(data.queue_depth, {
                    labelKeys: ['queue', 'name'],
                    emptyText: 'All bounded queues are empty.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Attempt stages</h4></div>
                ${operationalList(data.attempt_stages, {
                    labelKeys: ['stage'],
                    emptyText: 'No attempts in this window.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Attempt outcomes</h4></div>
                ${operationalList(data.attempt_outcomes, {
                    labelKeys: ['outcome'],
                    emptyText: 'No terminal outcomes in this window.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Failure clusters</h4></div>
                ${operationalList(data.failure_clusters, {
                    labelKeys: ['reason_code'],
                    secondaryKeys: ['ats', 'adapter_version', 'selector_version'],
                    emptyText: 'No bounded failure clusters in this window.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Form resolution</h4></div>
                ${operationalList(data.form_resolution, {
                    labelKeys: ['resolver', 'resolver_source'],
                    secondaryKeys: ['field_type'],
                    emptyText: 'No form-resolution observations in this window.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Attachment results</h4></div>
                ${operationalList(data.attachment_results, {
                    labelKeys: ['result', 'attachment_result'],
                    secondaryKeys: ['ats'],
                    emptyText: 'No attachment observations in this window.',
                })}
            </div>
            <div class="operations-card">
                <div class="operations-card-heading"><h4>Evidence types</h4></div>
                ${operationalList(data.evidence_types, {
                    labelKeys: ['evidence_type', 'type'],
                    secondaryKeys: ['ats'],
                    emptyText: 'No employer evidence recorded in this window.',
                })}
            </div>
        </div>`;
    lucide.createIcons();
}

function renderPipelineFunnel(d) {
    const container = $('pipeline-funnel');
    if (!container) return;

    const steps = [
        { label: 'URLs', value: d.total_urls ?? 0, icon: 'link', cls: '' },
        { label: 'Jobs', value: d.total_jobs ?? 0, icon: 'briefcase', cls: '' },
        { label: 'Applications', value: (d.applications_pending ?? 0) + (d.applications_approved ?? 0) + (d.applications_skipped ?? 0), icon: 'file-text', cls: '' },
        { label: 'Prepared', value: d.applications_approved ?? 0, icon: 'clipboard-check', cls: 'approved' },
        { label: 'Employer verified', value: d.submissions_success ?? 0, icon: 'badge-check', cls: 'submitted' },
    ];

    const max = Math.max(...steps.map(s => s.value), 1);

    container.innerHTML = steps.map(step => {
        const pct = Math.round((step.value / max) * 100);
        return `
        <div class="funnel-step">
            <div class="funnel-label">
                <i data-lucide="${step.icon}" style="width:13px;height:13px;"></i>
                ${step.label}
            </div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar ${step.cls}" style="width:${pct}%"></div>
            </div>
            <div class="funnel-value">${step.value}</div>
        </div>`;
    }).join('');

    lucide.createIcons();
}

function renderUrlHealthBanner(d) {
    const banner = $('url-health-banner');
    if (!banner) return;
    const issues = [];
    if (d.urls_failed > 0) issues.push(`${d.urls_failed} URL fetch failure${d.urls_failed !== 1 ? 's' : ''}`);
    if (d.urls_blocked > 0) issues.push(`${d.urls_blocked} blocked by bot protection`);
    if (issues.length) {
        banner.innerHTML = `<i data-lucide="alert-triangle" style="width:14px;height:14px;flex-shrink:0;"></i> ${issues.join(' &bull; ')} — check worker logs for details`;
        banner.style.display = 'flex';
        lucide.createIcons();
    } else {
        banner.style.display = 'none';
    }
}

function renderScoreHistogram(d) {
    const container = $('score-histogram');
    if (!container) return;
    const dist = d.score_distribution || {};
    const buckets = ['0-20', '20-40', '40-60', '60-80', '80-100'];
    const colors = ['#ef4444', '#f59e0b', '#3b82f6', '#10b981', '#06d6a0'];
    const labels = ['Very Low', 'Low', 'Medium', 'Good', 'Excellent'];
    const values = buckets.map(b => dist[b] || 0);
    const total = values.reduce((a, b) => a + b, 0);
    const max = Math.max(...values, 1);

    if (total === 0) {
        container.innerHTML = '<div class="hist-empty">No scored jobs yet</div>';
        return;
    }

    container.innerHTML = buckets.map((bucket, i) => {
        const pct = Math.round((values[i] / max) * 100);
        const share = total > 0 ? Math.round((values[i] / total) * 100) : 0;
        return `
        <div class="hist-row">
            <div class="hist-label" title="${labels[i]}">${bucket}</div>
            <div class="hist-bar-wrap">
                <div class="hist-bar" style="width:${pct}%;background:${colors[i]}"></div>
            </div>
            <div class="hist-count">${values[i]}<span class="hist-pct">${share}%</span></div>
        </div>`;
    }).join('');
}

// ── Candidate Profile / CV Upload ──────────────────────────────────────────
let selectedResumeFile = null;

const ONBOARDING_FIELDS = Object.freeze({
    legal_name: 'onboarding-legal-name',
    primary_email: 'onboarding-primary-email',
    phone: 'onboarding-phone',
    location: 'onboarding-location',
    work_authorization: 'onboarding-work-authorization',
    sponsorship: 'onboarding-sponsorship',
    citizenship: 'onboarding-citizenship',
    nationality: 'onboarding-nationality',
    gender: 'onboarding-gender',
    disability: 'onboarding-disability',
    ethnicity: 'onboarding-ethnicity',
    veteran_status: 'onboarding-veteran-status',
});

function setupOnboarding() {
    const form = $('onboarding-form');
    if (!form) return;
    form.addEventListener('input', () => {
        state.onboardingDirty = true;
    });
    form.addEventListener('submit', event => {
        event.preventDefault();
        saveOnboardingProfile();
    });
}

function renderProfileReadiness(profileReadiness = null) {
    const container = $('profile-readiness');
    if (!container) return;
    const automation = state.runtimeCapabilities?.automation;
    const stages = automation?.stages || profileReadiness || {};
    const modes = state.runtimeCapabilities?.mode || {};
    const rows = [
        {
            key: 'discovery',
            label: 'Discovery',
            enabled: modes.discovery_enabled !== false,
        },
        {
            key: 'preparation',
            label: 'Auto-prepare',
            enabled: modes.auto_prepare_enabled === true,
        },
        {
            key: 'submission',
            label: 'Qualified autopilot',
            enabled: modes.qualified_autopilot_enabled === true,
        },
    ];
    container.innerHTML = rows.map(row => {
        const stage = stages[row.key] || {};
        const status = !row.enabled ? 'off' : stage.ready === true ? 'ready' : 'blocked';
        const reasons = (stage.reason_codes || []).map(humanizeReasonCode).join(' · ');
        return `<span class="profile-stage ${status === 'ready' ? '' : status === 'off' ? 'is-disabled' : 'is-blocked'}"
                      title="${esc(reasons || `${row.label} is ${status}`)}">
                    ${esc(row.label)}: ${esc(status)}
                </span>`;
    }).join('');
}

function populateOnboardingForm(data) {
    if (!data || state.onboardingDirty) return;
    Object.entries(ONBOARDING_FIELDS).forEach(([field, id]) => {
        const input = $(id);
        if (input) input.value = data[field] || '';
    });
    state.onboardingProfile = data;
    renderProfileReadiness(data.readiness);
    const identityReasons = [
        ...(data.readiness?.preparation?.reason_codes || []),
        ...(data.readiness?.submission?.reason_codes || []),
    ];
    const needsOnboarding = identityReasons.some(reason => (
        reason.startsWith('PROFILE_NAME_')
        || reason.startsWith('PROFILE_EMAIL_')
        || reason.startsWith('PROFILE_PHONE_')
        || reason.includes('AUTHORIZATION_UNCONFIRMED')
        || reason.includes('SPONSORSHIP_UNCONFIRMED')
        || reason.includes('CITIZENSHIP_OR_NATIONALITY_UNCONFIRMED')
    ));
    if (needsOnboarding && $('onboarding-panel')) $('onboarding-panel').open = true;
}

async function fetchOnboardingProfile() {
    if (state.onboardingDirty) return;
    const data = await apiCall('/api/profile/onboarding');
    if (data) populateOnboardingForm(data);
}

async function saveOnboardingProfile() {
    const form = $('onboarding-form');
    const button = $('btn-save-onboarding');
    if (!form || !button || !form.reportValidity()) return;
    const payload = Object.fromEntries(
        Object.entries(ONBOARDING_FIELDS).map(([field, id]) => [
            field,
            ($(id)?.value || '').trim(),
        ])
    );
    if (!payload.citizenship && !payload.nationality) {
        showToast('Confirm citizenship or nationality before saving', 'warning');
        return;
    }

    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader"></i> Saving…';
    lucide.createIcons();
    const result = await apiCall('/api/profile/onboarding', 'PUT', payload);
    if (result) {
        state.onboardingDirty = false;
        populateOnboardingForm(result);
        showToast(`Confirmed profile saved as version ${result.profile_version}`, 'info');
        await fetchRuntimeStatus();
        await fetchProfileSummary();
    }
    button.disabled = false;
    button.innerHTML = original;
    lucide.createIcons();
}

function setupResumeUpload() {
    const dropzone = $('resume-dropzone');
    const input = $('resumeInput');
    const uploadBtn = $('btn-upload-resume');
    if (!dropzone || !input || !uploadBtn) return;

    dropzone.addEventListener('click', () => input.click());

    input.addEventListener('change', () => {
        if (input.files.length) setSelectedResumeFile(input.files[0]);
    });

    ['dragenter', 'dragover'].forEach(evt => {
        dropzone.addEventListener(evt, e => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });
    });
    ['dragleave', 'drop'].forEach(evt => {
        dropzone.addEventListener(evt, e => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
        });
    });
    dropzone.addEventListener('drop', e => {
        const file = e.dataTransfer?.files?.[0];
        if (file) setSelectedResumeFile(file);
    });

    uploadBtn.addEventListener('click', uploadResume);
}

function setSelectedResumeFile(file) {
    const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
    if (!isPdf) {
        showToast('Please select a PDF file', 'warning');
        return;
    }
    selectedResumeFile = file;
    $('resume-filename').textContent = `Selected: ${file.name}`;
    $('btn-upload-resume').disabled = false;
}

async function uploadResume() {
    if (!selectedResumeFile) return;
    const btn = $('btn-upload-resume');
    const originalLabel = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Uploading…';
    lucide.createIcons();

    const formData = new FormData();
    formData.append('file', selectedResumeFile);
    const headers = {};
    if (state.authToken) headers['Authorization'] = `Bearer ${state.authToken}`;

    try {
        const res = await fetch('/api/profile/resume', { method: 'POST', headers, body: formData });
        if (res.status === 401 || res.status === 403) {
            showToast('Authentication failed. Check API Secret.', 'error');
            return;
        }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(boundedApiError(err, `HTTP ${res.status}`));
        }
        const data = await res.json();
        renderProfileSummary(data);
        showToast(
            `CV processed — profile v${data.version} rebuilt, ${data.rescored} job${data.rescored !== 1 ? 's' : ''} rescored`,
            'info'
        );
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalLabel;
        lucide.createIcons();
    }
}

function renderProfileSummary(data) {
    const summary = $('profile-summary');
    if (!summary) return;
    $('profile-summary-name').innerHTML =
        `<i data-lucide="user" style="width:14px;height:14px;"></i> ${esc(data.name || 'Unknown')}`;
    const roles = data.roles || [];
    $('profile-summary-roles').innerHTML = roles.length
        ? roles.map(r => `<span class="profile-role-chip">${esc(r)}</span>`).join('')
        : '<span class="text-muted text-sm">No roles detected</span>';
    summary.style.display = 'grid';
    lucide.createIcons();
}

async function fetchProfileSummary() {
    const data = await apiCall('/api/profile');
    if (!data) return;
    renderProfileReadiness(data.readiness);
    const el = $('profile-current-summary');
    if (el) {
        el.textContent = data.name
            ? `Current: ${data.name}${data.roles?.length ? ' · ' + data.roles.slice(0, 2).join(', ') : ''}`
            : 'No profile on file yet';
    }
    if (data.name) renderProfileSummary(data);
}

function toggleAutoRefresh() {
    state.autoRefresh = !state.autoRefresh;
    const btn = $('btn-auto-refresh');
    if (state.autoRefresh) {
        btn.classList.add('active');
        btn.title = 'Auto-refresh ON — click to disable';
        state.autoRefreshTimer = setInterval(() => {
            refreshAllData();
        }, 30000);
        showToast('Auto-refresh enabled (every 30s)', 'info');
    } else {
        btn.classList.remove('active');
        btn.title = 'Toggle auto-refresh (30s)';
        clearInterval(state.autoRefreshTimer);
        state.autoRefreshTimer = null;
        showToast('Auto-refresh disabled', 'info');
    }
}

function applyJobFilter(status) {
    const btns = document.querySelectorAll('#view-jobs .filter-btn');
    btns.forEach(b => {
        b.classList.toggle('active', b.dataset.status === status);
    });
    state.filters.jobs = status;
    fetchJobs();
}

function exportJobsCSV() {
    if (!state.jobs.length) { showToast('No jobs to export', 'info'); return; }
    const headers = ['Title', 'Company', 'Location', 'Employment Type', 'Score', 'Status', 'Employer Verified', 'Date'];
    const rows = state.jobs.map(j => [
        j.title || '',
        j.company || '',
        j.location || '',
        j.employment_type || '',
        j.score ?? '',
        j.display_status || (j.status === 'submitted' ? 'unverified' : j.status) || '',
        j.employer_verified === true ? 'yes' : 'no',
        j.created_at ? new Date(j.created_at).toLocaleDateString() : '',
    ]);
    const csv = [headers, ...rows]
        .map(row => row.map(v => `"${String(v).replace(/"/g, '""')}"`).join(','))
        .join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `jobs-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast(`Exported ${state.jobs.length} jobs`, 'info');
}

function renderActivityFeed() {
    const container = $('activity-feed');
    const apps = [...state.applications].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 12);
    const jobs = [...state.jobs].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 12);

    // Merge and sort
    const events = [
        ...apps.map(a => ({
            ts: new Date(a.created_at),
            type: a.status === 'submitted' && a.submission_verified !== true
                ? 'unverified'
                : isPreparedApplication(a)
                    ? 'approved'
                    : a.status,
            title: a.job_title,
            meta: a.job_company,
            score: a.job_score,
        })),
        ...jobs.filter(j => !apps.find(a => a.job_title === j.title && a.job_company === j.company)).map(j => ({
            ts: new Date(j.created_at),
            type: 'ingested',
            title: j.title,
            meta: j.company,
            score: j.score,
        })),
    ].sort((a, b) => b.ts - a.ts).slice(0, 15);

    if (!events.length) {
        container.innerHTML = '<div class="empty-feed"><i data-lucide="inbox"></i><p>No recent activity. Add a job URL to get started.</p></div>';
        lucide.createIcons();
        return;
    }

    const iconMap = {
        ingested: { icon: 'globe', cls: 'ingested', label: 'Discovered' },
        scored: { icon: 'target', cls: 'scored', label: 'Scored' },
        draft: { icon: 'file-edit', cls: 'drafted', label: 'Draft ready' },
        approved: { icon: 'clipboard-check', cls: 'approved', label: 'Prepared' },
        skipped: { icon: 'skip-forward', cls: 'skipped', label: 'Skipped' },
        submitted: { icon: 'badge-check', cls: 'submitted', label: 'Employer verified' },
        unverified: { icon: 'circle-help', cls: 'unverified', label: 'Unverified result' },
    };

    container.innerHTML = events.map(ev => {
        const cfg = iconMap[ev.type] || iconMap.ingested;
        return `
        <div class="activity-item">
            <div class="activity-icon ${cfg.cls}">
                <i data-lucide="${cfg.icon}" style="width:15px;height:15px;"></i>
            </div>
            <div class="activity-body">
                <div class="activity-title">${esc(ev.title || 'Untitled')}</div>
                <div class="activity-meta">
                    ${cfg.label}${ev.meta ? ` &bull; ${esc(ev.meta)}` : ''}
                    ${ev.score != null ? ` &bull; Score: ${ev.score}` : ''}
                </div>
            </div>
            <div class="activity-time">${timeAgo(ev.ts)}</div>
        </div>`;
    }).join('');
    lucide.createIcons();
}

// ── Rendering: Applications ────────────────────────────────────────────────────
function latestAttempt(application) {
    const attempts = application?.attempts || [];
    return attempts.length ? attempts[attempts.length - 1] : application?.attempt || null;
}

function isEmployerVerified(value) {
    if (!value) return false;
    if (value.submission_verified === true || value.verified === true) return true;
    return value.attempt?.verified === true || latestAttempt(value)?.verified === true;
}

function attemptOutcome(value) {
    const attempt = value?.attempt || latestAttempt(value);
    return String(
        attempt?.outcome
        || attempt?.status
        || value?.outcome
        || value?.state
        || value?.submission_status
        || value?.status
        || ''
    ).toLowerCase();
}

function applicationStatusLabel(application) {
    if (isPreparedApplication(application)) return 'prepared';
    if (application.status === 'approved') return 'prepared';
    if (application.status === 'submitted') {
        return isEmployerVerified(application) ? 'employer verified' : 'unverified';
    }
    return String(application.status || 'unknown').replace(/_/g, ' ');
}

function isPreparedApplication(application) {
    return ['approved', 'prepared', 'ready'].includes(application?.status)
        || (application?.status === 'draft' && Boolean(application?.approved_at));
}

function isReviewableApplication(application) {
    return application?.status === 'draft' && !isPreparedApplication(application);
}

function matchesApplicationFilter(application, filter) {
    if (filter === 'draft') return isReviewableApplication(application);
    if (filter === 'approved') return isPreparedApplication(application);
    return application.status === filter;
}

function parseServerTimestamp(rawValue) {
    if (typeof rawValue !== 'string' || rawValue.trim() === '') return Number.NaN;
    const raw = rawValue.trim();
    const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(raw) ? raw : `${raw}Z`;
    return Date.parse(normalized);
}

function hasValidFormPlan(application) {
    const plan = application?.form_plan || application?.latest_form_plan || {};
    if (plan.invalidated_at || application?.form_plan_invalidated_at) return false;
    const planId = plan.plan_id || application?.form_plan_id;
    const fingerprint = plan.fingerprint || application?.form_plan_fingerprint;
    if (!planId || !/^[0-9a-f]{64}$/.test(String(fingerprint || ''))) return false;
    if (
        plan.plan_id
        && application?.form_plan_id
        && plan.plan_id !== application.form_plan_id
    ) {
        return false;
    }
    if (
        plan.fingerprint
        && application?.form_plan_fingerprint
        && plan.fingerprint !== application.form_plan_fingerprint
    ) {
        return false;
    }
    const explicitlyValid = application?.form_plan_valid === true
        || application?.form_plan_status === 'valid'
        || plan.valid === true
        || plan.is_valid === true
        || plan.status === 'valid';
    if (!explicitlyValid) return false;

    const expiresAt = plan.expires_at || application?.form_plan_expires_at;
    const expiresAtMs = parseServerTimestamp(expiresAt);
    return Number.isFinite(expiresAtMs) && expiresAtMs > Date.now();
}

const ACTIVE_SUBMISSION_STAGES = new Set([
    'queued',
    'inspecting',
    'preparing',
    'ready',
    'committing',
    'verifying',
]);

function hasActiveSubmissionAttempt(application) {
    const stage = String(latestAttempt(application)?.stage || '').toLowerCase();
    return ACTIVE_SUBMISSION_STAGES.has(stage);
}

function adapterCapabilityForPlatform(platform) {
    if (!Array.isArray(state.adapterCapabilities)) return null;
    const normalized = String(platform || '').trim().toLowerCase();
    return state.adapterCapabilities.find(adapter => adapter.ats === normalized) || null;
}

function requiresVersionedFormPlan(application) {
    const capability = adapterCapabilityForPlatform(application?.platform);
    return (
        application?.requires_versioned_form_plan === true
        || capability?.execution_contract_version === 'two-phase-v2'
    );
}

function adapterInspectionBlockers(application) {
    const capability = adapterCapabilityForPlatform(application?.platform);
    if (!capability) {
        return [
            state.adapterProbeStatus === 'authentication_required'
                ? 'Enter the API Secret to verify ATS inspection qualification'
                : 'ATS inspection qualification is unavailable'
        ];
    }
    if (capability.execution_contract_version !== 'two-phase-v2') {
        return ['This ATS does not have a versioned two-phase browser adapter'];
    }
    if (!['dry_run_qualified', 'live_canary_qualified'].includes(
        capability.qualification_tier
    )) {
        return [
            `${capability.ats} ${capability.adapter_version} is `
            + `${capability.qualification_tier.replace(/_/g, ' ')} only; `
            + 'real-URL inspection is disabled'
        ];
    }
    if (
        !Array.isArray(capability.qualified_form_scope)
        || capability.qualified_form_scope.length === 0
    ) {
        return ['No qualified real-form scope is recorded for this adapter version'];
    }
    return [];
}

function adapterQualificationBlockers(application) {
    const blockers = [];
    if (!Array.isArray(state.adapterCapabilities)) {
        blockers.push(
            state.adapterProbeStatus === 'authentication_required'
                ? 'Enter the API Secret to verify ATS qualification'
                : 'ATS qualification inventory is unavailable'
        );
        return blockers;
    }

    const adapterName = application?.form_plan_adapter_name;
    const adapterVersion = application?.form_plan_adapter_version;
    const selectorVersion = application?.form_plan_selector_version;
    const fingerprint = application?.form_plan_fingerprint;
    if (!adapterName || !adapterVersion || !selectorVersion) {
        return ['The current form plan has no versioned adapter identity'];
    }
    const capability = state.adapterCapabilities.find(adapter => (
        adapter.ats === adapterName
        && adapter.adapter_version === adapterVersion
        && adapter.selector_version === selectorVersion
    ));
    if (!capability) {
        return ['The exact form adapter version is not registered'];
    }
    if (capability.final_execution_enabled !== true) {
        blockers.push(
            `${adapterName} ${adapterVersion} is not live-canary qualified for final execution`
        );
    }
    const qualifiedScope = Array.isArray(capability.qualified_form_scope)
        ? capability.qualified_form_scope
        : [];
    if (
        !/^[0-9a-f]{64}$/.test(String(fingerprint || ''))
        || !qualifiedScope.includes(fingerprint)
    ) {
        blockers.push('The current form fingerprint is outside the qualified live scope');
    }
    return blockers;
}

function liveSendBlockers(application, runtime = runtimeSubmissionState()) {
    const blockers = [...runtime.reasons];
    const runtimeModel = state.runtimeCapabilities?.llm;
    if (hasActiveSubmissionAttempt(application)) {
        blockers.push('A submission attempt is already in progress');
    }
    if (application?.material_eligible !== true) {
        blockers.push('Evidence-validated application materials are required');
    } else if (
        !runtimeModel
        || runtimeModel.ready !== true
        || runtimeModel.local !== true
        || application.material_prompt_version !== QUALIFIED_MATERIAL_PROMPT_VERSION
        || application.material_model_provider !== runtimeModel.provider
        || application.material_model_name !== runtimeModel.model
        || application.material_model_digest !== runtimeModel.digest
    ) {
        blockers.push('The local model changed; regenerate and review the materials');
    }
    if (
        application?.form_plan_uses_local_llm === true
        && (
            !runtimeModel
            || application.form_plan_llm_prompt_version !== QUALIFIED_FORM_PROMPT_VERSION
            || application.form_plan_llm_model_provider !== runtimeModel.provider
            || application.form_plan_llm_model_name !== runtimeModel.model
            || application.form_plan_llm_model_digest !== runtimeModel.digest
        )
    ) {
        blockers.push('The local model changed; inspect the employer form again');
    }
    if (!hasValidFormPlan(application)) blockers.push('A current validated form plan is required');
    blockers.push(...adapterQualificationBlockers(application));
    if (application?.portal_session_ready === false) blockers.push('Sign in to the employer portal');
    return [...new Set(blockers)];
}

function finalSendUiState(application) {
    const runtime = runtimeSubmissionState();
    const blockers = liveSendBlockers(application, runtime);
    const candidate = (
        isPreparedApplication(application)
        && !isEmployerVerified(application)
        && !hasActiveSubmissionAttempt(application)
    );
    const visible = (
        candidate
        && runtime.allowed
        && hasValidFormPlan(application)
        && blockers.length === 0
    );
    return { candidate, visible, blockers };
}

function summarizeSendBlockers(blockers, limit = 2) {
    const visible = blockers.slice(0, limit);
    const remaining = Math.max(0, blockers.length - visible.length);
    return `${visible.join(' · ')}${remaining ? ` · ${remaining} more` : ''}`;
}

function renderApplications() {
    const filtered = state.applications.filter(
        application => matchesApplicationFilter(application, state.filters.applications)
    );
    const container = $('applications-list');
    updateBatchApproveButton();

    if (!filtered.length) {
        const filterLabel = {
            draft: 'reviewable',
            approved: 'prepared',
            submitted: 'submission-record',
        }[state.filters.applications] || state.filters.applications;
        container.innerHTML = `
            <div class="empty-state">
                <i data-lucide="inbox"></i>
                <h3>No ${filterLabel} applications</h3>
                <p>Applications in the '${filterLabel}' view will appear here.</p>
            </div>`;
        lucide.createIcons();
        return;
    }

    container.innerHTML = filtered.map(app => {
        const isPending = isReviewableApplication(app);
        const isPrepared = isPreparedApplication(app);
        const outcome = attemptOutcome(app);
        const verified = isEmployerVerified(app);
        const canRetry = ['failed', 'failed_before_commit', 'draft_only'].includes(outcome);
        const sendUi = finalSendUiState(app);
        const sendGuidance = sendUi.candidate && !sendUi.visible
            ? `<div class="app-action-guidance">
                <i data-lucide="circle-alert" aria-hidden="true"></i>
                <span><strong>Send unavailable.</strong>
                    ${esc(summarizeSendBlockers(sendUi.blockers))}
                    Open application details to review every blocker.
                </span>
            </div>`
            : '';

        // Submission badge
        let subBadge = '';
        if (app.submission_status) {
            let cls = 'sub-badge-pending';
            let label = String(outcome || app.submission_status).replace(/_/g, ' ');
            if (verified) {
                cls = 'sub-badge-success';
                label = '✅ Employer confirmed';
            } else if (['success', 'confirmed_submitted', 'legacy_unverified', 'operator_confirmed'].includes(outcome)) {
                cls = 'sub-badge-unverified';
                label = '⚠️ Unverified — not counted';
            } else if (outcome === 'draft_only') {
                cls = 'sub-badge-draft';
                label = '⚠️ Draft only — not submitted';
            } else if (['failed', 'failed_before_commit'].includes(outcome)) {
                cls = 'sub-badge-failed';
                label = '❌ Failed before confirmation';
            } else if (outcome === 'unknown') {
                cls = 'sub-badge-unverified';
                label = '⚠️ Unknown — review required';
            } else if (outcome === 'already_applied') {
                cls = 'sub-badge-pending';
                label = 'Already applied — not a new submission';
            }
            const platform = app.submission_platform ? ` · ${app.submission_platform.replace(/_/g, ' ')}` : '';
            subBadge = `<div class="sub-badge ${cls}">${label}${esc(platform)}</div>`;
        }

        const statusClass = app.status === 'submitted' && !verified
            ? 'unverified'
            : isPrepared
                ? 'approved'
                : app.status;

        return `
        <div class="app-card">
            <div>
                <div class="app-meta mb-1">
                    <span class="status ${statusClass}">${esc(applicationStatusLabel(app))}</span>
                    <span class="text-sm">${fmtDate(app.created_at)}</span>
                </div>
                ${isPending ? `<label class="batch-select">
                    <input type="checkbox"
                           ${state.selectedApplications.has(app.id) ? 'checked' : ''}
                           onchange="toggleApplicationSelection(${app.id}, this.checked)">
                    Select for batch preparation
                </label>` : ''}
                <h3 class="app-title" title="${esc(app.job_title)}">${esc(app.job_title)}</h3>
                <div class="text-dim mb-1" style="font-size:0.85rem;">${esc(app.job_company)}</div>
                <div class="text-dim mb-1" style="font-size:0.78rem;">
                    ${esc((app.platform || 'unknown').replace(/_/g, ' '))}
                    ${app.portal_session_ready === true ? ' · Session ready' : ''}
                    ${app.portal_session_ready === false ? ' · Sign-in needed' : ''}
                </div>
                <div class="app-score mb-1">
                    <i data-lucide="target" style="width:14px;height:14px;"></i>
                    ${app.job_score}/100
                    <div class="score-bar-track" style="display:inline-flex;width:60px;">
                        <div class="score-bar-fill" style="width:${app.job_score}%"></div>
                    </div>
                </div>
                ${subBadge}
                <div class="app-excerpt">${esc(app.cover_letter || '—')}</div>
                ${sendGuidance}
            </div>
            <div style="border-top:1px solid var(--border-light);padding-top:14px;margin-top:auto;display:flex;flex-direction:column;gap:8px;">
                ${isPending
                ? `<button class="btn btn-primary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="eye" style="width:14px;height:14px;"></i> Review &amp; Prepare
                       </button>`
                : `<button class="btn btn-secondary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="file-search" style="width:14px;height:14px;"></i> View application
                       </button>`
                }
                ${canRetry
                ? `<button class="btn btn-retry full-width" onclick="handleRetry(${app.id})">
                         <i data-lucide="refresh-cw" style="width:14px;height:14px;"></i> Retry attempt
                       </button>`
                : ''
                }
            </div>
        </div>`;
    }).join('');

    lucide.createIcons();
}

window.toggleApplicationSelection = (applicationId, selected) => {
    if (selected) state.selectedApplications.add(applicationId);
    else state.selectedApplications.delete(applicationId);
    updateBatchApproveButton();
};

function updateBatchApproveButton() {
    const btn = $('btn-batch-approve');
    if (!btn) return;
    const count = state.selectedApplications.size;
    btn.style.display = state.filters.applications === 'draft' && count > 0
        ? 'inline-flex'
        : 'none';
    btn.innerHTML = `<i data-lucide="check-check" style="width:16px"></i> Prepare selected (${count})`;
    if (count > 0) lucide.createIcons();
}

async function handleBatchApprove() {
    const ids = [...state.selectedApplications];
    if (!ids.length) return;
    if (!window.confirm(
        `Prepare exactly ${ids.length} reviewed application${ids.length === 1 ? '' : 's'}? This does not confirm employer submission.`
    )) return;
    const result = await apiCall('/api/applications/batch-prepare', 'POST', {
        application_ids: ids,
        acknowledgement: 'PREPARE_SELECTED_APPLICATIONS',
    });
    if (!result) return;
    state.selectedApplications.clear();
    const preparedIds = result.prepared_application_ids || result.queued_application_ids || [];
    showToast(`${preparedIds.length} application(s) accepted for preparation — not submitted`, 'info');
    await refreshAllData();
}

// ── Rendering: Jobs Table ──────────────────────────────────────────────────────
function renderJobs() {
    const tbody = $('jobs-table-body');
    let jobs = state.jobs;

    if (state.jobSearch) {
        jobs = jobs.filter(j =>
            (j.title || '').toLowerCase().includes(state.jobSearch) ||
            (j.company || '').toLowerCase().includes(state.jobSearch)
        );
    }

    if (!jobs.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-muted);">
            ${state.jobSearch ? `No jobs matching "${esc(state.jobSearch)}"` : 'No jobs found.'}
        </td></tr>`;
        return;
    }

    tbody.innerHTML = jobs.map(job => {
        const jobUrl = job.apply_url || job.source_url || '#';
        const score = job.score ?? null;
        const barWidth = score !== null ? Math.min(score, 100) : 0;
        return `
        <tr>
            <td>
                <a href="${esc(jobUrl)}" target="_blank" class="job-link">
                    ${esc(job.title || '—')}
                    <i data-lucide="external-link" style="width:11px;height:11px;opacity:0.5;"></i>
                </a>
            </td>
            <td style="color:var(--text-dim)">${esc(job.company || '—')}</td>
            <td>
                ${score !== null
                ? `<div class="score-bar">
                        <span>${score}</span>
                        <div class="score-bar-track"><div class="score-bar-fill" style="width:${barWidth}%"></div></div>
                       </div>`
                : '<span class="text-muted">—</span>'}
            </td>
            <td><span class="status ${job.employer_verified === true ? 'submitted' : (job.display_status || 'unverified')}">${esc((job.display_status || (job.status === 'submitted' ? 'unverified' : job.status) || 'pending').replace(/_/g, ' '))}</span></td>
            <td style="color:var(--text-muted);white-space:nowrap;">${fmtDate(job.created_at)}</td>
        </tr>`;
    }).join('');

    lucide.createIcons();
}

// ── Rendering: WhatsApp Messages ───────────────────────────────────────────────
function renderMessages() {
    const container = $('wa-messages-list');

    if (!state.messages.length) {
        container.innerHTML = `
            <div class="empty-state">
                <i data-lucide="message-circle"></i>
                <h3>No messages yet</h3>
                <p>Forward a job link via WhatsApp to get started</p>
            </div>`;
        lucide.createIcons();
        return;
    }

    container.innerHTML = state.messages.map(msg => {
        const body = msg.body || '';
        const urls = extractUrlsFromText(body);
        const initials = (msg.sender_phone || '?').slice(-4);
        const urlChips = urls.map(u => {
            const isJob = isJobUrl(u);
            const label = u.length > 50 ? u.slice(0, 48) + '…' : u;
            return `<a href="${esc(u)}" target="_blank" class="wa-msg-url ${isJob ? 'job-url' : ''}">
                <i data-lucide="${isJob ? 'briefcase' : 'link'}" style="width:10px;height:10px;"></i>
                ${esc(label)}
            </a>`;
        }).join('');

        return `
        <div class="wa-msg-item">
            <div class="wa-msg-avatar">${initials}</div>
            <div class="wa-msg-body">
                <div class="wa-msg-sender">${esc(msg.sender_phone || 'Unknown')}</div>
                <div class="wa-msg-text">${esc(body.slice(0, 200))}${body.length > 200 ? '…' : ''}</div>
                ${urlChips ? `<div class="wa-msg-urls">${urlChips}</div>` : ''}
            </div>
            <div class="wa-msg-time">${timeAgo(new Date(msg.created_at))}</div>
        </div>`;
    }).join('');

    lucide.createIcons();
}

// ── Ingest Modal ──────────────────────────────────────────────────────────────
function openIngestModal() {
    $('ingest-url').value = '';
    $('url-hint').textContent = '';
    $('url-hint').className = 'url-hint';
    $('ingest-modal').classList.add('visible');
    setTimeout(() => $('ingest-url').focus(), 50);
}

function validateIngestInput() {
    const raw = $('ingest-url').value.trim();
    const hint = $('url-hint');
    if (!raw) { hint.textContent = ''; hint.className = 'url-hint'; return; }

    const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
    const urls = lines.filter(l => l.startsWith('http'));
    const invalid = lines.filter(l => !l.startsWith('http'));

    if (invalid.length > 0) {
        hint.className = 'url-hint invalid';
        hint.innerHTML = `<i data-lucide="alert-circle" style="width:12px;height:12px;"></i> ${invalid.length} line(s) don't look like URLs`;
        lucide.createIcons();
        return;
    }

    const jobCount = urls.filter(isJobUrl).length;
    const shortCount = urls.filter(isShortUrl).length;

    let msg = `${urls.length} URL${urls.length !== 1 ? 's' : ''} detected`;
    let cls = 'url-hint valid';
    let extra = '';

    if (jobCount > 0) {
        cls = 'url-hint job';
        extra += ` <span class="url-type-badge job"><i data-lucide="briefcase" style="width:9px;height:9px;"></i> ${jobCount} job board</span>`;
    }
    if (shortCount > 0) {
        extra += ` <span class="url-type-badge short"><i data-lucide="link" style="width:9px;height:9px;"></i> ${shortCount} short link</span>`;
    }

    hint.className = cls;
    hint.innerHTML = `<i data-lucide="check-circle-2" style="width:12px;height:12px;"></i> ${msg}${extra}`;
    lucide.createIcons();
}

async function submitIngest() {
    const raw = $('ingest-url').value.trim();
    if (!raw) return;

    const btn = $('btn-submit-url');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Processing…';
    lucide.createIcons();

    try {
        const urls = raw.split('\n').map(line => line.trim()).filter(Boolean);
        const res = await apiCall('/api/dashboard/ingest', 'POST', {
            urls,
            sender: 'dashboard',
        });
        if (res) {
            const results = res.results || [];
            const counts = results.reduce((summary, item) => {
                const state = item.state || 'failed';
                summary[state] = (summary[state] || 0) + 1;
                return summary;
            }, {});
            showToast([
                `${counts.accepted || 0} accepted`,
                `${counts.duplicate || 0} duplicate`,
                `${counts.rejected || 0} rejected`,
                `${counts.failed || 0} failed`,
            ].join(' · '), counts.failed || counts.rejected ? 'warning' : 'info');
            $('ingest-url').value = '';
            $('ingest-modal').classList.remove('visible');
            setTimeout(refreshAllData, 2500);
        }
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="list-plus"></i> Queue URL(s)';
        lucide.createIcons();
    }
}

// ── Review Modal ──────────────────────────────────────────────────────────────
function formPlanIsReviewable(plan) {
    if (!plan || plan.invalidated_at) return false;
    const expiresAt = parseServerTimestamp(plan.expires_at);
    return Number.isFinite(expiresAt) && expiresAt > Date.now();
}

function formPlanHasAttachmentEvidence(plan) {
    if (!plan || plan.attachment_verified !== true) return false;
    const verifiedAt = parseServerTimestamp(plan.attachment_verified_at);
    return (
        plan.attachment_verification_source === 'candidate_browser_upload_complete'
        && Number.isFinite(verifiedAt)
        && Boolean(plan.selected_cv_ref)
        && plan.selected_cv_ref === plan.attached_cv_ref
        && /^[0-9a-f]{64}$/.test(String(plan.selected_cv_hash || ''))
        && plan.selected_cv_hash === plan.attached_cv_hash
    );
}

function cacheFormPlanIdentity(application, plan) {
    if (!application || !plan) return;
    application.form_plan_id = plan.plan_id || null;
    application.form_plan_fingerprint = plan.fingerprint || null;
    application.form_plan_adapter_name = plan.adapter_name || null;
    application.form_plan_adapter_version = plan.adapter_version || null;
    application.form_plan_selector_version = plan.selector_version || null;
}

function redactedDigest(value) {
    return /^[0-9a-f]{64}$/.test(String(value || ''))
        ? `${String(value).slice(0, 12)}…`
        : 'unavailable';
}

function formConstraintSummary(field) {
    const constraints = field.constraints || {};
    const parts = [];
    if (constraints.min_length !== null && constraints.min_length !== undefined) {
        parts.push(`minimum ${constraints.min_length} characters`);
    }
    if (constraints.max_length !== null && constraints.max_length !== undefined) {
        parts.push(`maximum ${constraints.max_length} characters`);
    }
    if (constraints.min_value !== null && constraints.min_value !== undefined) {
        parts.push(`minimum ${constraints.min_value}`);
    }
    if (constraints.max_value !== null && constraints.max_value !== undefined) {
        parts.push(`maximum ${constraints.max_value}`);
    }
    if (constraints.pattern) parts.push('employer format rule');
    if (constraints.multiple) parts.push('multiple selections allowed');
    return parts.join(' · ');
}

function formAnswerControl(field, decision, index, reviewable, partialPlan) {
    const controlId = `form-answer-${index}`;
    const reusableId = `form-reusable-${index}`;
    const value = decision?.disposition === 'resolved' ? decision.value : null;
    const disabled = reviewable ? '' : ' disabled';
    let control = '';

    if (['select', 'radio', 'multi_select'].includes(field.field_type)) {
        const selected = Array.isArray(value) ? value.map(String) : [String(value ?? '')];
        const multiple = field.field_type === 'multi_select' ? ' multiple' : '';
        const prompt = field.field_type === 'multi_select'
            ? ''
            : '<option value="">Choose an answer</option>';
        const options = (field.options || []).map(option => {
            const isSelected = selected.includes(String(option.value)) ? ' selected' : '';
            const isDisabled = option.disabled ? ' disabled' : '';
            return `<option value="${esc(option.value)}"${isSelected}${isDisabled}>${esc(option.label)}</option>`;
        }).join('');
        control = `<select id="${controlId}" class="form-input"${multiple}${disabled}>${prompt}${options}</select>`;
    } else if (['checkbox', 'consent', 'attestation'].includes(field.field_type)) {
        const yes = value === true ? ' selected' : '';
        const no = value === false ? ' selected' : '';
        control = `<select id="${controlId}" class="form-input"${disabled}>
            <option value="">Choose an answer</option>
            <option value="true"${yes}>Yes</option>
            <option value="false"${no}>No</option>
        </select>`;
    } else if (field.field_type === 'textarea') {
        const maxLength = field.constraints?.max_length;
        const maxAttr = Number.isInteger(maxLength) ? ` maxlength="${maxLength}"` : '';
        control = `<textarea id="${controlId}" class="form-input" rows="3"${maxAttr}${disabled}>${esc(value ?? '')}</textarea>`;
    } else if (['text', 'date', 'number', 'email', 'phone', 'url'].includes(field.field_type)) {
        const htmlType = {
            text: 'text',
            date: 'date',
            number: 'number',
            email: 'email',
            phone: 'tel',
            url: 'url',
        }[field.field_type];
        const constraints = field.constraints || {};
        const min = field.field_type === 'number' && Number.isFinite(constraints.min_value)
            ? ` min="${constraints.min_value}"`
            : '';
        const max = field.field_type === 'number' && Number.isFinite(constraints.max_value)
            ? ` max="${constraints.max_value}"`
            : '';
        const minLength = Number.isInteger(constraints.min_length)
            ? ` minlength="${constraints.min_length}"`
            : '';
        const maxLength = Number.isInteger(constraints.max_length)
            ? ` maxlength="${constraints.max_length}"`
            : '';
        control = `<input id="${controlId}" class="form-input" type="${htmlType}"
            value="${esc(value ?? '')}"${min}${max}${minLength}${maxLength}${disabled}>`;
    } else {
        const message = field.field_type === 'file'
            ? 'Attachments are verified separately and cannot be confirmed here.'
            : 'This control is unsupported. Reinspect the application before continuing.';
        return `<div class="text-sm text-dim">${esc(message)}</div>`;
    }

    const reusable = field.canonical_name
        ? `<label class="text-sm text-dim" style="display:flex;gap:7px;align-items:center;">
            <input id="${reusableId}" type="checkbox"${partialPlan ? ' checked required aria-required="true"' : ''}${disabled}>
            ${partialPlan
                ? 'Required for this partial plan: save as a scoped reusable answer'
                : 'Reuse only for this exact field and form version'}
        </label>`
        : partialPlan
            ? '<div class="text-sm text-warning">This partial field has no reusable scope. Reinspect after its field mapping is corrected.</div>'
            : '';
    const actionDisabled = !reviewable || (partialPlan && !field.canonical_name)
        ? ' disabled'
        : '';
    return `${control}
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px;">
            <button type="button" class="btn btn-sm btn-secondary"
                    data-confirm-field-index="${index}"${actionDisabled}>
                ${decision?.disposition === 'resolved' ? 'Update answer' : 'Confirm answer'}
            </button>
            ${reusable}
        </div>`;
}

function renderFormPlanPanel(appId, plan) {
    const panel = $('modal-form-plan');
    const fields = Array.isArray(plan.fields) ? plan.fields : [];
    const disclosures = Array.isArray(plan.disclosures) ? plan.disclosures : [];
    const decisions = Array.isArray(plan.decisions) ? plan.decisions : [];
    const decisionByField = new Map(decisions.map(item => [item.field_id, item]));
    const resolved = fields.filter(
        field => decisionByField.get(field.field_id)?.disposition === 'resolved'
    ).length;
    const unresolvedRequired = fields.filter(
        field => field.required
            && decisionByField.get(field.field_id)?.disposition !== 'resolved'
    ).length;
    const provenance = [...new Set(
        decisions.map(item => item.provenance).filter(Boolean)
    )];
    const blockers = Array.isArray(plan.blockers) ? plan.blockers : [];
    const partialPlan = blockers.includes('FORM_PLAN_INCOMPLETE');
    const reviewable = formPlanIsReviewable(plan);
    const attachmentEvidence = formPlanHasAttachmentEvidence(plan);
    const planLabel = partialPlan
        ? 'Partial observation — scoped answers and reinspection required'
        : plan.valid
        ? 'Current, prepared plan'
        : reviewable
            ? 'Current plan — prepare again after changes'
            : 'Expired or changed plan';
    const fieldRows = fields.map((field, index) => {
        const decision = decisionByField.get(field.field_id);
        const constraints = formConstraintSummary(field);
        const status = decision?.disposition === 'resolved'
            ? `Resolved · ${decision.provenance || 'recorded source'}`
            : decision?.reason_code || 'Operator review required';
        return `<div class="qa-item" data-form-field-index="${index}">
            <div class="qa-q">
                ${esc(field.label)}
                ${field.required ? '<span class="text-warning"> · Required</span>' : ''}
                ${field.sensitive_category ? `<span class="text-warning"> · ${esc(field.sensitive_category)}</span>` : ''}
            </div>
            <div class="qa-a">${esc(field.field_type)} · ${esc(status)}</div>
            ${constraints ? `<div class="qa-a text-dim">${esc(constraints)}</div>` : ''}
            <div style="margin-top:9px;">
                ${formAnswerControl(field, decision, index, reviewable, partialPlan)}
            </div>
        </div>`;
    }).join('');
    const disclosureRows = disclosures.map(disclosure => `
        <div class="qa-item" data-form-disclosure>
            <div class="qa-q">${esc(
                String(disclosure.kind || 'information').replaceAll('_', ' ')
            )}</div>
            <div class="qa-a">${esc(disclosure.summary || 'Disclosure unavailable')}</div>
            <div class="qa-a text-dim">
                ${esc(disclosure.source || 'inline')} · evidence
                ${esc(redactedDigest(disclosure.content_sha256))}
                ${disclosure.acknowledgement_field_id
                    ? ' · Bound to an operator-reviewed consent control'
                    : ''}
            </div>
        </div>`).join('');

    panel.innerHTML = `
        <div class="qa-item">
            <div class="qa-q">${esc(planLabel)}</div>
            <div class="qa-a">${esc(plan.adapter_name)} ${esc(plan.adapter_version)} · selector ${esc(plan.selector_version)}</div>
        </div>
        ${partialPlan ? `<div class="qa-item" data-partial-plan-guidance>
            <div class="qa-q">Preparation is unavailable for this partial plan</div>
            <div class="qa-a">Confirm the unresolved field as a scoped reusable answer, then run Inspect application form again. Only a complete reinspection that reaches the employer review step can become preparation-ready.</div>
        </div>` : ''}
        <div class="qa-item">
            <div class="qa-q">Answers</div>
            <div class="qa-a">${resolved}/${fields.length} resolved · ${unresolvedRequired} required answers need review</div>
        </div>
        <div class="qa-item">
            <div class="qa-q">Provenance</div>
            <div class="qa-a">${esc(provenance.join(' · ') || 'No resolved answers')}</div>
        </div>
        <div class="qa-item">
            <div class="qa-q">Employer disclosures</div>
            <div class="qa-a">${disclosures.length} ordered notice${disclosures.length === 1 ? '' : 's'} recorded for review</div>
        </div>
        ${disclosureRows || '<div class="qa-item" data-form-disclosure><div class="qa-a text-dim">No employer disclosures were recorded.</div></div>'}
        <div class="qa-item" data-attachment-evidence>
            <div class="qa-q">Selected CV (redacted)</div>
            <div class="qa-a">${esc(plan.selected_cv_ref || 'unavailable')} · SHA-256 ${esc(redactedDigest(plan.selected_cv_hash))}</div>
        </div>
        <div class="qa-item" data-attachment-evidence>
            <div class="qa-q">Attached CV (redacted)</div>
            <div class="qa-a">${esc(plan.attached_cv_ref || 'unavailable')} · SHA-256 ${esc(redactedDigest(plan.attached_cv_hash))}</div>
        </div>
        <div class="qa-item" data-attachment-evidence>
            <div class="qa-q">Attachment evidence</div>
            <div class="qa-a">${attachmentEvidence
                ? `Browser-observed upload-complete marker · recorded ${esc(fmtDate(plan.attachment_verified_at))}`
                : 'Not verified — preparation is disabled'}</div>
        </div>
        ${blockers.length ? `<div class="qa-item">
            <div class="qa-q">Plan blockers</div>
            <div class="qa-a">${esc(blockers.join(' · '))}</div>
        </div>` : ''}
        ${fieldRows || '<div class="text-dim text-sm">No observed fields were recorded.</div>'}`;

    panel.querySelectorAll('[data-confirm-field-index]').forEach(button => {
        button.addEventListener('click', () => {
            const index = Number(button.dataset.confirmFieldIndex);
            void confirmFormAnswer(appId, plan, index);
        });
    });
}

function readFormAnswer(field, index) {
    const control = $(`form-answer-${index}`);
    if (!control) throw new Error('This form control cannot be confirmed here.');
    if (typeof control.checkValidity === 'function' && !control.checkValidity()) {
        if (typeof control.reportValidity === 'function') control.reportValidity();
        throw new Error('Enter an answer that satisfies the observed form constraints.');
    }
    if (field.field_type === 'multi_select') {
        const values = [...control.selectedOptions].map(option => option.value);
        if (!values.length) throw new Error('Choose at least one answer.');
        return values;
    }
    if (['checkbox', 'consent', 'attestation'].includes(field.field_type)) {
        if (control.value === '') throw new Error('Choose Yes or No.');
        return control.value === 'true';
    }
    if (field.field_type === 'number') {
        if (control.value.trim() === '') throw new Error('Enter a number.');
        const value = Number(control.value);
        if (!Number.isFinite(value)) throw new Error('Enter a valid number.');
        return value;
    }
    const value = control.value.trim();
    if (!value) throw new Error('Enter an answer before confirming.');
    return value;
}

async function confirmFormAnswer(appId, plan, index) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const field = plan.fields?.[index];
    if (!field) return;
    let value;
    try {
        value = readFormAnswer(field, index);
    } catch (error) {
        showToast(error.message, 'warning');
        return;
    }
    const partialPlan = (plan.blockers || []).includes('FORM_PLAN_INCOMPLETE');
    const reusableControl = $(`form-reusable-${index}`);
    if (partialPlan && (!field.canonical_name || reusableControl?.checked !== true)) {
        showToast(
            'Partial plans require a scoped reusable answer, followed by reinspection.',
            'warning'
        );
        return;
    }
    const reusable = Boolean(reusableControl?.checked);
    const button = document.querySelector(`[data-confirm-field-index="${index}"]`);
    if (button) button.disabled = true;
    const result = await probeJson(
        `/api/applications/${appId}/answers/${encodeURIComponent(field.field_id)}/confirm`,
        'POST',
        {
            plan_id: plan.plan_id,
            application_revision: plan.application_revision,
            value,
            reusable,
            evidence_source: 'operator_confirmation',
            evidence_reference: 'dashboard_review',
        }
    );
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (result.ok && result.data) {
        const application = state.applications.find(item => item.id === appId);
        if (application) {
            application.status = 'draft';
            application.approved_at = null;
            application.prepared_revision = null;
            application.revision = result.data.application_revision;
            application.application_revision = result.data.application_revision;
            cacheFormPlanIdentity(application, result.data);
            application.form_plan_valid = result.data.valid === true;
            application.form_plan_blockers = result.data.blockers || [];
            application.form_plan_review_ready = (
                formPlanIsReviewable(result.data)
                && formPlanHasAttachmentEvidence(result.data)
                && (result.data.blockers || []).length === 0
            );
        }
        const remainsPartial = (result.data.blockers || []).includes(
            'FORM_PLAN_INCOMPLETE'
        );
        showToast(
            remainsPartial
                ? 'Reusable answer saved. Reinspect the employer form; this partial plan cannot be prepared.'
                : 'Answer confirmed. Review the updated plan, then prepare again.',
            'info'
        );
        renderFormPlanPanel(appId, result.data);
        const prepareButton = $('btn-approve-app');
        if (prepareButton && application) {
            prepareButton.disabled = application.form_plan_review_ready !== true;
            prepareButton.title = prepareButton.disabled
                ? 'Resolve all current form blockers before preparation'
                : '';
        }
        return;
    }
    if (result.status === 409) {
        const reasonCode = result.data?.detail?.code;
        showToast(
            boundedApiError(
                result.data,
                `Answer confirmation rejected (HTTP ${result.status})`
            ),
            'warning'
        );
        if (['FORM_CHANGED', 'ANSWER_POLICY_CHANGED'].includes(reasonCode)) {
            const refreshed = await probeJson(`/api/applications/${appId}/form-plan`);
            if (refreshed.ok && refreshed.data) renderFormPlanPanel(appId, refreshed.data);
        } else if (button) {
            button.disabled = false;
        }
        return;
    }
    showToast(
        boundedApiError(result.data, `Answer confirmation failed (HTTP ${result.status})`),
        'error'
    );
    if (button) button.disabled = false;
}

window.openReviewModal = async appId => {
    const app = state.applications.find(a => a.id === appId);
    if (!app) return;
    const requestToken = beginReviewModalRequest(appId);
    const reviewModal = $('review-modal');
    const actionButtons = [
        $('btn-preview-cv'),
        $('btn-override-cv'),
        $('btn-inspect-app'),
        $('btn-approve-app'),
        $('btn-reject-app'),
        $('btn-retry-app'),
        $('btn-allow-remote-send'),
        $('btn-send-app'),
    ].filter(Boolean);
    actionButtons.forEach(button => {
        button.disabled = true;
        button.onclick = null;
    });
    $('btn-allow-remote-send').style.display = 'none';
    $('btn-send-app').style.display = 'none';
    const initialSendReason = $('modal-send-disabled-reason');
    if (initialSendReason) {
        initialSendReason.style.display = 'none';
        initialSendReason.textContent = '';
    }
    $('btn-approve-app').innerHTML = 'Prepare application';
    $('btn-allow-remote-send').innerHTML = '<i data-lucide="shield-check"></i> Allow remote Send';
    $('btn-send-app').innerHTML = '<i data-lucide="send"></i> Send application';

    $('modal-job-title').textContent = app.job_title;
    $('modal-company').textContent = app.job_company;
    $('modal-score').textContent = app.job_score;
    $('modal-apply-url').href = app.apply_url || '#';
    $('modal-cover-letter').value = app.cover_letter || '';
    $('modal-recruiter-msg').textContent = app.recruiter_message || 'N/A';
    const routingEvidence = (app.cv_routing_evidence || []).join(' · ');
    $('modal-cv-routing').textContent = app.selected_cv_id
        ? `${app.selected_cv_ref || 'redacted CV reference unavailable'} · SHA-256 ${redactedDigest(app.selected_cv_hash)} · confidence ${Math.round((app.cv_routing_confidence || 0) * 100)}%${routingEvidence ? ' · ' + routingEvidence : ''}`
        : `Review required${app.cv_routing_fallback_reason ? ' · ' + app.cv_routing_fallback_reason : ''}`;
    $('btn-preview-cv').onclick = () => previewCvRoute(app.id);
    $('btn-override-cv').onclick = () => overrideCvRoute(app.id);

    const materialBlockers = app.material_blockers || [];
    const claimEvidence = app.material_claim_evidence || [];
    const supportedClaims = claimEvidence.filter(claim => claim.supported === true).length;
    const materialState = app.material_eligible === true
        ? 'Eligible after evidence validation'
        : app.material_eligible === false
            ? 'Blocked — operator review required'
            : 'Legacy material — no v4 audit';
    const modelLabel = app.material_model_provider
        ? `${app.material_model_provider} ${app.material_model_name || ''}${app.material_model_digest ? ' · ' + app.material_model_digest.slice(0, 19) + '…' : ''}`
        : 'No model identity recorded';
    $('modal-material-quality').innerHTML = `
        <div class="qa-item">
            <div class="qa-q">${esc(materialState)}</div>
            <div class="qa-a">${esc(modelLabel)}</div>
        </div>
        <div class="qa-item">
            <div class="qa-q">Claim evidence</div>
            <div class="qa-a">${supportedClaims}/${claimEvidence.length} factual claims supported</div>
        </div>
        ${materialBlockers.length ? `<div class="qa-item">
            <div class="qa-q">Blockers</div>
            <div class="qa-a">${esc(materialBlockers.join(' · '))}</div>
        </div>` : ''}`;

    const formPlanPanel = $('modal-form-plan');
    formPlanPanel.innerHTML = '<div class="text-dim text-sm">Loading the current form inspection…</div>';
    reviewModal.classList.add('visible');
    lucide.createIcons();
    if (app.form_plan_id) {
        const planResult = await probeJson(`/api/applications/${app.id}/form-plan`);
        if (!isCurrentReviewModalRequest(app.id, requestToken)) return;
        if (planResult.ok) {
            cacheFormPlanIdentity(app, planResult.data);
            app.form_plan_expires_at = planResult.data?.expires_at || null;
            app.form_plan_invalidated_at = planResult.data?.invalidated_at || null;
            app.form_plan_valid = (
                planResult.data?.valid === true
                && !app.form_plan_invalidated_at
            );
            app.form_plan_review_ready = (
                formPlanIsReviewable(planResult.data)
                && formPlanHasAttachmentEvidence(planResult.data)
                && (planResult.data?.blockers || []).length === 0
            );
            renderFormPlanPanel(app.id, planResult.data);
        } else {
            app.form_plan_valid = false;
            app.form_plan_review_ready = false;
            app.form_plan_expires_at = null;
            app.form_plan_invalidated_at = null;
            formPlanPanel.innerHTML = '<div class="text-dim text-sm">The recorded plan is no longer available.</div>';
        }
    } else {
        app.form_plan_valid = false;
        app.form_plan_review_ready = false;
        app.form_plan_expires_at = null;
        app.form_plan_invalidated_at = null;
        formPlanPanel.innerHTML = '<div class="text-dim text-sm">No current form inspection is available.</div>';
    }

    if (!isCurrentReviewModalRequest(app.id, requestToken)) return;

    // Q&A
    let qaHtml = '';
    if (app.qa_answers && Object.keys(app.qa_answers).length > 0) {
        for (const [k, v] of Object.entries(app.qa_answers)) {
            const label = k.split('_').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
            qaHtml += `<div class="qa-item">
                <div class="qa-q">${esc(label)}</div>
                <div class="qa-a">${esc(v)}</div>
            </div>`;
        }
    } else {
        qaHtml = '<div class="text-dim text-sm">No Q&amp;A generated</div>';
    }
    $('modal-qa-list').innerHTML = qaHtml;

    const events = app.events || [];
    const attempts = app.attempts || [];
    const history = [
        ...events.map(event => ({
            time: event.created_at,
            title: event.event_type.replace(/_/g, ' '),
            detail: `${event.actor}${event.details?.reason_code ? ' · ' + event.details.reason_code : ''}`,
        })),
        ...attempts.map(attemptHistoryItem),
    ].sort((a, b) => new Date(b.time || 0) - new Date(a.time || 0));
    $('modal-audit-list').innerHTML = history.length
        ? history.map(item => `<div class="qa-item">
            <div class="qa-q">${esc(item.title)}</div>
            <div class="qa-a">${esc(item.detail)} · ${fmtDate(item.time)}</div>
        </div>`).join('')
        : '<div class="text-dim text-sm">No automation events yet</div>';

    // Submission result (if exists). Raw "success" is deliberately not enough:
    // only an explicit employer-verification bit may render the green state.
    if (app.submission_status) {
        const outcome = attemptOutcome(app);
        const verified = isEmployerVerified(app);
        const isDraft = outcome === 'draft_only';
        const isFailed = ['failed', 'failed_before_commit'].includes(outcome);
        const banner    = $('modal-submission-banner');
        if (banner) {
            const icon = verified ? '✅' : isFailed ? '❌' : '⚠️';
            const headline = verified
                ? 'Employer-confirmed submission'
                : isDraft
                    ? 'Draft only — not submitted'
                    : isFailed
                        ? 'Submission was not confirmed'
                        : ['success', 'confirmed_submitted'].includes(outcome)
                            ? 'Unverified result — not proof of submission'
                            : `${String(outcome || 'Unknown result').replace(/_/g, ' ')} — review required`;
            const platform  = (app.submission_platform || '').replace(/_/g, ' ');
            banner.className = `submission-banner ${verified ? 'status-success' : isFailed ? 'status-failed' : 'status-unverified'}`;
            banner.style.display = 'flex';
            banner.innerHTML = `<span style="font-size:1.2rem;">${icon}</span>
                <div>
                    <div style="font-weight:600;">${headline}</div>
                    <div style="font-size:0.82rem;opacity:0.8;">${platform ? 'Platform: ' + esc(platform) : ''}${app.submission_reason_code ? ' · ' + esc(app.submission_reason_code) : ''}</div>
                </div>`;
        }
    } else {
        const banner = $('modal-submission-banner');
        if (banner) banner.style.display = 'none';
    }

    const isPending = isReviewableApplication(app);
    const outcome = attemptOutcome(app);
    const canRetry = ['failed', 'failed_before_commit', 'draft_only'].includes(outcome);
    const isPrepared = isPreparedApplication(app);
    const inspectBtn = $('btn-inspect-app');
    const requiresPlan = requiresVersionedFormPlan(app);
    const inspectionBlockers = adapterInspectionBlockers(app);
    if (inspectBtn) {
        inspectBtn.style.display = isPending && requiresPlan
            ? 'inline-flex'
            : 'none';
        inspectBtn.disabled = (
            app.material_eligible !== true
            || !app.selected_cv_id
            || inspectionBlockers.length > 0
        );
        inspectBtn.title = app.material_eligible !== true || !app.selected_cv_id
            ? 'A routed CV and evidence-eligible material are required first'
            : inspectionBlockers.length
                ? inspectionBlockers.join(' · ')
                : 'May upload or replace the routed CV to verify it; never clicks final submit';
        inspectBtn.onclick = () => inspectApplicationForm(app.id);
    }
    $('btn-approve-app').style.display = isPending ? 'inline-flex' : 'none';
    $('btn-reject-app').style.display  = isPending ? 'inline-flex' : 'none';
    $('btn-approve-app').disabled = (
        requiresPlan
        && app.form_plan_review_ready !== true
    );
    $('btn-approve-app').title = $('btn-approve-app').disabled
        ? 'Inspect and resolve the current employer form before preparation'
        : '';
    $('btn-reject-app').disabled = false;
    $('btn-preview-cv').disabled = false;
    $('btn-override-cv').disabled = false;
    const retryBtn = $('btn-retry-app');
    if (retryBtn) {
        retryBtn.style.display = canRetry ? 'inline-flex' : 'none';
        retryBtn.disabled = false;
    }
    const remoteSendBtn = $('btn-allow-remote-send');
    const sendBtn = $('btn-send-app');
    const sendReason = $('modal-send-disabled-reason');
    const sendUi = finalSendUiState(app);
    if (remoteSendBtn) {
        remoteSendBtn.style.display = sendUi.visible ? 'inline-flex' : 'none';
        remoteSendBtn.disabled = !sendUi.visible;
        remoteSendBtn.title = sendUi.visible
            ? 'Create a one-use, five-minute permit for this exact reviewed application'
            : '';
        remoteSendBtn.onclick = () => handleAllowRemoteSend(app.id);
    }
    if (sendBtn) {
        sendBtn.style.display = sendUi.visible ? 'inline-flex' : 'none';
        sendBtn.disabled = !sendUi.visible;
        sendBtn.title = '';
        sendBtn.onclick = () => handleSend(app.id);
        if (sendReason) {
            sendReason.style.display = sendUi.candidate && !sendUi.visible
                ? 'block'
                : 'none';
            sendReason.textContent = sendUi.candidate && !sendUi.visible
                ? `Send application is hidden until every live-readiness and form-plan blocker is resolved: ${sendUi.blockers.join(' · ')}`
                : '';
        }
    } else if (sendReason) {
        sendReason.style.display = 'none';
    }
    // Material eligibility is bound to the exact audited text. Editing this
    // field would create a false impression that a correction was persisted.
    $('modal-cover-letter').readOnly = true;

    $('btn-approve-app').onclick = () => handlePrepare(app.id);
    $('btn-reject-app').onclick  = () => handleReject(app.id);
    if (retryBtn) retryBtn.onclick = () => handleRetry(app.id);

    reviewModal.classList.add('visible');
    lucide.createIcons();
};

async function previewCvRoute(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const result = await apiCall('/api/cv-routing/preview', 'POST', { application_id: appId });
    if (!isCurrentReviewModalRequest(appId, requestToken) || !result) return;
    showToast(result.selected_cv_id ? 'CV routing preview is ready for review' : 'Routing abstained — choose a CV', 'info');
    hideModal($('review-modal'));
    await refreshAllData();
}

async function overrideCvRoute(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const cvId = $('modal-cv-override').value.trim();
    if (!cvId) {
        showToast('Enter a configured CV id', 'info');
        return;
    }

    const result = await apiCall(`/api/applications/${appId}/cv-override`, 'POST', { cv_id: cvId });
    if (!isCurrentReviewModalRequest(appId, requestToken) || !result) return;
    showToast('CV override saved. Reinspect the employer form before preparation.', 'info');
    hideModal($('review-modal'));
    await refreshAllData();
}

async function inspectApplicationForm(appId) {
    const requestToken = reviewModalState.requestToken;
    const application = state.applications.find(item => item.id === appId);
    const button = $('btn-inspect-app');
    if (!application || !isCurrentReviewModalRequest(appId, requestToken)) return;

    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Inspecting…';
    lucide.createIcons();
    const result = await probeJson(
        `/api/applications/${appId}/inspect`,
        'POST',
        { application_revision: application.revision }
    );
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    button.innerHTML = '<i data-lucide="scan-search" style="width:14px;height:14px;"></i> Inspect application form';
    lucide.createIcons();

    if (!result.ok || !result.data) {
        button.disabled = false;
        showToast(
            boundedApiError(result.data, `Form inspection stopped (HTTP ${result.status})`),
            result.status === 409 ? 'warning' : 'error'
        );
        return;
    }

    cacheFormPlanIdentity(application, result.data);
    application.form_plan_expires_at = result.data.expires_at;
    application.form_plan_invalidated_at = result.data.invalidated_at;
    application.form_plan_valid = result.data.valid === true;
    application.prepared_revision = null;
    application.approved_at = null;
    application.form_plan_review_ready = (
        formPlanIsReviewable(result.data)
        && formPlanHasAttachmentEvidence(result.data)
        && (result.data.blockers || []).length === 0
    );
    application.form_plan_blockers = result.data.blockers || [];
    renderFormPlanPanel(appId, result.data);
    const prepareButton = $('btn-approve-app');
    if (prepareButton) {
        prepareButton.disabled = application.form_plan_review_ready !== true;
        prepareButton.title = prepareButton.disabled
            ? 'Resolve all current form blockers before preparation'
            : '';
    }
    const incomplete = (result.data.blockers || []).includes('FORM_PLAN_INCOMPLETE');
    showToast(
        incomplete
            ? 'Partial form inspected — save scoped reusable answers, then reinspect; preparation is unavailable.'
            : result.data.blockers?.length
            ? 'Form inspected — review the unresolved fields before preparation'
            : 'Form inspected — exact CV upload evidence recorded for review',
        result.data.blockers?.length ? 'warning' : 'info'
    );
    button.disabled = false;
}

window.copyCoverLetter = () => {
    const ta = $('modal-cover-letter');
    const text = ta.value;
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
            .then(() => showToast('Cover letter copied to clipboard', 'info'))
            .catch(() => showToast('Copy failed — select text and press Ctrl+C', 'info'));
    } else {
        ta.select();
        showToast('Select text and press Ctrl+C to copy', 'info');
    }
};

function operationStatusUrl(result) {
    const raw = result?.status_url || result?.attempt?.status_url;
    const attemptId = result?.attempt_id || result?.attempt?.id;
    const candidate = raw || (attemptId ? `/api/submission-attempts/${attemptId}` : '');
    if (!candidate) return '';
    try {
        const parsed = new URL(candidate, location.origin);
        return parsed.origin === location.origin ? `${parsed.pathname}${parsed.search}` : '';
    } catch {
        return '';
    }
}

function isTerminalAttemptResult(result) {
    if (isEmployerVerified(result)) return true;
    return new Set([
        'confirmed_submitted',
        'success',
        'prepared',
        'already_applied',
        'needs_review',
        'unknown',
        'failed',
        'failed_before_commit',
        'draft_only',
        'operator_confirmed',
        'legacy_unverified',
    ]).has(attemptOutcome(result));
}

function showAttemptOutcome(result, operation = 'Application') {
    const outcome = attemptOutcome(result);
    if (isEmployerVerified(result)) {
        showToast('Employer confirmed this application submission', 'success');
        return;
    }
    const messages = {
        prepared: ['Application prepared — nothing was submitted', 'info'],
        draft_only: ['Draft-only run completed — nothing was submitted', 'warning'],
        failed: ['Submission failed before employer confirmation', 'error'],
        failed_before_commit: ['Stopped before the final external action', 'error'],
        unknown: ['Submission outcome is unknown — manual review is required', 'warning'],
        needs_review: ['Application needs review — nothing is confirmed', 'warning'],
        already_applied: ['Employer reports an earlier application; no new submission was counted', 'info'],
        operator_confirmed: ['Operator reconciliation recorded — employer verification is still unavailable', 'warning'],
        legacy_unverified: ['Legacy result is unverified and is not counted as submitted', 'warning'],
        success: ['Backend reported success without employer evidence — not counted', 'warning'],
        confirmed_submitted: ['Submission claim lacks employer verification evidence — not counted', 'warning'],
    };
    const [message, type] = messages[outcome] || [`${operation} status: ${outcome || 'pending'}`, 'info'];
    showToast(message, type);
}

async function monitorOperation(result, operation) {
    if (isTerminalAttemptResult(result)) {
        showAttemptOutcome(result, operation);
        await refreshAllData();
        return;
    }

    const statusUrl = operationStatusUrl(result);
    showToast(`${operation} accepted — waiting for the recorded outcome`, 'info');
    if (!statusUrl) {
        await refreshAllData();
        return;
    }

    for (let attempt = 0; attempt < 20; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        const probe = await probeJson(statusUrl);
        if (!probe.ok || !probe.data) continue;
        if (isTerminalAttemptResult(probe.data)) {
            showAttemptOutcome(probe.data, operation);
            await refreshAllData();
            return;
        }
    }

    showToast(`${operation} is still in progress — no submission is confirmed yet`, 'info');
    await refreshAllData();
}

async function requestPreparation(appId) {
    const preferred = await probeJson(`/api/applications/${appId}/prepare`, 'POST');
    if (preferred.ok) return preferred.data;
    if (preferred.status === 404 || preferred.status === 405) {
        // Compatibility with an older safe-mode API. A live-capable backend
        // must expose the preparation endpoint so this legacy route is never
        // mistaken for a non-submitting action.
        const mode = state.runtimeCapabilities?.mode || {};
        if (mode.dry_run !== true && mode.draft_only !== true) {
            showToast('Preparation endpoint unavailable; refusing an ambiguous legacy action', 'error');
            return null;
        }
        return apiCall(`/api/applications/${appId}/approve`, 'POST');
    }
    if (preferred.status === 401 || preferred.status === 403) {
        showToast('Authentication failed. Check API Secret.', 'error');
        return null;
    }
    showToast(
        boundedApiError(preferred.data, `Preparation failed (HTTP ${preferred.status})`),
        'error'
    );
    return null;
}

async function handlePrepare(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const btn = $('btn-approve-app');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Preparing…';
    lucide.createIcons();
    const res = await requestPreparation(appId);
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (res) {
        hideModal($('review-modal'));
        void monitorOperation(res, 'Preparation');
    }
    btn.disabled = false;
    btn.innerHTML = 'Prepare application';
    lucide.createIcons();
}

function sendIdempotencyStorageKey(application) {
    const revision = application.revision ?? application.application_revision ?? 'unknown';
    return `job-agent-send:${application.id}:${revision}:${application.form_plan_id || 'no-plan'}`;
}

function getOrCreateSendIdempotencyKey(application) {
    const storageKey = sendIdempotencyStorageKey(application);
    let value = state.sendIdempotencyKeys.get(storageKey) || '';
    try {
        value = value || sessionStorage.getItem(storageKey) || '';
    } catch {
        // The in-memory map still preserves the key for this loaded dashboard.
    }
    if (!/^[A-Za-z0-9_.:-]{8,128}$/.test(value)) {
        value = globalThis.crypto?.randomUUID
            ? globalThis.crypto.randomUUID()
            : `dashboard-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    state.sendIdempotencyKeys.set(storageKey, value);
    try {
        sessionStorage.setItem(storageKey, value);
    } catch {
        // Session storage can be disabled; the in-memory map remains authoritative.
    }
    return { storageKey, value };
}

function clearSendIdempotencyKey(storageKey) {
    state.sendIdempotencyKeys.delete(storageKey);
    try {
        sessionStorage.removeItem(storageKey);
    } catch {
        // Nothing else is required when browser storage is unavailable.
    }
}

function replaceApplicationState(application) {
    const index = state.applications.findIndex(item => item.id === application.id);
    if (index >= 0) state.applications[index] = application;
}

async function reconcileAmbiguousSend(
    application,
    previousAttemptIds,
    storageKey,
    requestToken
) {
    for (let probeAttempt = 0; probeAttempt < 3; probeAttempt += 1) {
        if (probeAttempt > 0) {
            await new Promise(resolve => setTimeout(resolve, 500));
        }
        const refreshed = await probeJson(`/api/applications/${application.id}`);
        if (!refreshed.ok || !refreshed.data) continue;
        replaceApplicationState(refreshed.data);
        const newAttempt = (refreshed.data.attempts || []).find(
            attempt => !previousAttemptIds.has(attempt.id)
        );
        if (newAttempt) {
            clearSendIdempotencyKey(storageKey);
            if (isCurrentReviewModalRequest(application.id, requestToken)) {
                hideModal($('review-modal'));
            }
            void monitorOperation({ attempt: newAttempt }, 'Recovered send request');
            return true;
        }
    }
    showToast(
        'The send response was interrupted. No new attempt is visible yet; retry will reuse the same request key.',
        'warning'
    );
    if (isCurrentReviewModalRequest(application.id, requestToken)) {
        await window.openReviewModal(application.id);
    }
    return false;
}

async function handleSend(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const app = state.applications.find(application => application.id === appId);
    if (!app) return;
    const blockers = liveSendBlockers(app);
    if (blockers.length) {
        showToast(`Send disabled: ${blockers.join(' · ')}`, 'warning');
        return;
    }
    if (!window.confirm(
        `Send this exact application to ${app.job_company}? This is the final external action.`
    )) return;

    const btn = $('btn-send-app');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Sending…';
    lucide.createIcons();
    const idempotency = getOrCreateSendIdempotencyKey(app);
    const previousAttemptIds = new Set((app.attempts || []).map(attempt => attempt.id));
    const response = await probeJson(
        `/api/applications/${appId}/submit`,
        'POST',
        {
            acknowledgement: 'SEND_APPLICATION',
            idempotency_key: idempotency.value,
            application_revision: app.revision ?? app.application_revision,
            form_plan_id: app.form_plan_id,
            client_release: LOADED_DASHBOARD_RELEASE,
        }
    );
    if (response.ok && response.data) {
        clearSendIdempotencyKey(idempotency.storageKey);
        if (isCurrentReviewModalRequest(appId, requestToken)) {
            hideModal($('review-modal'));
        }
        await refreshAllData();
        void monitorOperation(response.data, 'Send request');
        return;
    }
    if (response.status === 0 || response.status >= 500) {
        await reconcileAmbiguousSend(
            app,
            previousAttemptIds,
            idempotency.storageKey,
            requestToken
        );
        return;
    }
    clearSendIdempotencyKey(idempotency.storageKey);
    showToast(
        boundedApiError(response.data, `Send request rejected (HTTP ${response.status})`),
        'error'
    );
    const refreshed = await probeJson(`/api/applications/${appId}`);
    if (refreshed.ok && refreshed.data) replaceApplicationState(refreshed.data);
    if (isCurrentReviewModalRequest(appId, requestToken)) {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="send"></i> Send application';
        lucide.createIcons();
    }
}

async function handleAllowRemoteSend(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const app = state.applications.find(application => application.id === appId);
    if (!app) return;
    const blockers = liveSendBlockers(app);
    if (blockers.length) {
        showToast(`Remote Send disabled: ${blockers.join(' · ')}`, 'warning');
        return;
    }
    if (!window.confirm(
        'Allow the protected remote dashboard to request one Send for this exact reviewed application for up to five minutes? This does not submit now.'
    )) return;

    const btn = $('btn-allow-remote-send');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Allowing…';
    lucide.createIcons();
    const response = await probeJson(
        `/api/applications/${appId}/control-plane-review-grant`,
        'POST',
        {
            acknowledgement: 'ALLOW_REMOTE_SEND',
            application_revision: app.revision ?? app.application_revision,
            form_plan_id: app.form_plan_id,
        }
    );
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (response.ok && response.data) {
        btn.innerHTML = '<i data-lucide="shield-check"></i> Remote Send allowed';
        btn.title = `One-use review permit expires ${fmtDate(response.data.expires_at)}`;
        showToast(
            'One-use remote Send permission created. No application was submitted.',
            'info'
        );
        lucide.createIcons();
        return;
    }
    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="shield-check"></i> Allow remote Send';
    showToast(
        boundedApiError(
            response.data,
            `Remote Send permission rejected (HTTP ${response.status})`
        ),
        'error'
    );
    lucide.createIcons();
}

async function handleReject(appId) {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    const res = await apiCall(`/api/applications/${appId}/reject?reason=Skipped+from+dashboard`, 'POST');
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (res) {
        showToast('Application skipped', 'info');
        hideModal($('review-modal'));
        refreshAllData();
    }
}

window.handleRetry = async appId => {
    const requestToken = reviewModalState.requestToken;
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (!window.confirm(
        'Prepare a new retry? Nothing will be submitted. Review it, then use Send application separately.'
    )) return;
    const res = await apiCall(`/api/applications/${appId}/retry`, 'POST');
    if (!isCurrentReviewModalRequest(appId, requestToken)) return;
    if (res) {
        hideModal($('review-modal'));
        void monitorOperation(res, 'Retry preparation');
    }
};

// ── URL Queue ──────────────────────────────────────────────────────────────────
async function fetchUrls() {
    const data = await apiCall('/api/urls?limit=200');
    if (!data) return;
    state.urls = data;
    renderUrls();
}

window.retryUrl = async urlId => {
    const btn = document.getElementById(`url-retry-${urlId}`);
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    const res = await apiCall(`/api/urls/${urlId}/retry`, 'POST');
    if (res) {
        showToast('URL re-queued for processing', 'info');
        setTimeout(fetchUrls, 2000);
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
};

function renderUrls() {
    const tbody = $('urls-table-body');
    const f = state.filters.urls;
    const rows = f ? state.urls.filter(u => u.status === f) : state.urls;

    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-muted);">
            ${f ? `No ${f} URLs` : 'No URLs yet — send a job link via WhatsApp or use Add Job URL.'}
        </td></tr>`;
        return;
    }

    const statusColors = {
        fetched: 'var(--success)', pending: 'var(--warning)',
        failed: 'var(--danger)', blocked: '#f59e0b',
    };

    tbody.innerHTML = rows.map(u => {
        const shortUrl = u.url.length > 60 ? u.url.slice(0, 58) + '…' : u.url;
        const color = statusColors[u.status] || 'var(--text-muted)';
        const canRetry = u.status === 'failed' || u.status === 'blocked' || (u.status === 'fetched' && u.jobs_found === 0);
        return `
        <tr>
            <td>
                <a href="${esc(u.url)}" target="_blank" class="job-link" title="${esc(u.url)}">
                    ${esc(shortUrl)} <i data-lucide="external-link" style="width:11px;height:11px;opacity:0.5;"></i>
                </a>
                ${u.error ? `<div style="color:var(--danger);font-size:0.75rem;margin-top:2px;">${esc(u.error.slice(0,80))}</div>` : ''}
            </td>
            <td style="text-align:center;">
                <span style="font-weight:600;color:${u.jobs_found > 0 ? 'var(--success)' : 'var(--text-muted)'};">${u.jobs_found}</span>
            </td>
            <td><span class="status" style="background:${color}22;color:${color};border:1px solid ${color}44;">${u.status}</span></td>
            <td style="color:var(--text-muted);white-space:nowrap;">${fmtDate(u.created_at)}</td>
            <td style="text-align:center;">
                ${canRetry
                    ? `<button id="url-retry-${u.id}" class="btn btn-retry" style="padding:4px 12px;font-size:0.78rem;" onclick="retryUrl(${u.id})">
                        Retry
                       </button>`
                    : '—'
                }
            </td>
        </tr>`;
    }).join('');

    lucide.createIcons();
}

// ── WhatsApp: Bridge Status + Method Switcher ─────────────────────────────────

async function fetchBridgeStatus() {
    const data = await apiCall('/api/bridge/status');
    const banner = $('wa-bridge-status');
    if (!banner) return;
    if (!data) { banner.style.display = 'none'; return; }
    const connected = data.connected;
    const ago = data.last_seen ? timeAgo(new Date(data.last_seen)) : 'never';
    banner.style.display = 'flex';
    banner.style.alignItems = 'center';
    banner.style.gap = '10px';
    banner.style.padding = '10px 16px';
    banner.style.borderRadius = '8px';
    banner.style.fontSize = '0.875rem';
    banner.style.background = connected ? 'var(--success-bg, #22c55e18)' : 'var(--warning-bg, #f59e0b18)';
    banner.style.border = `1px solid ${connected ? '#22c55e44' : '#f59e0b44'}`;
    banner.innerHTML = `
        <span style="width:10px;height:10px;border-radius:50%;background:${connected ? 'var(--success)' : 'var(--warning)'};flex-shrink:0;${connected ? 'box-shadow:0 0 6px var(--success)' : ''}"></span>
        <span><strong>Bridge ${connected ? 'connected' : 'disconnected'}</strong>${connected ? '' : ` — last seen ${ago}`}
        ${connected ? `<span style="color:var(--text-muted);margin-left:8px;">· last ping ${ago}</span>` : ''}
        </span>
        ${!connected ? `<span style="color:var(--text-muted);margin-left:auto;font-size:0.8rem;">Run <code>cd bridge &amp;&amp; node whatsapp_bridge.js</code></span>` : ''}
    `;
}

window.switchWaMethod = function(method) {
    document.querySelectorAll('.wa-method-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.method === method);
    });
    $('wa-panel-bridge').style.display = method === 'bridge' ? '' : 'none';
    $('wa-panel-cloud').style.display  = method === 'cloud'  ? '' : 'none';
};

// ── Utilities: WhatsApp ────────────────────────────────────────────────────────
window.fetchMessages = fetchMessages;

function extractUrlsFromText(text) {
    const re = /https?:\/\/[^\s<>"')\]},;]+/gi;
    return [...new Set((text.match(re) || []).map(u => u.replace(/[.,;:!?)\]]+$/, '')))];
}

function isJobUrl(url) {
    try {
        const host = new URL(url).hostname.replace(/^www\./, '');
        return JOB_HOSTS.some(h => host.includes(h))
            || /\/(jobs?|careers?|apply|job-openings?)\//i.test(url);
    } catch { return false; }
}

function isShortUrl(url) {
    try {
        const host = new URL(url).hostname.replace(/^www\./, '');
        return SHORT_HOSTS.some(h => host === h);
    } catch { return false; }
}

window.copyText = id => {
    const el = $(id);
    const text = el?.textContent || '';
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
            .then(() => showToast('Copied!', 'info'))
            .catch(() => showToast('Copy failed — try selecting manually', 'info'));
    } else {
        showToast('Copy requires HTTPS — select manually', 'info');
    }
};

// ── Utilities: Toast ───────────────────────────────────────────────────────────
function showToast(message, type = 'info') {
    const icons = { success: 'check-circle-2', error: 'alert-circle', info: 'info', warning: 'alert-triangle' };
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<i data-lucide="${icons[type] || 'info'}" style="width:16px;height:16px;flex-shrink:0;"></i><span></span>`;
    toast.querySelector('span').textContent = String(message);
    $('toast-container').appendChild(toast);
    lucide.createIcons();
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(60px)';
        setTimeout(() => toast.remove(), 320);
    }, 4500);
}

// ── Utilities: Formatting ──────────────────────────────────────────────────────
function esc(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function attemptHistoryItem(attempt) {
    const stage = attempt.stage || attempt.status || 'unknown';
    const outcome = attempt.outcome || attempt.status || 'unknown';
    const adapterParts = [
        operationalToken(attempt.platform, 'unresolved ATS'),
        attempt.adapter_version
            ? `adapter ${shortOperationalToken(attempt.adapter_version)}`
            : null,
        attempt.selector_version
            ? `selector ${shortOperationalToken(attempt.selector_version)}`
            : null,
    ].filter(Boolean);
    const auditParts = [
        ...adapterParts,
        attempt.reason_code ? operationalToken(attempt.reason_code) : null,
        attempt.form_plan_fingerprint
            ? `plan ${shortOperationalToken(attempt.form_plan_fingerprint, 14)}`
            : null,
        attempt.attachment_verified === true
            ? 'attachment verified'
            : attempt.attachment_verified === false
                ? 'attachment not verified'
                : null,
        attempt.verification_kind
            ? `evidence ${operationalToken(attempt.verification_kind)}`
            : null,
        attempt.runner_release
            ? `runner ${shortOperationalToken(attempt.runner_release, 14)}`
            : null,
        attempt.created_at ? `created ${fmtDateTime(attempt.created_at)}` : null,
        attempt.reconciled_at ? `reconciled ${fmtDateTime(attempt.reconciled_at)}` : null,
    ].filter(Boolean);
    return {
        time: attempt.reconciled_at
            || attempt.finished_at
            || attempt.started_at
            || attempt.created_at,
        title: `attempt ${attempt.attempt_number} · ${operationalToken(stage)} → ${operationalToken(outcome)}`,
        detail: auditParts.join(' · '),
    };
}

function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
    catch { return iso; }
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (!Number.isFinite(date.getTime())) return '—';
    return date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function timeAgo(date) {
    const secs = Math.floor((Date.now() - date) / 1000);
    if (secs < 60) return 'just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
}
