/**
 * AI Job Apply Agent - Dashboard Application Logic
 */

// ── State Management ────────────────────────────────────
const state = {
    currentTab: 'dashboard',
    authToken: '',
    dashboardData: null,
    applications: [],
    jobs: [],
    urlPipeline: [],
    submissions: [],
    filters: {
        applications: 'draft',
        jobs: '', // empty means all
        jobsSearch: '',
        applicationsSearch: '',
        submissionsSearch: '',
        submissionsStatus: ''
    },
    autoRefreshEnabled: false,
    autoRefreshHandle: null,
    pendingGoTo: false
};

// ── DOM Elements ────────────────────────────────────────
const els = {
    tabs: document.querySelectorAll('.stat-item[data-tab]'),
    views: document.querySelectorAll('.view'),
    apiTokenInput: document.getElementById('api-secret'),
    btnRefresh: document.getElementById('btn-refresh'),
    btnShortcuts: document.getElementById('btn-shortcuts'),
    autoRefreshToggle: document.getElementById('auto-refresh-toggle'),
    shortcutsModal: document.getElementById('shortcuts-modal'),
    navPendingCount: document.getElementById('nav-pending-count'),
    
    // Dashboard Stats
    dashboardStats: document.getElementById('dashboard-stats'),
    
    // Applications
    applicationsList: document.getElementById('applications-list'),
    appFilters: document.querySelectorAll('#view-applications .filter-btn'),
    
    // Jobs
    jobsTableBody: document.getElementById('jobs-table-body'),
    jobFilters: document.querySelectorAll('#view-jobs .filter-btn'),
    urlPipelineList: document.getElementById('url-pipeline-list'),
    btnRefreshUrlPipeline: document.getElementById('btn-refresh-url-pipeline'),
    btnApproveVisibleDrafts: document.getElementById('btn-approve-visible-drafts'),
    btnOpenDraftLinks: document.getElementById('btn-open-draft-links'),
    jobsSearch: document.getElementById('jobs-search'),
    applicationsSearch: document.getElementById('applications-search'),
    submissionsSearch: document.getElementById('submissions-search'),
    submissionFilters: document.querySelectorAll('#view-submissions .filter-btn[data-submission-status]'),
    submissionsTableBody: document.getElementById('submissions-table-body'),
    applySessionModal: document.getElementById('apply-session-modal'),
    applySessionList: document.getElementById('apply-session-list'),
    
    // Modals
    reviewModal: document.getElementById('review-modal'),
    ingestModal: document.getElementById('ingest-modal'),
    btnIngestModal: document.getElementById('btn-ingest-modal'),
    btnIngestSubmit: document.getElementById('btn-submit-url'),
    ingestUrlInput: document.getElementById('ingest-url'),
    toastContainer: document.getElementById('toast-container'),
    
    // Review Modal Elements
    modalJobTitle: document.getElementById('modal-job-title'),
    modalCompany: document.getElementById('modal-company'),
    modalScore: document.getElementById('modal-score'),
    modalApplyUrl: document.getElementById('modal-apply-url'),
    modalRecruiterMsg: document.getElementById('modal-recruiter-msg'),
    modalCoverLetter: document.getElementById('modal-cover-letter'),
    modalQaList: document.getElementById('modal-qa-list'),
    btnApproveApp: document.getElementById('btn-approve-app'),
    btnRejectApp: document.getElementById('btn-reject-app')
};

// ── Initialization ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Initialize Lucide icons
    lucide.createIcons();
    
    // Load Token from storage
    const savedToken = localStorage.getItem('job_agent_token');
    if (savedToken) {
        state.authToken = savedToken;
        els.apiTokenInput.value = savedToken;
    }
    
    const autoRefresh = localStorage.getItem('job_agent_auto_refresh') === '1';
    state.autoRefreshEnabled = autoRefresh;
    if (els.autoRefreshToggle) els.autoRefreshToggle.checked = autoRefresh;

    setupEventListeners();
    setAutoRefresh(autoRefresh);
    refreshAllData();
});

