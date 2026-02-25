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
    'careers.microsoft.com',
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
    jobs: [],
    messages: [],
    jobSearch: '',
    filters: {
        applications: 'draft',
        jobs: '',
    },
};

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const tabs       = () => document.querySelectorAll('.stat-item[data-tab]');
const views      = () => document.querySelectorAll('.view');
const appFilters = () => document.querySelectorAll('#view-applications .filter-btn');
const jobFilters = () => document.querySelectorAll('#view-jobs .filter-btn');

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();

    const saved = localStorage.getItem('job_agent_token');
    if (saved) {
        state.authToken = saved;
        $('api-secret').value = saved;
    }

    // Set full webhook URL in WhatsApp view
    $('wa-webhook-url').textContent = `${location.origin}/webhook/whatsapp`;

    setupListeners();
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
        renderApplications();
    }));

    // Job filters
    jobFilters().forEach(btn => btn.addEventListener('click', e => {
        jobFilters().forEach(b => b.classList.remove('active'));
        e.currentTarget.classList.add('active');
        state.filters.jobs = e.currentTarget.dataset.status;
        fetchJobs();
    }));

    // Job search
    $('jobs-search').addEventListener('input', e => {
        state.jobSearch = e.target.value.toLowerCase();
        renderJobs();
    });

    // Auth token
    $('api-secret').addEventListener('change', e => {
        state.authToken = e.target.value;
        localStorage.setItem('job_agent_token', state.authToken);
        refreshAllData();
    });

    // Refresh button
    $('btn-refresh').addEventListener('click', () => {
        const icon = $('btn-refresh');
        icon.classList.add('spinning');
        refreshAllData().finally(() => setTimeout(() => icon.classList.remove('spinning'), 600));
    });

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
    if (tabId === 'whatsapp') fetchMessages();
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
            showToast('Authentication failed. Check API Secret.', 'error');
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
    if (state.currentTab === 'applications') await fetchApplications();
    if (state.currentTab === 'jobs') await fetchJobs();
    if (state.currentTab === 'whatsapp') await fetchMessages();
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

    $('dashboard-stats').innerHTML = `
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="inbox"></i> Messages Received</div>
            <div class="stat-value count-anim">${d.total_messages ?? 0}</div>
            <div class="stat-sub">${d.total_urls ?? 0} URLs extracted</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="briefcase"></i> Jobs Found</div>
            <div class="stat-value count-anim">${d.total_jobs ?? 0}</div>
            <div class="stat-sub">Valid job postings</div>
        </div>
        <div class="stat-card warning-card">
            <div class="stat-header"><i data-lucide="file-check-2" style="color:var(--warning)"></i> Awaiting Review</div>
            <div class="stat-value count-anim text-warning">${d.applications_pending ?? 0}</div>
            <div class="stat-sub">Draft applications</div>
        </div>
        <div class="stat-card success-card">
            <div class="stat-header"><i data-lucide="send" style="color:var(--success)"></i> Submitted</div>
            <div class="stat-value count-anim text-success">${d.submissions_success ?? 0}</div>
            <div class="stat-sub">of ${d.submissions_total ?? 0} attempts</div>
        </div>
    `;
    lucide.createIcons();
    renderActivityFeed();
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
        ingested:  { icon: 'globe', cls: 'ingested',  label: 'Discovered' },
        scored:    { icon: 'target', cls: 'scored',   label: 'Scored' },
        draft:     { icon: 'file-edit', cls: 'drafted', label: 'Draft ready' },
        approved:  { icon: 'check-circle-2', cls: 'approved', label: 'Approved' },
        skipped:   { icon: 'skip-forward', cls: 'skipped', label: 'Skipped' },
        submitted: { icon: 'send', cls: 'submitted',  label: 'Submitted' },
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
        return `
        <div class="app-card">
            <div>
                <div class="app-meta mb-1">
                    <span class="status ${app.status}">${app.status.replace('_', ' ')}</span>
                    <span class="text-sm">${fmtDate(app.created_at)}</span>
                </div>
                <h3 class="app-title" title="${esc(app.job_title)}">${esc(app.job_title)}</h3>
                <div class="text-dim mb-1" style="font-size:0.85rem;">${esc(app.job_company)}</div>
                <div class="app-score mb-1">
                    <i data-lucide="target" style="width:14px;height:14px;"></i>
                    ${app.job_score}/100
                    <div class="score-bar-track" style="display:inline-flex;width:60px;">
                        <div class="score-bar-fill" style="width:${app.job_score}%"></div>
                    </div>
                </div>
                <div class="app-excerpt">${esc(app.cover_letter || '—')}</div>
            </div>
            <div style="border-top:1px solid var(--border-light);padding-top:14px;margin-top:auto;">
                ${isPending
                    ? `<button class="btn btn-primary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="eye" style="width:14px;height:14px;"></i> Review &amp; Approve
                       </button>`
                    : `<button class="btn btn-secondary full-width" onclick="openReviewModal(${app.id})">
                         <i data-lucide="eye" style="width:14px;height:14px;"></i> View Details
                       </button>`
                }
            </div>
        </div>`;
    }).join('');

    lucide.createIcons();
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
window.openReviewModal = appId => {
    const app = state.applications.find(a => a.id === appId);
    if (!app) return;

    $('modal-job-title').textContent = app.job_title;
    $('modal-company').textContent   = app.job_company;
    $('modal-score').textContent     = app.job_score;
    $('modal-apply-url').href        = app.apply_url || '#';
    $('modal-cover-letter').value    = app.cover_letter || '';
    $('modal-recruiter-msg').textContent = app.recruiter_message || 'N/A';

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

    const isPending = app.status === 'draft';
    $('btn-approve-app').style.display = isPending ? 'inline-flex' : 'none';
    $('btn-reject-app').style.display  = isPending ? 'inline-flex' : 'none';
    $('modal-cover-letter').readOnly   = !isPending;

    $('btn-approve-app').onclick = () => handleApprove(app.id);
    $('btn-reject-app').onclick  = () => handleReject(app.id);

    $('review-modal').classList.add('visible');
    lucide.createIcons();
};

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
    if (secs < 60)  return 'just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
}
