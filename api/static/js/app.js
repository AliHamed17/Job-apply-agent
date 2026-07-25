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
};

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
        btn.addEventListener('click', e => e.target.closest('.modal').classList.remove('visible'));
    });
    document.querySelectorAll('.modal').forEach(m => {
        m.addEventListener('click', e => { if (e.target === m) m.classList.remove('visible'); });
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
        if (e.key === 'Escape') document.querySelectorAll('.modal.visible').forEach(m => m.classList.remove('visible'));
    });
}

function isInputFocused() {
    const tag = document.activeElement?.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA';
}

// ── Tab Switching ─────────────────────────────────────────────────────────────
const TAB_TITLES = {
    dashboard: 'Dashboard',
    applications: 'Approvals',
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
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return await res.json();
    } catch (err) {
        showToast(err.message, 'error');
        return null;
    }
}

async function refreshAllData() {
    await fetchDashboard();
    await fetchOverview();
    // Always keep jobs current (used by dashboard histogram + CSV export)
    await fetchJobs();
    await fetchProfileSummary();
    if (state.currentTab === 'applications') await fetchApplications();
    if (state.currentTab === 'urls') await fetchUrls();
    if (state.currentTab === 'whatsapp') { await fetchMessages(); await fetchBridgeStatus(); }
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
        showToast('Automation resumed', 'success');
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
            <div class="stat-header"><i data-lucide="send" style="color:var(--success)"></i> Submitted</div>
            <div class="stat-value count-anim text-success">${d.submissions_success ?? 0}</div>
            <div class="stat-sub">of ${d.submissions_total ?? 0} attempts</div>
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
            <div class="stat-header"><i data-lucide="percent"></i> Submit Success Rate</div>
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
            <div class="stat-sub">${Object.entries(d.selector_failure_clusters || {}).map(([reason, count]) => `${esc(reason)}: ${count}`).join(' · ') || 'No selector failures'}</div>
        </div>
    `;

    lucide.createIcons();
    renderUrlHealthBanner(d);
    renderPipelineFunnel(d);
    renderScoreHistogram(d);
    renderActivityFeed();
}

function renderPipelineFunnel(d) {
    const container = $('pipeline-funnel');
    if (!container) return;

    const steps = [
        { label: 'URLs', value: d.total_urls ?? 0, icon: 'link', cls: '' },
        { label: 'Jobs', value: d.total_jobs ?? 0, icon: 'briefcase', cls: '' },
        { label: 'Applications', value: (d.applications_pending ?? 0) + (d.applications_approved ?? 0) + (d.applications_skipped ?? 0), icon: 'file-text', cls: '' },
        { label: 'Approved', value: d.applications_approved ?? 0, icon: 'check-circle-2', cls: 'approved' },
        { label: 'Submitted', value: d.submissions_success ?? 0, icon: 'send', cls: 'submitted' },
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
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const data = await res.json();
        renderProfileSummary(data);
        showToast(
            `CV processed — profile v${data.version} rebuilt, ${data.rescored} job${data.rescored !== 1 ? 's' : ''} rescored`,
            'success'
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
    const headers = ['Title', 'Company', 'Location', 'Employment Type', 'Score', 'Status', 'Date'];
    const rows = state.jobs.map(j => [
        j.title || '',
        j.company || '',
        j.location || '',
        j.employment_type || '',
        j.score ?? '',
        j.status || '',
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
    showToast(`Exported ${state.jobs.length} jobs`, 'success');
}

function renderActivityFeed() {
    const container = $('activity-feed');
    const apps = [...state.applications].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 12);
    const jobs = [...state.jobs].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 12);

    // Merge and sort
    const events = [
        ...apps.map(a => ({
            ts: new Date(a.created_at),
            type: a.status,
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
        approved: { icon: 'check-circle-2', cls: 'approved', label: 'Approved' },
        skipped: { icon: 'skip-forward', cls: 'skipped', label: 'Skipped' },
        submitted: { icon: 'send', cls: 'submitted', label: 'Submitted' },
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
function renderApplications() {
    const filtered = state.applications.filter(a => a.status === state.filters.applications);
    const container = $('applications-list');
    updateBatchApproveButton();

    if (!filtered.length) {
        container.innerHTML = `
            <div class="empty-state">
                <i data-lucide="inbox"></i>
                <h3>No ${state.filters.applications} applications</h3>
                <p>Applications with status '${state.filters.applications}' will appear here.</p>
            </div>`;
        lucide.createIcons();
        return;
    }

    container.innerHTML = filtered.map(app => {
        const isPending = app.status === 'draft';
        const canRetry = app.submission_status === 'failed' || app.submission_status === 'draft_only';

        // Submission badge
        let subBadge = '';
        if (app.submission_status) {
            const badgeMap = {
                success:    ['sub-badge-success', '✅ Submitted'],
                draft_only: ['sub-badge-draft',   '⚠️ Draft Only'],
                failed:     ['sub-badge-failed',  '❌ Failed'],
            };
            const [cls, label] = badgeMap[app.submission_status] || ['sub-badge-draft', app.submission_status];
            const platform = app.submission_platform ? ` · ${app.submission_platform.replace(/_/g, ' ')}` : '';
            subBadge = `<div class="sub-badge ${cls}">${label}${esc(platform)}</div>`;
        }

        return `
        <div class="app-card">
            <div>
                <div class="app-meta mb-1">
                    <span class="status ${app.status}">${app.status.replace('_', ' ')}</span>
                    <span class="text-sm">${fmtDate(app.created_at)}</span>
                </div>
                ${isPending ? `<label class="batch-select">
                    <input type="checkbox"
                           ${state.selectedApplications.has(app.id) ? 'checked' : ''}
                           onchange="toggleApplicationSelection(${app.id}, this.checked)">
                    Select for batch approval
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
            </div>
            <div style="border-top:1px solid var(--border-light);padding-top:14px;margin-top:auto;display:flex;flex-direction:column;gap:8px;">
                ${isPending
                ? `<button class="btn btn-primary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="eye" style="width:14px;height:14px;"></i> Review &amp; Approve
                       </button>`
                : `<button class="btn btn-secondary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="send" style="width:14px;height:14px;"></i> View Submission
                       </button>`
                }
                ${canRetry
                ? `<button class="btn btn-retry full-width" onclick="handleRetry(${app.id})">
                         <i data-lucide="refresh-cw" style="width:14px;height:14px;"></i> Retry Submission
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
    btn.innerHTML = `<i data-lucide="check-check" style="width:16px"></i> Approve selected (${count})`;
    if (count > 0) lucide.createIcons();
}

async function handleBatchApprove() {
    const ids = [...state.selectedApplications];
    if (!ids.length) return;
    if (!window.confirm(
        `Approve and queue exactly ${ids.length} reviewed application${ids.length === 1 ? '' : 's'}?`
    )) return;
    const result = await apiCall('/api/applications/batch-approve', 'POST', {
        application_ids: ids,
        acknowledgement: 'APPROVE_SELECTED_APPLICATIONS',
    });
    if (!result) return;
    state.selectedApplications.clear();
    showToast(`${result.queued_application_ids.length} application(s) queued`, 'success');
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
            <td><span class="status ${job.status}">${(job.status || 'pending').replace('_', ' ')}</span></td>
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
        const res = await apiCall('/api/ingest', 'POST', { url: raw, sender: 'dashboard' });
        if (res) {
            const added = res.added ?? 1;
            const skipped = res.skipped ?? 0;
            showToast(
                skipped > 0
                    ? `${added} URL(s) queued, ${skipped} duplicate(s) skipped`
                    : `${added} URL(s) queued for processing`,
                'success'
            );
            $('ingest-url').value = '';
            $('ingest-modal').classList.remove('visible');
            setTimeout(refreshAllData, 2500);
        }
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="zap"></i> Process URL(s)';
        lucide.createIcons();
    }
}