function setAutoRefresh(enabled) {
    state.autoRefreshEnabled = Boolean(enabled);
    if (state.autoRefreshHandle) {
        clearInterval(state.autoRefreshHandle);
        state.autoRefreshHandle = null;
    }

    if (state.autoRefreshEnabled) {
        state.autoRefreshHandle = setInterval(() => {
            refreshAllData();
        }, 30000);
    }

    localStorage.setItem('job_agent_auto_refresh', state.autoRefreshEnabled ? '1' : '0');
}

function isTypingTarget(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || target.isContentEditable;
}

function focusCurrentSearch() {
    const map = {
        applications: els.applicationsSearch,
        jobs: els.jobsSearch,
        submissions: els.submissionsSearch,
    };
    const el = map[state.currentTab];
    if (el) {
        el.focus();
        el.select?.();
    }
}

function setupEventListeners() {
    // Tab switching
    els.tabs.forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });
    
    // Filters
    els.appFilters.forEach(btn => {
        btn.addEventListener('click', (e) => {
            els.appFilters.forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            state.filters.applications = e.target.dataset.status;
            renderApplications();
        });
    });
    
    els.jobFilters.forEach(btn => {
        btn.addEventListener('click', (e) => {
            els.jobFilters.forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            state.filters.jobs = e.target.dataset.status;
            fetchJobs(); // Need to fetch because filtering is done API side right now
        });
    });

    if (els.btnRefreshUrlPipeline) {
        els.btnRefreshUrlPipeline.addEventListener('click', () => fetchUrlPipeline());
    }

    if (els.btnApproveVisibleDrafts) {
        els.btnApproveVisibleDrafts.addEventListener('click', () => approveVisibleDrafts());
    }

    if (els.btnOpenDraftLinks) {
        els.btnOpenDraftLinks.addEventListener('click', () => openDraftApplyLinks());
    }

    if (els.jobsSearch) {
        els.jobsSearch.addEventListener('input', (e) => {
            state.filters.jobsSearch = (e.target.value || '').trim().toLowerCase();
            renderJobs();
        });
    }

    if (els.applicationsSearch) {
        els.applicationsSearch.addEventListener('input', (e) => {
            state.filters.applicationsSearch = (e.target.value || '').trim().toLowerCase();
            renderApplications();
        });
    }

    if (els.submissionsSearch) {
        els.submissionsSearch.addEventListener('input', (e) => {
            state.filters.submissionsSearch = (e.target.value || '').trim().toLowerCase();
            renderSubmissions();
        });
    }

    if (els.submissionFilters) {
        els.submissionFilters.forEach((btn) => {
            btn.addEventListener('click', (e) => {
                els.submissionFilters.forEach((b) => b.classList.remove('active'));
                e.currentTarget.classList.add('active');
                state.filters.submissionsStatus = e.currentTarget.dataset.submissionStatus || '';
                renderSubmissions();
            });
        });
    }
    
    // Auth token
    els.apiTokenInput.addEventListener('change', (e) => {
        state.authToken = e.target.value;
        localStorage.setItem('job_agent_token', state.authToken);
        refreshAllData();
    });
    
    // Refresh button
    els.btnRefresh.addEventListener('click', () => {
        els.btnRefresh.classList.add('spinning');
        refreshAllData().finally(() => {
            setTimeout(() => els.btnRefresh.classList.remove('spinning'), 500);
        });
    });

    if (els.btnShortcuts && els.shortcutsModal) {
        els.btnShortcuts.addEventListener('click', () => {
            els.shortcutsModal.classList.add('visible');
            lucide.createIcons();
        });
    }

    if (els.autoRefreshToggle) {
        els.autoRefreshToggle.addEventListener('change', (e) => {
            setAutoRefresh(Boolean(e.target.checked));
            showToast(`Auto refresh ${e.target.checked ? 'enabled' : 'disabled'}`, 'info');
        });
    }

    document.addEventListener('keydown', (e) => {
        if (isTypingTarget(e.target)) {
            if (e.key === 'Escape') e.target.blur();
            return;
        }

        const key = e.key.toLowerCase();
        if (key === '?') {
            e.preventDefault();
            els.shortcutsModal?.classList.toggle('visible');
            return;
        }

        if (key === '/') {
            e.preventDefault();
            focusCurrentSearch();
            return;
        }

        if (key === 'r') {
            e.preventDefault();
            els.btnRefresh.click();
            return;
        }

        if (state.pendingGoTo) {
            const tabMap = { d: 'dashboard', a: 'applications', j: 'jobs', s: 'submissions' };
            const nextTab = tabMap[key];
            state.pendingGoTo = false;
            if (nextTab) {
                e.preventDefault();
                switchTab(nextTab);
            }
            return;
        }

        if (key === 'g') {
            state.pendingGoTo = true;
            setTimeout(() => { state.pendingGoTo = false; }, 1500);
        }
    });
    
    // Modals
    document.querySelectorAll('.close-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.target.closest('.modal').classList.remove('visible');
        });
    });
    
    els.btnIngestModal.addEventListener('click', () => {
        els.ingestModal.classList.add('visible');
        els.ingestUrlInput.focus();
    });
    
    els.btnIngestSubmit.addEventListener('click', async () => {
        const url = els.ingestUrlInput.value.trim();
        if (!url) return;
        
        els.btnIngestSubmit.disabled = true;
        els.btnIngestSubmit.innerHTML = '<i data-lucide="loader" class="icon-sm"></i> Processing...';
        lucide.createIcons();
        
        try {
            const res = await apiCall('/api/ingest', 'POST', { url, sender: 'dashboard' });
            if (res) {
                showToast('URL queued for processing', 'success');
                els.ingestUrlInput.value = '';
                els.ingestModal.classList.remove('visible');
                setTimeout(refreshAllData, 2000); // Wait for worker
            }
        } finally {
            els.btnIngestSubmit.disabled = false;
            els.btnIngestSubmit.innerHTML = 'Process URL';
        }
    });
}

