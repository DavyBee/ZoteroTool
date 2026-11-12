"""
pdf_storage.py — Download a source's PDF so it can be attached in Zotero.

save_pdf(url) tries, in order:
    1. GET the URL. If the response is a real PDF (Content-Type or the %PDF
       magic bytes), save it.
    2. If the response is HTML, follow its <meta name="citation_pdf_url"> tag
       (the same Highwire convention as the other citation_* tags) once.
    3. Anything else (paywall page, bot block, error): no PDF.

save_slack_file downloads a document uploaded to Slack, using the expiring
url_private link from the export.

Files are saved as pdfs/<hash of URL>.<ext>, which avoids illegal characters
and makes repeat downloads free. The %PDF check keeps HTML paywall pages from
being saved as PDFs. config.PDF_MAX_BYTES caps file size.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from . import config
from . import http_fetch


# ── Public entry point ────────────────────────────────────────────────────────

def save_pdf(url: str) -> Optional[tuple[str, int]]:
    """
    Try to download and locally save a PDF for `url`. Returns (path, size_in_
    bytes) on success, or None if no PDF could be obtained.

    Idempotent: if the target file already exists on disk and is a valid
    PDF, returns its path immediately without re-downloading.
    """
    config.PDFS_DIR.mkdir(parents=True, exist_ok=True)

    target = _target_path_for(url)
    if target.exists() and _looks_like_pdf_file(target):
        return (str(target), target.stat().st_size)

    # Pass 1: fetch the URL itself
    body, content_type = _fetch(url)
    if body is None:
        return None

    if _is_pdf_payload(content_type, body):
        return _write_if_within_limit(target, body)

    # Pass 2: if we got HTML, look for citation_pdf_url and try that once
    if _looks_like_html(content_type, body):
        pdf_url = _extract_citation_pdf_url(body, base=url)
        if pdf_url:
            body2, content_type2 = _fetch(pdf_url)
            if body2 is not None and _is_pdf_payload(content_type2, body2):
                return _write_if_within_limit(target, body2)

    return None


# Uploaded file types treated as papers. Images, slides, and spreadsheets are not.
_SLACK_DOC_EXTS = (".pdf", ".docx", ".doc", ".rtf", ".odt")


def _slack_doc_ext(filename: str) -> str:
    """The original extension if it's a supported document type, else '.pdf'."""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext in _SLACK_DOC_EXTS:
            return ext
    return ".pdf"


def save_slack_file(download_url: str, key_url: str,
                    filename: str = "") -> Optional[tuple[str, int]]:
    """Download a Slack-uploaded document (PDF or Word/RTF/ODT) from the
    token-bearing url_private, stored keyed by `key_url` (the stable permalink)
    and keeping the original extension so Zotero handles the file correctly.

    PDFs get the %PDF check. Other types are accepted unless Slack returned an
    HTML page, which means the token has expired. Returns (path, size) or None."""
    config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    ext = _slack_doc_ext(filename)
    digest = hashlib.sha256(key_url.encode("utf-8")).hexdigest()[:16]
    target = config.PDFS_DIR / f"{digest}{ext}"
    if target.exists() and target.stat().st_size > 0:
        return (str(target), target.stat().st_size)
    body, content_type = _fetch(download_url)
    if body is None:
        return None
    if ext == ".pdf":
        if not _is_pdf_payload(content_type, body):
            return None
    elif _looks_like_html(content_type, body):
        return None                       # expired-token login page, not the file
    return _write_if_within_limit(target, body)


# ── HTTP fetch ────────────────────────────────────────────────────────────────

def _fetch(url: str) -> tuple[Optional[bytes], str]:
    """(body, content_type), with body None on any failure. Reads at most
    config.PDF_FETCH_BYTES. Paywalled PDFs won't download; open-access ones
    usually do."""
    body, content_type, _status = http_fetch.fetch(url, max_bytes=config.PDF_FETCH_BYTES)
    return body, content_type


# ── Type detection ────────────────────────────────────────────────────────────

def _is_pdf_payload(content_type: str, body: bytes) -> bool:
    """
    A response is a PDF iff the body starts with %PDF. Content-Type alone is
    not trusted: some publishers serve paywall HTML with Content-Type=
    application/pdf, and some open-access mirrors serve PDFs as
    application/octet-stream. Magic bytes are the source of truth.
    """
    return body.startswith(b"%PDF")


def _looks_like_html(content_type: str, body: bytes) -> bool:
    if "text/html" in content_type or "application/xhtml" in content_type:
        return True
    head = body[:200].lower()
    return b"<html" in head or b"<!doctype html" in head


def _looks_like_pdf_file(path) -> bool:
    """Cheap on-disk check used for the idempotency early-return."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


# ── citation_pdf_url meta-tag extraction ──────────────────────────────────────

# Google-Scholar / Zotero standard. Most major academic publishers include
# either citation_pdf_url or (less commonly) og:pdf_url in the page head.
# We accept both attribute orderings (name= before content= or vice versa)
# and both meta names. Case-insensitive on attribute names and the meta-name
# value; the URL itself is preserved as-is.
_CITATION_PDF_RE = re.compile(
    rb'<meta[^>]+(?:name|property)\s*=\s*["\']'
    rb'(?:citation_pdf_url|og:pdf_url)["\']'
    rb'[^>]*content\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Same shape, with the attribute order flipped (content= first).
_CITATION_PDF_RE_FLIPPED = re.compile(
    rb'<meta[^>]+content\s*=\s*["\']([^"\']+)["\']'
    rb'[^>]*(?:name|property)\s*=\s*["\']'
    rb'(?:citation_pdf_url|og:pdf_url)["\']',
    re.IGNORECASE,
)


def _extract_citation_pdf_url(html: bytes, base: str) -> Optional[str]:
    """
    Pull the citation_pdf_url from an HTML response. Resolves relative URLs
    against `base` so '/content/foo.pdf'-style hrefs become absolute.
    Returns None if no match.
    """
    for pattern in (_CITATION_PDF_RE, _CITATION_PDF_RE_FLIPPED):
        m = pattern.search(html)
        if m:
            href = m.group(1).decode("utf-8", errors="replace").strip()
            if href:
                return urljoin(base, href)
    return None


# ── File layout ───────────────────────────────────────────────────────────────

def _target_path_for(url: str):
    """Hash-based filename in the configured PDFs directory."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return config.PDFS_DIR / f"{digest}.pdf"


def _write_if_within_limit(target, body: bytes) -> Optional[tuple[str, int]]:
    """Honor the size cap, write atomically, return (path, size)."""
    if len(body) > config.PDF_MAX_BYTES:
        return None     # too big; skip it
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return (str(target), len(body))


def save_attached_pdf(data: bytes) -> tuple[str, int]:
    """Save a PDF a person attached on a review card; returns (path, size).
    Raises ValueError with a message for the page. Named by its contents, so
    one file never replaces another."""
    if b"%PDF" not in data[:1024]:
        raise ValueError("This file is not a PDF.")
    if len(data) > config.PDF_MAX_BYTES:
        raise ValueError(f"This file is over {config.PDF_MAX_BYTES // (1024 * 1024)} MB.")
    config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    saved = _write_if_within_limit(config.PDFS_DIR / f"attached-{digest}.pdf", data)
    if not saved:
        raise ValueError("The file could not be saved. Try again.")
    return saved


def ensure_local_pdf(paper: dict) -> Optional[str]:
    """The paper's downloaded file, if it is still on disk."""
    path = paper.get("local_pdf_path")
    if path and Path(path).is_file() and Path(path).stat().st_size > 0:
        return path
    return None