// ── Review Modal ──────────────────────────────────────────────────────────────
window.openReviewModal = async appId => {
    const app = state.applications.find(a => a.id === appId);
    if (!app) return;

    $('modal-job-title').textContent = app.job_title;
    $('modal-company').textContent = app.job_company;
    $('modal-score').textContent = app.job_score;
    $('modal-apply-url').href = app.apply_url || '#';
    $('modal-cover-letter').value = app.cover_letter || '';
    $('modal-recruiter-msg').textContent = app.recruiter_message || 'N/A';
    const routingEvidence = (app.cv_routing_evidence || []).join(' · ');
    $('modal-cv-routing').textContent = app.selected_cv_id
        ? `${app.selected_cv_id} · confidence ${Math.round((app.cv_routing_confidence || 0) * 100)}%${routingEvidence ? ' · ' + routingEvidence : ''}`
        : `Review required${app.cv_routing_fallback_reason ? ' · ' + app.cv_routing_fallback_reason : ''}`;
    $('btn-preview-cv').onclick = () => previewCvRoute(app.id);
    $('btn-override-cv').onclick = () => overrideCvRoute(app.id);

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
        ...attempts.map(attempt => ({
            time: attempt.finished_at || attempt.started_at,
            title: `attempt ${attempt.attempt_number} · ${attempt.status}`,
            detail: `${attempt.platform || 'unresolved'}${attempt.reason_code ? ' · ' + attempt.reason_code : ''}`,
        })),
    ].sort((a, b) => new Date(b.time || 0) - new Date(a.time || 0));
    $('modal-audit-list').innerHTML = history.length
        ? history.map(item => `<div class="qa-item">
            <div class="qa-q">${esc(item.title)}</div>
            <div class="qa-a">${esc(item.detail)} · ${fmtDate(item.time)}</div>
        </div>`).join('')
        : '<div class="text-dim text-sm">No automation events yet</div>';

    // Submission result (if exists)
    if (app.submission_status) {
        const isSuccess = app.submission_status === 'success';
        const isDraft   = app.submission_status === 'draft_only';
        const banner    = $('modal-submission-banner');
        if (banner) {
            const icon      = isSuccess ? '✅' : isDraft ? '⚠️' : '❌';
            const headline  = isSuccess ? 'Submitted successfully' : isDraft ? 'Saved as draft — not submitted' : 'Submission failed';
            const platform  = (app.submission_platform || '').replace(/_/g, ' ');
            banner.className = `submission-banner ${isSuccess ? 'status-success' : isDraft ? 'status-draft' : 'status-failed'}`;
            banner.style.display = 'flex';
            banner.innerHTML = `<span style="font-size:1.2rem;">${icon}</span>
                <div>
                    <div style="font-weight:600;">${headline}</div>
                    <div style="font-size:0.82rem;opacity:0.8;">${platform ? 'Platform: ' + esc(platform) : ''}${app.submission_error ? ' — ' + esc(app.submission_error.slice(0,120)) : ''}</div>
                </div>`;
        }
    } else {
        const banner = $('modal-submission-banner');
        if (banner) banner.style.display = 'none';
    }

    const isPending = app.status === 'draft';
    const canRetry  = app.submission_status === 'failed' || app.submission_status === 'draft_only';
    $('btn-approve-app').style.display = isPending ? 'inline-flex' : 'none';
    $('btn-reject-app').style.display  = isPending ? 'inline-flex' : 'none';
    const retryBtn = $('btn-retry-app');
    if (retryBtn) retryBtn.style.display = canRetry ? 'inline-flex' : 'none';
    $('modal-cover-letter').readOnly = !isPending;

    $('btn-approve-app').onclick = () => handleApprove(app.id);
    $('btn-reject-app').onclick  = () => handleReject(app.id);
    if (retryBtn) retryBtn.onclick = () => handleRetry(app.id);

    $('review-modal').classList.add('visible');
    lucide.createIcons();
};