function switchTab(tabId) {
    state.currentTab = tabId;
    
    // Update nav
    els.tabs.forEach(t => t.classList.remove('active'));
    document.querySelector(`.stat-item[data-tab="${tabId}"]`).classList.add('active');
    
    // Update views
    els.views.forEach(v => v.classList.remove('active'));
    document.getElementById(`view-${tabId}`).classList.add('active');
    
    if (tabId === 'dashboard' && !state.dashboardData) fetchDashboard();
    if (tabId === 'applications' && state.applications.length === 0) fetchApplications();
    if (tabId === 'jobs' && state.jobs.length === 0) fetchJobs();
    if (tabId === 'submissions' && state.submissions.length === 0) fetchSubmissions();

    if (tabId === 'applications' && els.applicationsSearch) els.applicationsSearch.value = state.filters.applicationsSearch || '';
    if (tabId === 'jobs' && els.jobsSearch) els.jobsSearch.value = state.filters.jobsSearch || '';
    if (tabId === 'submissions' && els.submissionsSearch) els.submissionsSearch.value = state.filters.submissionsSearch || '';
}

// ── API Layer ───────────────────────────────────────────
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
            throw new Error(err.detail || `HTTP Error ${res.status}`);
        }
        
        return await res.json();
    } catch (err) {
        showToast(err.message, 'error');
        console.error('API Error:', err);
        return null;
    }
}

async function refreshAllData() {
    await fetchDashboard();
    await fetchUrlPipeline();
    if (state.currentTab === 'applications') await fetchApplications();
    if (state.currentTab === 'jobs') await fetchJobs();
    if (state.currentTab === 'submissions') await fetchSubmissions();
}

