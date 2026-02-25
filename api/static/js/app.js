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
    filters: {
        applications: 'draft',
        jobs: '' // empty means all
    }
};

// ── DOM Elements ────────────────────────────────────────
const els = {
    tabs: document.querySelectorAll('.stat-item[data-tab]'),
    views: document.querySelectorAll('.view'),
    apiTokenInput: document.getElementById('api-secret'),
    btnRefresh: document.getElementById('btn-refresh'),
    navPendingCount: document.getElementById('nav-pending-count'),
    
    // Dashboard Stats
    dashboardStats: document.getElementById('dashboard-stats'),
    
    // Applications
    applicationsList: document.getElementById('applications-list'),
    appFilters: document.querySelectorAll('#view-applications .filter-btn'),
    
    // Jobs
    jobsTableBody: document.getElementById('jobs-table-body'),
    jobFilters: document.querySelectorAll('#view-jobs .filter-btn'),
    
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
    
    setupEventListeners();
    refreshAllData();
});

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
    
    // Auth token
    els.apiTokenInput.addEventListener('change', (e) => {
        state.authToken = e.target.value;
        localStorage.setItem('job_agent_token', state.authToken);
        refreshAllData();
    });
    
    // Refresh button
    els.btnRefresh.addEventListener('click', () => {
        const icon = els.btnRefresh.querySelector('i');
        els.btnRefresh.classList.add('spinning');
        refreshAllData().finally(() => {
            setTimeout(() => els.btnRefresh.classList.remove('spinning'), 500);
        });
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
    if (state.currentTab === 'applications') await fetchApplications();
    if (state.currentTab === 'jobs') await fetchJobs();
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
    const url = status ? `/api/jobs?status=${status}&limit=100` : '/api/jobs?limit=100';
    const data = await apiCall(url);
    if (!data) return;
    state.jobs = data;
    renderJobs();
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
    const filtered = state.applications.filter(a => a.status === state.filters.applications);
    
    if (filtered.length === 0) {
        els.applicationsList.innerHTML = `
            <div class="col-span-full py-8 text-center text-dim w-full" style="grid-column: 1/-1;">
                No applications found with status '${state.filters.applications}'.
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
            
            <div class="flex-between mt-auto pt-4" style="border-top: 1px solid var(--border-light)">
                ${app.status === 'draft' ? 
                    `<button class="btn btn-primary full-width" onclick="openReviewModal(${app.id})">Review &amp; Approve</button>` : 
                    `<button class="btn btn-secondary full-width" onclick="openReviewModal(${app.id})">View Details</button>`
                }
            </div>
        </div>
    `).join('');
    
    lucide.createIcons();
}

function renderJobs() {
    if (state.jobs.length === 0) {
        els.jobsTableBody.innerHTML = `<tr><td colspan="5" class="text-center text-dim py-4">No jobs found.</td></tr>`;
        return;
    }
    
    els.jobsTableBody.innerHTML = state.jobs.map(job => `
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
        </tr>
    `).join('');
    
    lucide.createIcons();
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
