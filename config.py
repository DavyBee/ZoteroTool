"""
config.py — Settings, paths, and constants.

Keys and library settings come from Streamlit's secrets (the Secrets box on
Streamlit Cloud, or .streamlit/secrets.toml). They are copied into environment
variables, which take precedence if already set.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# ── Local files ───────────────────────────────────────────────────────────────
# Nothing here is permanent: Zotero is the record. On Streamlit Cloud these
# folders are wiped on every restart, which is fine.

# Log of every AI request and response, for checking what the AI step did.
STATE_DIR      = PROJECT_ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LLM_AUDIT_PATH = STATE_DIR / "llm_audit.jsonl"

# PDFs downloaded during a scan, attached to their Zotero items on upload.
PDFS_DIR        = PROJECT_ROOT / "pdfs"
PDF_MAX_BYTES   = 50 * 1024 * 1024     # 50 MB per file; larger ones are skipped
PDF_FETCH_BYTES = 60 * 1024 * 1024     # hard read cap (slightly above max)

# ── Network ───────────────────────────────────────────────────────────────────

USER_AGENT     = "eviction-lab-zotero-importer/1.0"
# A realistic Chrome string for content fetches that get bot-detected when
# we use the friendly USER_AGENT (e.g., bse.eu, some BMJ pages, Cloudflare-
# protected publisher CDNs). Used by pdf_storage.py for PDF downloads.
# The friendly USER_AGENT is still preferred for API calls (CrossRef gives
# polite-UA clients better priority); Citoid has its own contact-carrying UA
# (CITOID_USER_AGENT) required by Wikimedia's usage policy.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
CROSSREF_BASE  = "https://api.crossref.org/works"
HTTP_TIMEOUT   = 15    # seconds

# Citoid: Wikimedia's hosted copy of Zotero's translators (the same ones the
# Zotero browser extension uses). GET <base><url-encoded URL> returns a JSON
# list of Zotero items.
CITOID_BASE = "https://en.wikipedia.org/api/rest_v1/data/citation/zotero/"

# Wikimedia's API policy asks for a User-Agent with contact details.
CITOID_USER_AGENT = (
    "eviction-lab-zotero-importer/1.0 "
    "(https://github.com/DavyBee/ZoteroTool; david.beeson123@gmail.com)"
)

# Minimum seconds between Citoid requests (see metadata._citoid_get).
CITOID_MIN_INTERVAL = 1.0

# ── LLM ───────────────────────────────────────────────────────────────────────

LLM_MODEL_DEFAULT = "claude-haiku-5-5"

# Characters of page text sent to the AI. On publisher pages the author list
# can come well after the start of the text.
LLM_PAGE_TEXT_CHARS = 30_000

# A scanned PDF (no text layer) is sent to the AI as images. The title,
# authors and DOI are on the first pages, so only these are sent.
LLM_VISION_PAGES = 2


# ── Secrets ───────────────────────────────────────────────────────────────────

def load_streamlit_secrets() -> None:
    """Copy each top-level secret into os.environ, without overriding a
    variable that is already set. Does nothing if there are no secrets."""
    try:
        import streamlit as st
    except Exception:
        return
    try:
        items = list(st.secrets.items())
    except Exception:
        return
    for key, val in items:
        if isinstance(val, (str, int, float, bool)):
            os.environ.setdefault(key, str(val))


load_streamlit_secrets()


# ── Accessors ─────────────────────────────────────────────────────────────────

def zotero_api_key() -> str | None:
    return os.environ.get("ZOTERO_API_KEY")

def zotero_library_id() -> str | None:
    return os.environ.get("ZOTERO_LIBRARY_ID")

def zotero_library_type() -> str:
    return os.environ.get("ZOTERO_LIBRARY_TYPE", "group")

def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")

def zotero_collection_key() -> str | None:
    return os.environ.get("ZOTERO_COLLECTION_KEY")

def llm_model() -> str:
    # An empty LLM_MODEL means "use the default".
    return os.environ.get("LLM_MODEL") or LLM_MODEL_DEFAULT


def set_llm_model(model: str) -> None:
    """Switch the Claude model for the rest of this process. Used by the AI
    step when the configured model has been retired (metadata._auto_heal_model).
    Not saved: after a restart the switch happens again on the first request."""
    os.environ["LLM_MODEL"] = model