async function fetchDashboard() {
    const data = await apiCall('/api/dashboard');
    if (!data) return;
    state.dashboardData = data;
    renderDashboard();
    
    // Update nav badge
    els.navPendingCount.textContent = data.applications_pending;
    els.navPendingCount.style.display = data.applications_pending > 0 ? 'inline-block' : 'none';
}

async function fetchApplications() {
    const data = await apiCall(`/api/applications`);
    if (!data) return;
    state.applications = data;
    renderApplications();
}

async function fetchJobs() {
    const status = state.filters.jobs;
    const params = new URLSearchParams();
    params.set('limit', '100');
    params.set('sort_by', 'score');
    params.set('sort_order', 'desc');
    params.set('has_application', 'true');
    if (status) params.set('status', status);
    const search = (state.filters.jobsSearch || '').trim().toLowerCase();
    const platforms = ['greenhouse','lever','linkedin','indeed','workday'];
    if (platforms.includes(search)) params.set('platform', search);
    const url = `/api/jobs?${params.toString()}`;
    const data = await apiCall(url);
    if (!data) return;
    state.jobs = data;
    renderJobs();
}

async function fetchUrlPipeline() {
    const data = await apiCall('/api/urls?limit=12');
    if (!data || !data.items) return;
    state.urlPipeline = data.items;
    renderUrlPipeline();
}

async function fetchSubmissions() {
    const data = await apiCall('/api/submissions');
    if (!data) return;
    state.submissions = data;
    renderSubmissions();
}

// ── Rendering ───────────────────────────────────────────
function renderDashboard() {
    if (!state.dashboardData) return;
    const d = state.dashboardData;
    
    els.dashboardStats.innerHTML = `
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="inbox"></i> Ingestion</div>
            <div class="stat-value">${d.total_urls}</div>
            <div class="stat-sub">URLs extracted from ${d.total_messages} messages</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="briefcase"></i> Discovery</div>
            <div class="stat-value">${d.total_jobs}</div>
            <div class="stat-sub">Valid job postings found</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="file-check-2"></i> Drafts Pending</div>
            <div class="stat-value text-warning">${d.applications_pending}</div>
            <div class="stat-sub">Awaiting human review</div>
        </div>
        <div class="stat-card">
            <div class="stat-header"><i data-lucide="send"></i> Submissions</div>
            <div class="stat-value text-success">${d.submissions_success}</div>
            <div class="stat-sub">Out of ${d.submissions_total} attempts</div>
        </div>
    `;
    lucide.createIcons();
}

function renderApplications() {
    const query = state.filters.applicationsSearch || '';
    const filtered = state.applications.filter((a) => {
        if (a.status !== state.filters.applications) return false;
        if (!query) return true;
        const haystack = `${a.job_title} ${a.job_company} ${a.cover_letter}`.toLowerCase();
        return haystack.includes(query);
    });

    if (filtered.length === 0) {
        els.applicationsList.innerHTML = `
            <div class="col-span-full py-8 text-center text-dim w-full" style="grid-column: 1/-1;">
                No applications found for current filters.
            </div>
        `;
        return;
    }
    
    els.applicationsList.innerHTML = filtered.map(app => `
        <div class="app-card">
            <div>
                <div class="app-meta mb-1">
                    <span class="status ${app.status}">${app.status.replace('_', ' ')}</span>
                    <span class="text-sm">${new Date(app.created_at).toLocaleDateString()}</span>
                </div>
                <h3 class="app-title" title="${app.job_title}">${app.job_title}</h3>
                <div class="text-dim mb-1">${app.job_company}</div>
                
                <div class="app-score mb-1">
                    <i data-lucide="target" class="icon-sm"></i> Score: ${app.job_score}/100 
                </div>
                
                <div class="app-excerpt">${app.cover_letter}</div>
            </div>
            
            <div class="app-actions-row mt-auto pt-4" style="border-top: 1px solid var(--border-light)">
                ${app.status === 'draft' ? `
                    <button class="btn btn-primary" onclick="quickApprove(${app.id})">Approve & Submit</button>
                    <button class="btn btn-secondary" onclick="openReviewModal(${app.id})">Review</button>
                ` : `<button class="btn btn-secondary" onclick="openReviewModal(${app.id})">View Details</button>`}
                <button class="btn btn-glass" onclick="generateInterviewPrep(${app.id})">Interview Prep</button>
                ${app.apply_url ? `<a class="btn btn-glass" href="${app.apply_url}" target="_blank" rel="noopener">Open Apply Page</a>` : ''}
            </div>
        </div>
    `).join('');
    
    lucide.createIcons();
}

