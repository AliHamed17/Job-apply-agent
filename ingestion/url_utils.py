"""URL normalization, expansion, and hashing utilities.

Enhanced for WhatsApp message handling:
- Strips WhatsApp text formatting (bold, italic, code markers)
- Handles URL-encoded links
- Detects job board URLs from 20+ platforms
- Expands 30+ URL shortener domains
- Preserves ATS-important parameters (e.g., gh_jid)
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ── Tracking parameters to strip ───────────────────────────────────────────
# Generic analytics params — safe to remove from any URL
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
    "igshid", "igsh",
    # LinkedIn tracking
    "trk", "trkEmail",
    # WhatsApp share tracking
    "wt_mc_o", "wt_mc_n", "wt_mc_t", "wa_source", "wa_campaign",
    # Generic referrer param (safe to strip for job boards)
    "si",
}

# Params that look like tracking but contain meaningful ATS data — never strip
ATS_PRESERVE_PARAMS = {
    "gh_jid",        # Greenhouse job ID
    "gh_src",        # Greenhouse source
    "lever-origin",
    "j",             # Jobvite job ID
    "jobId",         # Various ATS
}

# ── URL shortener domains ───────────────────────────────────────────────────
SHORT_URL_DOMAINS = {
    # Global shorteners
    "bit.ly", "bitly.com", "b.link",
    "t.co",
    "goo.gl",
    "tinyurl.com",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "tiny.cc",
    "rb.gy",
    "cutt.ly",
    "shorturl.at",
    "short.link",
    "s.id",
    "v.gd",
    "clck.ru",
    "x.co",
    "snip.ly",
    "zpr.io",
    "mcaf.ee",
    # LinkedIn
    "lnkd.in", "linkedin.com/slink",
    # WhatsApp / Meta
    "wa.me",
    "fb.me",
    # Job-specific shorteners
    "jobs.ly",
    "jbist.com",
    "career.pm",
    # Branded shorteners
    "smarturl.it",
    "urlr.me",
}

# ── Job board patterns ──────────────────────────────────────────────────────
JOB_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pat, re.IGNORECASE)
    for pat in [
        r"greenhouse\.io",
        r"lever\.co",
        r"myworkday(?:jobs)?\.com",
        r"workday\.com",
        r"linkedin\.com/jobs",
        r"linkedin\.com/job",
        r"indeed\.com",
        r"glassdoor\.com/job",
        r"ziprecruiter\.com",
        r"angel\.co/company",
        r"wellfound\.com",
        r"otta\.com/jobs",
        r"remote\.co/job",
        r"weworkremotely\.com/remote-jobs",
        r"jobvite\.com",
        r"icims\.com",
        r"smartrecruiters\.com",
        r"ashbyhq\.com",
        r"rippling\.com/careers",
        r"bamboohr\.com/careers",
        r"workable\.com",
        r"recruitee\.com",
        r"teamtailor\.com",
        r"dover\.com/apply",
        r"careers\.microsoft\.com",
        r"amazon\.jobs",
        r"jobs\.apple\.com",
        r"careers\.google\.com",
        # Path-based heuristics (lower confidence)
        r"/jobs/\d",
        r"/careers/\d",
        r"/job-openings/",
        r"/job/[^/]+$",
        r"/career/[^/]+$",
        r"/apply/",
        r"boards\.[^/]+/jobs",
    ]
]


def is_likely_job_url(url: str) -> bool:
    """Return True if the URL looks like a job posting or job board link."""
    for pattern in JOB_URL_PATTERNS:
        if pattern.search(url):
            return True
    return False


# ── WhatsApp text formatting strippers ─────────────────────────────────────
# WhatsApp uses markdown-like formatting that can wrap URLs
_WA_FORMAT_RE = re.compile(r"[*_~`]")

# ── Core URL regex ─────────────────────────────────────────────────────────
_URL_RE = re.compile(
    # Zero-width chars (\u200b etc.) are included in the exclusion set
    r"https?://[^\s<>\"')\]},;|\u200b\u200c\u200d\ufeff]+",
    re.IGNORECASE,
)

# Characters to strip from the end of extracted URLs
_TRAIL_JUNK = ".,;:!?)]"


def _clean_url(raw: str) -> str:
    """Strip trailing junk and zero-width chars from a URL."""
    return raw.rstrip(_TRAIL_JUNK).rstrip("\u200b\u200c\u200d")


def _decode_if_encoded(url: str) -> str:
    """Percent-decode a URL if the scheme itself was encoded."""
    lower = url.lower()
    if lower.startswith("https%3a") or lower.startswith("http%3a"):
        url = unquote(url)
    return url


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from a text string.

    Handles:
    - WhatsApp bold/italic/code formatting around URLs
    - URL-encoded URLs (https%3A%2F%2F...)
    - Zero-width characters injected by WhatsApp
    - Trailing punctuation
    - Deduplication (order-preserving)
    """
    if not text:
        return []

    # Strip WhatsApp formatting characters that may wrap a URL
    cleaned_text = _WA_FORMAT_RE.sub(" ", text)

    seen: set[str] = set()
    result: list[str] = []
    for raw in _URL_RE.findall(cleaned_text):
        u = _decode_if_encoded(_clean_url(raw))
        if u and u not in seen:
            seen.add(u)
            result.append(u)

    return result


