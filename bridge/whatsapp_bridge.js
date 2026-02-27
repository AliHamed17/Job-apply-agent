/**
 * WhatsApp Personal Bridge — Job Agent
 *
 * Connects to your personal WhatsApp via WhatsApp Web, monitors the groups
 * you configure, extracts job URLs, and forwards them to the Job Agent API
 * for automatic processing (extraction → scoring → cover letter → dashboard).
 *
 * How it works:
 *   1. Run this script once — a QR code appears in the terminal
 *   2. Open WhatsApp on your phone → Linked Devices → Link a Device
 *   3. Scan the QR code — session is saved locally (no re-scan needed)
 *   4. The bridge runs in the background and watches your groups
 *   5. Any message containing a URL in a watched group is forwarded
 *
 * Configuration (bridge/.env):
 *   JOB_AGENT_URL       — URL of your running job-agent API
 *   JOB_AGENT_TOKEN     — API secret (matches SECRET_KEY in main .env)
 *   WATCH_ALL_GROUPS    — "true" to watch every group, "false" to filter
 *   GROUP_KEYWORDS      — comma-separated keywords; only groups whose name
 *                         contains one of these are watched (e.g. "jobs,hiring,careers")
 *   JOB_URL_ONLY        — "true" to only forward likely job-board URLs
 *   SESSION_DIR         — where to store the WhatsApp session (default: ./.wwebjs_auth)
 *   LOG_LEVEL           — "info" | "verbose" | "silent"
 */

'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fetch = require('node-fetch');
const path = require('path');
const fs = require('fs');

// ── Load config ──────────────────────────────────────────────────────────────
require('dotenv').config({ path: path.join(__dirname, '.env') });

const CONFIG = {
  agentUrl: (process.env.JOB_AGENT_URL || 'http://localhost:8000').replace(/\/$/, ''),
  agentToken: process.env.JOB_AGENT_TOKEN || '',
  watchAllGroups: process.env.WATCH_ALL_GROUPS !== 'false',   // default: watch all
  watchArchivedOnly: process.env.WATCH_ARCHIVED_ONLY === 'true', // take only archived groups
  groupKeywords: (process.env.GROUP_KEYWORDS || 'jobs,hiring,careers,work,job,tech,remote,vacancy,recruitment')
    .split(',').map(s => s.trim().toLowerCase()).filter(Boolean),
  jobUrlOnly: process.env.JOB_URL_ONLY !== 'false',   // default: job URLs only
  sessionDir: process.env.SESSION_DIR || path.join(__dirname, '.wwebjs_auth'),
  logLevel: process.env.LOG_LEVEL || 'info',     // info | verbose | silent
};

// ── Known job board hostnames (mirrors ingestion/url_utils.py) ───────────────
const JOB_HOSTS = [
  'greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'workday.com',
  'linkedin.com', 'indeed.com', 'glassdoor.com', 'ziprecruiter.com',
  'angel.co', 'wellfound.com', 'otta.com', 'remote.co', 'weworkremotely.com',
  'jobvite.com', 'icims.com', 'smartrecruiters.com', 'ashbyhq.com',
  'workable.com', 'recruitee.com', 'teamtailor.com', 'bamboohr.com',
  'dover.com', 'amazon.jobs', 'careers.google.com', 'careers.microsoft.com',
  'jobs.apple.com', 'efinancialcareers.com', 'totaljobs.com', 'reed.co.uk',
  'cwjobs.co.uk', 'jobsite.co.uk', 'monster.co.uk', 'cityjobs.com',
  'comeet.com', 'comeet.co', 'rippling.com', 'dover.io',
];

const SHORT_HOSTS = [
  'bit.ly', 't.co', 'goo.gl', 'tinyurl.com', 'ow.ly', 'lnkd.in',
  'rb.gy', 'cutt.ly', 'buff.ly', 'tiny.cc', 'is.gd', 's.id',
];

// ── URL extraction ────────────────────────────────────────────────────────────
const URL_RE = /https?:\/\/[^\s<>"')\]},;|\u200b\u200c\u200d\ufeff]+/gi;
const TRAIL = /[.,;:!?)\]]+$/;