async function previewCvRoute(appId) {
    const result = await apiCall('/api/cv-routing/preview', 'POST', { application_id: appId });
    if (!result) return;
    showToast(result.selected_cv_id ? `Selected CV: ${result.selected_cv_id}` : 'Routing abstained — choose a CV', result.selected_cv_id ? 'success' : 'info');
    await refreshAllData();
    $('review-modal').classList.remove('visible');
}

async function overrideCvRoute(appId) {
    const cvId = $('modal-cv-override').value.trim();
    if (!cvId) {
        showToast('Enter a configured CV id', 'info');
        return;
    }

    const result = await apiCall(`/api/applications/${appId}/cv-override`, 'POST', { cv_id: cvId });
    if (!result) return;
    showToast(`CV override saved: ${result.selected_cv_id}`, 'success');
    await refreshAllData();
    $('review-modal').classList.remove('visible');
}

window.copyCoverLetter = () => {
    const ta = $('modal-cover-letter');
    const text = ta.value;
    if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
            .then(() => showToast('Cover letter copied to clipboard', 'success'))
            .catch(() => showToast('Copy failed — select text and press Ctrl+C', 'info'));
    } else {
        ta.select();
        showToast('Select text and press Ctrl+C to copy', 'info');
    }
};

async function handleApprove(appId) {
    const btn = $('btn-approve-app');
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" style="width:14px;height:14px;animation:spin 1s linear infinite;"></i> Approving…';
    lucide.createIcons();
    const res = await apiCall(`/api/applications/${appId}/approve`, 'POST');
    if (res) {
        showToast('Application approved and queued for submission', 'success');
        $('review-modal').classList.remove('visible');
        refreshAllData();
    }
    btn.disabled = false;
    btn.innerHTML = 'Approve &amp; Submit';
    lucide.createIcons();
}

async function handleReject(appId) {
    const res = await apiCall(`/api/applications/${appId}/reject?reason=Skipped+from+dashboard`, 'POST');
    if (res) {
        showToast('Application skipped', 'info');
        $('review-modal').classList.remove('visible');
        refreshAllData();
    }
}

window.handleRetry = async appId => {
    const res = await apiCall(`/api/applications/${appId}/retry`, 'POST');
    if (res) {
        showToast('Re-queued for submission — check back in a moment', 'success');
        $('review-modal').classList.remove('visible');
        setTimeout(refreshAllData, 3000);
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
        showToast('URL re-queued for processing', 'success');
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
            .then(() => showToast('Copied!', 'success'))
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
    toast.innerHTML = `<i data-lucide="${icons[type] || 'info'}" style="width:16px;height:16px;flex-shrink:0;"></i><span>${message}</span>`;
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

function fmtDate(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
    catch { return iso; }
}

function timeAgo(date) {
    const secs = Math.floor((Date.now() - date) / 1000);
    if (secs < 60) return 'just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
}