def extract_urls_from_whatsapp_message(msg: dict) -> list[str]:
    """Extract URLs from any WhatsApp Cloud API message object.

    Covers all message types:
    - text.body                        — plain text
    - image/video/document.caption     — media captions
    - button.text / button.payload     — quick-reply buttons
    - interactive.body / footer        — interactive messages
    - interactive.list_reply.title     — list reply
    - interactive.button_reply.title   — button reply
    - context fallback                 — forwarded message text
    """
    texts: list[str] = []

    msg_type = msg.get("type", "text")

    if msg_type == "text":
        texts.append(msg.get("text", {}).get("body", ""))

    elif msg_type in ("image", "video", "audio", "document", "sticker"):
        texts.append(msg.get(msg_type, {}).get("caption", ""))

    elif msg_type == "button":
        btn = msg.get("button", {})
        texts.append(btn.get("text", ""))
        texts.append(btn.get("payload", ""))

    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        inter_type = interactive.get("type", "")
        if inter_type == "list_reply":
            reply = interactive.get("list_reply", {})
            texts.append(reply.get("title", ""))
            texts.append(reply.get("description", ""))
        elif inter_type == "button_reply":
            reply = interactive.get("button_reply", {})
            texts.append(reply.get("title", ""))
        texts.append(interactive.get("body", {}).get("text", ""))
        texts.append(interactive.get("footer", {}).get("text", ""))

    else:
        # Fallback: try common text-bearing fields
        for field in ("text", "caption", "body", "title", "description"):
            val = msg.get(field)
            if isinstance(val, str):
                texts.append(val)
            elif isinstance(val, dict):
                texts.append(val.get("body", "") or val.get("text", ""))

    # Also check forwarded context for any embedded text
    context = msg.get("context", {})
    if context:
        ctx_text = context.get("body", "") or context.get("text", "")
        if ctx_text:
            texts.append(str(ctx_text))

    seen: set[str] = set()
    result: list[str] = []
    for fragment in texts:
        for url in extract_urls(fragment):
            if url not in seen:
                seen.add(url)
                result.append(url)

    return result


def normalize_url(url: str) -> str:
    """Normalize a URL: lowercase host, strip tracking params, remove fragments.

    Preserves ATS-important params like gh_jid.
    """
    try:
        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # Strip trailing slash from path (keep "/" if sole path)
        path = parsed.path.rstrip("/") or "/"

        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {
                k: v for k, v in qs.items()
                if k.lower() not in TRACKING_PARAMS or k in ATS_PRESERVE_PARAMS
            }
            query = urlencode(filtered, doseq=True) if filtered else ""
        else:
            query = ""

        return urlunparse((scheme, netloc, path, "", query, ""))

    except Exception:
        return url


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL for deduplication."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_short_url(url: str) -> bool:
    """Check if a URL belongs to a known shortener."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return host in SHORT_URL_DOMAINS
    except Exception:
        return False


async def expand_short_url(url: str, max_redirects: int = 8) -> str:
    """Follow redirects to resolve a shortened URL.

    Tries HEAD first (cheaper), falls back to GET.
    Returns the original URL on any error.
    """
    if not is_short_url(url):
        return url

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=max_redirects,
        timeout=10.0,
    ) as client:
        try:
            resp = await client.head(url)
            final_url = str(resp.url)
            logger.debug("expanded_short_url", original=url, expanded=final_url)
            return final_url
        except Exception:
            pass

        try:
            resp = await client.get(url)
            final_url = str(resp.url)
            logger.debug("expanded_short_url_via_get", original=url, expanded=final_url)
            return final_url
        except Exception as exc:
            logger.warning("short_url_expansion_failed", url=url, error=str(exc))
            return url


async def expand_and_normalize(url: str) -> str:
    """Expand a shortened URL then normalize it."""
    expanded = await expand_short_url(url)
    return normalize_url(expanded)


def job_signature(title: str, company: str, location: str) -> str:
    """Generate a dedup signature for a job posting."""
    raw = f"{title.lower().strip()}|{company.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