function renderJobs() {
    const query = state.filters.jobsSearch || '';
    const filteredJobs = state.jobs.filter((job) => {
        if (!query) return true;
        const haystack = `${job.title} ${job.company} ${job.location}`.toLowerCase();
        return haystack.includes(query);
    });

    if (filteredJobs.length === 0) {
        els.jobsTableBody.innerHTML = `<tr><td colspan="6" class="text-center text-dim py-4">No jobs found.</td></tr>`;
        return;
    }

    els.jobsTableBody.innerHTML = filteredJobs.map(job => `
        <tr>
            <td class="font-medium">
                <a href="${job.apply_url || job.source_url}" target="_blank" class="text-main" style="text-decoration:none">
                    ${job.title} <i data-lucide="external-link" style="width:12px; height:12px; margin-left:4px;" class="text-dim"></i>
                </a>
            </td>
            <td>${job.company}</td>
            <td><span class="status scored">${job.score !== null ? job.score : '-'}</span></td>
            <td><span class="status ${job.status}">${job.status.replace('_', ' ')}</span></td>
            <td class="text-dim">${new Date(job.created_at).toLocaleDateString()}</td>
            <td>
                <div class="card-actions">
                    <button class="btn btn-primary btn-sm" onclick="applyNowForJob(${job.id})">Apply Now</button>
                    <a class="btn btn-glass btn-sm" href="${job.apply_url || job.source_url}" target="_blank" rel="noopener">Open</a>
                </div>
            </td>
        </tr>
    `).join('');

    lucide.createIcons();
}

function renderUrlPipeline() {
    if (!els.urlPipelineList) return;

    if (!state.urlPipeline.length) {
        els.urlPipelineList.innerHTML = `<div class="text-dim">No URL pipeline items yet. Add a job URL to start.</div>`;
        return;
    }

    els.urlPipelineList.innerHTML = state.urlPipeline.map((item) => `
        <div class="quick-url-card">
            <h4>${item.normalized_url}</h4>
            <div class="quick-url-meta">
                <span class="status ${item.status.toLowerCase()}">${item.status}</span>
                <span class="status scored">Jobs ${item.jobs_found}</span>
                <span class="status draft">Drafts ${item.applications_ready}</span>
                <span class="status approved">Auto ${item.auto_apply_candidates}</span>
            </div>
            <div class="card-actions">
                <button class="btn btn-primary btn-sm" onclick="triggerUrlAutoApply(${item.url_id})">Auto-Apply URL</button>
                ${item.requires_auth ? `<button class="btn btn-warning btn-sm" onclick="resolveAuthPrompt(${item.url_id})">Resolve Auth</button>` : ''}
                <a class="btn btn-glass btn-sm" href="${item.normalized_url}" target="_blank" rel="noopener">Open</a>
            </div>
        </div>
    `).join('');
}