function extractUrls(text) {
  if (!text) return [];
  // Strip WhatsApp bold/italic/code markers
  const clean = text.replace(/[*_~`]/g, ' ');
  const raw = clean.match(URL_RE) || [];
  const seen = new Set();
  return raw
    .map(u => u.replace(TRAIL, ''))
    .filter(u => u && !seen.has(u) && seen.add(u));
}

function isJobUrl(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    if (JOB_HOSTS.some(h => host.includes(h))) return true;
    if (/\/(jobs?|careers?|apply|job-openings?)\//i.test(url)) return true;
    return false;
  } catch { return false; }
}

function isShortUrl(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    return SHORT_HOSTS.includes(host);
  } catch { return false; }
}

// ── Logging ───────────────────────────────────────────────────────────────────
function log(level, ...args) {
  if (CONFIG.logLevel === 'silent') return;
  if (level === 'verbose' && CONFIG.logLevel !== 'verbose') return;
  const ts = new Date().toISOString().slice(11, 19);
  const prefix = { info: '✓', verbose: '·', warn: '⚠', error: '✗' }[level] || '?';
  console.log(`[${ts}] ${prefix}`, ...args);
}

// ── Group filter ──────────────────────────────────────────────────────────────
function shouldWatchGroup(chat) {
  if (CONFIG.watchArchivedOnly && !chat.archived) return false;
  if (CONFIG.watchAllGroups) return true;
  const lower = (chat.name || '').toLowerCase();
  return CONFIG.groupKeywords.some(kw => lower.includes(kw));
}

// ── Forward URL to Job Agent ──────────────────────────────────────────────────
async function forwardUrl(url, senderPhone, groupName) {
  const endpoint = `${CONFIG.agentUrl}/api/ingest`;
  const headers = { 'Content-Type': 'application/json' };
  if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        url,
        sender: senderPhone || 'whatsapp-bridge',
        source: `group:${groupName}`,
      }),
      timeout: 10000,
    });

    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      log('info', `Forwarded → ${url.slice(0, 70)}`);
      if (data.added > 0) log('verbose', `  ↳ added: ${data.added}`);
      if (data.skipped > 0) log('verbose', `  ↳ duplicate, skipped`);
      return true;
    } else {
      log('warn', `Agent rejected ${url} — HTTP ${res.status}`);
      return false;
    }
  } catch (err) {
    log('error', `Failed to forward ${url}: ${err.message}`);
    return false;
  }
}

// ── Heartbeat — lets the dashboard know the bridge is alive ──────────────────
let _heartbeatTimer = null;
let _watchedGroupCount = 0;

async function sendHeartbeat() {
  if (!CONFIG.agentUrl) return;
  const headers = { 'Content-Type': 'application/json' };
  if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;
  try {
    await fetch(`${CONFIG.agentUrl}/api/bridge/heartbeat`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ id: 'whatsapp-web', groups_watched: _watchedGroupCount }),
      timeout: 5000,
    });
    log('verbose', `Heartbeat sent (groups: ${_watchedGroupCount})`);
  } catch (err) {
    log('verbose', `Heartbeat failed: ${err.message}`);
  }
}

function startHeartbeat() {
  if (_heartbeatTimer) clearInterval(_heartbeatTimer);
  sendHeartbeat();  // send immediately on connect
  _heartbeatTimer = setInterval(sendHeartbeat, 60_000);  // then every 60 s
}

function stopHeartbeat() {
  if (_heartbeatTimer) { clearInterval(_heartbeatTimer); _heartbeatTimer = null; }
}

// ── Deduplification (in-memory, resets on restart) ────────────────────────────
const seenUrls = new Set();

function markSeen(url) { seenUrls.add(url); }
function alreadySeen(url) { return seenUrls.has(url); }

// ── WhatsApp client ───────────────────────────────────────────────────────────
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: CONFIG.sessionDir }),
  puppeteer: {
    headless: true,
    protocolTimeout: 60000,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
    ],
  },
  webVersionCache: {
    type: 'remote',
    remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.2412.54.html',
  },
});

// QR code — shown once on first run
client.on('qr', qr => {
  console.log('\n════════════════════════════════════════════');
  console.log('  Scan this QR code with your WhatsApp:');
  console.log('  Phone → Linked Devices → Link a Device');
  console.log('════════════════════════════════════════════\n');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => log('info', 'WhatsApp session authenticated'));
client.on('ready', async () => {
  log('info', '─────────────────────────────────────────────');
  log('info', 'WhatsApp bridge is READY');
  log('info', `  Agent URL   : ${CONFIG.agentUrl}`);
  log('info', `  Watch all   : ${CONFIG.watchAllGroups}`);
  log('info', `  Archived only: ${CONFIG.watchArchivedOnly}`);
  if (!CONFIG.watchAllGroups) {
    log('info', `  Keywords    : ${CONFIG.groupKeywords.join(', ')}`);
  }
  log('info', `  Job URLs only: ${CONFIG.jobUrlOnly}`);
  log('info', '─────────────────────────────────────────────');

  // List currently-watched groups on startup
  try {
    const chats = await client.getChats();
    const groups = chats.filter(c => c.isGroup);
    const watched = groups.filter(g => shouldWatchGroup(g));
    _watchedGroupCount = watched.length;
    log('info', `Monitoring ${watched.length} / ${groups.length} groups:`);
    for (const g of watched) {
      log('info', `  • ${g.name}`);
      // Process last 5 messages from each group on startup
      const messages = await g.fetchMessages({ limit: 5 });
      for (const m of messages) {
        await processMessage(m);
      }
    }
  } catch (err) {
    log('warn', `Could not list groups: ${err.message}`);
  }

  startHeartbeat();
});

client.on('disconnected', reason => {
  log('warn', `Disconnected: ${reason}`);
  stopHeartbeat();
  // Attempt reconnect after 10 seconds
  setTimeout(() => {
    log('info', 'Attempting to reconnect…');
    client.initialize().catch(e => log('error', `Reconnect failed: ${e.message}`));
  }, 10_000);
});

// ── Main message handler ──────────────────────────────────────────────────────
async function processMessage(msg) {
  try {
    // Only process group messages
    const chat = await msg.getChat();
    if (!chat.isGroup) return;

    // Check if this group is watched
    if (!shouldWatchGroup(chat)) {
      return;
    }

    const body = msg.body || '';

    // Also check quoted/forwarded message body
    const quotedBody = msg.hasQuotedMsg
      ? (await msg.getQuotedMessage().catch(() => null))?.body || ''
      : '';

    const fullText = `${body}\n${quotedBody}`.trim();
    const urls = extractUrls(fullText);

    if (!urls.length) return;

    const contact = await msg.getContact();
    const sender = contact.number || msg.from;

    for (const url of urls) {
      // Filter: only forward job-board URLs if jobUrlOnly is set
      if (CONFIG.jobUrlOnly && !isJobUrl(url) && !isShortUrl(url)) {
        continue;
      }

      if (alreadySeen(url)) {
        continue;
      }

      markSeen(url);
      log('info', `[${chat.name}] ${sender} → ${url.slice(0, 70)}`);
      await forwardUrl(url, sender, chat.name);
    }

  } catch (err) {
    log('error', `Message handler error: ${err.message}`);
  }
}

client.on('message', processMessage);

// ── Graceful shutdown ─────────────────────────────────────────────────────────
process.on('SIGINT', () => { log('info', 'Shutting down…'); stopHeartbeat(); client.destroy().then(() => process.exit(0)); });
process.on('SIGTERM', () => { log('info', 'Shutting down…'); stopHeartbeat(); client.destroy().then(() => process.exit(0)); });

// ── Start ─────────────────────────────────────────────────────────────────────
log('info', 'Starting WhatsApp bridge…');
client.initialize().catch(err => {
  log('error', `Failed to start: ${err.message}`);
  process.exit(1);
});