function renderSubmissions() {
    if (!els.submissionsTableBody) return;

    const query = state.filters.submissionsSearch || '';
    const statusFilter = state.filters.submissionsStatus || '';

    const filtered = state.submissions.filter((s) => {
        if (statusFilter && s.status !== statusFilter) return false;
        if (!query) return true;
        const haystack = `${s.job_title || ''} ${s.submitter_name || ''} ${s.error_message || ''}`.toLowerCase();
        return haystack.includes(query);
    });

    if (!filtered.length) {
        els.submissionsTableBody.innerHTML = `<tr><td colspan="6" class="text-center text-dim py-4">No submissions found for current filters.</td></tr>`;
        return;
    }

    els.submissionsTableBody.innerHTML = filtered.map((s) => `
        <tr>
            <td>${s.job_title || '-'}</td>
            <td>${s.submitter_name || '-'}</td>
            <td><span class="status ${s.status}">${(s.status || '-').replaceAll('_', ' ')}</span></td>
            <td class="text-dim" title="${s.error_message || ''}">${s.error_message || '-'}</td>
            <td class="text-dim">${s.created_at ? new Date(s.created_at).toLocaleString() : '-'}</td>
            <td>
                ${s.application_id ? `<button class="btn btn-secondary btn-sm" onclick="retrySubmission(${s.application_id})">Retry</button>` : ''}
                ${s.confirmation_url ? `<a class="btn btn-glass btn-sm" href="${s.confirmation_url}" target="_blank" rel="noopener">Open</a>` : ''}
            </td>
        </tr>
    `).join('');
}

// ── Interactions ────────────────────────────────────────
window.openReviewModal = (appId) => {
    const app = state.applications.find(a => a.id === appId);
    if (!app) return;
    
    els.modalJobTitle.textContent = app.job_title;
    els.modalCompany.textContent = app.job_company;
    els.modalScore.textContent = app.job_score;
    els.modalApplyUrl.href = app.apply_url || '#';
    
    els.modalCoverLetter.value = app.cover_letter || '';
    els.modalRecruiterMsg.textContent = app.recruiter_message || 'N/A';
    
    // Q&A mapping
    let qaHtml = '';
    if (app.qa_answers && Object.keys(app.qa_answers).length > 0) {
        for (const [key, val] of Object.entries(app.qa_answers)) {
            const prettyKey = key.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
            qaHtml += `<div class="qa-item">
                <div class="qa-q">${prettyKey}</div>
                <div class="qa-a">${val}</div>
            </div>`;
        }
    } else {
        qaHtml = '<div class="text-dim text-sm">No Q&A generated</div>';
    }
    els.modalQaList.innerHTML = qaHtml;
    
    // Setup actions
    if (app.status === 'draft') {
        els.btnApproveApp.style.display = 'inline-flex';
        els.btnRejectApp.style.display = 'inline-flex';
        els.modalCoverLetter.readOnly = false;

        els.btnApproveApp.onclick = () => handleApprove(app.id);
        els.btnRejectApp.onclick = () => handleReject(app.id);
    } else {
        els.btnApproveApp.style.display = 'none';
        els.btnRejectApp.style.display = 'none';
        els.modalCoverLetter.readOnly = true;
    }
    
    els.reviewModal.classList.add('visible');
    lucide.createIcons();
};

window.copyCoverLetter = () => {
    els.modalCoverLetter.select();
    document.execCommand('copy');
    showToast('Cover letter copied to clipboard', 'success');
};

async function handleApprove(appId) {
    const btn = els.btnApproveApp;
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader" class="icon-sm spinning"></i> Approving...';
    lucide.createIcons();
    
    const res = await apiCall(`/api/applications/${appId}/approve`, 'POST');
    if (res) {
        showToast('Application approved and queued for submission', 'success');
        els.reviewModal.classList.remove('visible');
        refreshAllData();
    }
    
    btn.disabled = false;
    btn.innerHTML = 'Approve Application';
}

async function handleReject(appId) {
    const res = await apiCall(`/api/applications/${appId}/reject?reason=Skipped from dashboard`, 'POST');
    if (res) {
        showToast('Application skipped', 'info');
        els.reviewModal.classList.remove('visible');
        refreshAllData();
    }
}

window.generateInterviewPrep = async (appId) => {
    const res = await apiCall(`/api/applications/${appId}/interview-prep`, 'POST');
    if (!res || !res.prep) return;

    const app = state.applications.find((a) => a.id === appId);
    const title = app ? `${app.job_title} - Interview Prep` : `application-${appId}-interview-prep`;
    const payload = `${title}

Generated: ${res.generated_at}

${res.prep}`;

    const blob = new Blob([payload], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${title.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.txt`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    showToast('Interview prep generated and downloaded', 'success');
};

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    const iconMap = {
        'success': 'check-circle-2',
        'error': 'alert-circle',
        'info': 'info'
    };
    
    toast.innerHTML = `<i data-lucide="${iconMap[type]}"></i> <span>${message}</span>`;
    els.toastContainer.appendChild(toast);
    lucide.createIcons();
    
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(50px)';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

window.quickApprove = async (appId) => {
    const res = await apiCall(`/api/applications/${appId}/approve`, 'POST');
    if (res) {
        showToast('Application approved and queued', 'success');
        refreshAllData();
    }
};

async function approveVisibleDrafts() {
    if (state.applications.length === 0) {
        await fetchApplications();
    }

    const drafts = state.applications.filter((a) => a.status === 'draft');
    if (!drafts.length) {
        showToast('No draft applications to approve', 'info');
        return;
    }

    let approved = 0;
    for (const app of drafts) {
        const res = await apiCall(`/api/applications/${app.id}/approve`, 'POST');
        if (res) approved += 1;
    }

    showToast(`Approved ${approved}/${drafts.length} drafts`, approved ? 'success' : 'info');
    refreshAllData();
}

function openDraftApplyLinks() {
    const drafts = state.applications.filter((a) => a.status === 'draft' && a.apply_url);
    if (!drafts.length) {
        showToast('No draft apply links available', 'info');
        return;
    }

    els.applySessionList.innerHTML = drafts.slice(0, 20).map((app) => `
        <div class="qa-item">
            <div class="qa-q">${app.job_title} — ${app.job_company}</div>
            <div class="card-actions mt-1">
                <a class="btn btn-glass btn-sm" href="${app.apply_url}" target="_blank" rel="noopener">Open Link</a>
                <button class="btn btn-secondary btn-sm" onclick="copyText('${app.apply_url.replace(/'/g, "\'")}')">Copy Link</button>
            </div>
        </div>
    `).join('');

    els.applySessionModal.classList.add('visible');
    showToast(`Prepared ${Math.min(drafts.length, 20)} draft links`, 'success');
}

window.triggerUrlAutoApply = async (urlId) => {
    const res = await apiCall(`/api/urls/${urlId}/auto-apply`, 'POST');
    if (!res) return;
    showToast(`URL auto-apply queued: ${res.queued_submission_count}`, 'success');
    refreshAllData();
};

window.resolveAuthPrompt = async (urlId) => {
    const authenticatedUrl = window.prompt('Paste authenticated URL (same host/path scope):');
    if (!authenticatedUrl) return;

    const res = await apiCall(`/api/urls/${urlId}/resolve-auth`, 'POST', { authenticated_url: authenticatedUrl });
    if (!res) return;
    showToast('Authenticated URL updated and re-queued', 'success');
    refreshAllData();
};


window.applyNowForJob = async (jobId) => {
    const res = await apiCall(`/api/jobs/${jobId}/apply-now`, 'POST');
    if (!res) return;
    showToast('Job approved and queued for submission', 'success');
    refreshAllData();
};


window.retrySubmission = async (appId) => {
    const res = await apiCall(`/api/applications/${appId}/retry-submit`, 'POST');
    if (!res) return;
    showToast('Retry queued', 'success');
    fetchSubmissions();
};

window.copyText = (value) => {
    navigator.clipboard.writeText(value);
    showToast('Copied link', 'success');
};
