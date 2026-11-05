"""
zotero_client.py — Push pipeline records to a shared Zotero library.

Eligibility for programmatic upload:
    metadata_source in (translator, crossref_doi, llm) AND title set AND
    zotero_key not yet assigned.

pdf_saved and needs_manual papers are excluded — those go through the
manual Zotero desktop workflow. PDFs are never attached programmatically;
all saved PDFs go to pdfs_to_upload/ for import via Zotero desktop so
Zotero's recognizer remains the authoritative PDF handler.
"""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Callable, Optional

from pyzotero import zotero

import config
import pdf_storage
from database import Database, Paper, normalize_url, READY_SOURCES


BATCH_SIZE = 50          # Zotero API hard limit per create_items call


def _create_items(zot, items, retries: int = 3):
    """zot.create_items, hardened against pyzotero's 429 handling bug.

    On a rate-limited write pyzotero records the server's backoff but then tries
    to parse the empty 429 body as JSON, raising a ValueError
    ("Expecting value: line 1 column 1 (char 0)"). The backoff *is* stored, so
    retrying works: create_items calls _check_backoff() first, which sleeps off
    the recorded delay before re-sending. We retry only that specific empty-body
    parse error so genuine failures still surface immediately."""
    for attempt in range(retries):
        try:
            return zot.create_items(items)
        except ValueError as exc:
            # JSONDecodeError (a ValueError subclass) from the empty 429 body.
            if "Expecting value" not in str(exc) or attempt == retries - 1:
                raise
            # If pyzotero didn't record a backoff, sleep a small floor so the
            # retry doesn't immediately re-trip the limiter.
            if getattr(zot, "backoff_until", 0.0) <= time.time():
                time.sleep(1.0 * (attempt + 1))

# Where to put the journal/venue name depends on the Zotero item type.
# None means the type has no venue field — skip it entirely.
# Derived from pyzotero item_template() calls against the live API.
_VENUE_FIELD: dict[str, str | None] = {
    "journalArticle":  "publicationTitle",
    "preprint":        "repository",
    "thesis":          "university",
    "report":          "institution",
    "blogPost":        "blogTitle",
    "webpage":         "websiteTitle",
    "conferencePaper": "proceedingsTitle",
    "bookSection":     "bookTitle",
    "book":            None,   # no venue field
    "podcast":         None,   # no venue field
}

# Fields that are only valid for certain item types, verified via
# pyzotero item_template() calls. Sending an unsupported field causes
# Zotero to reject the entire item.
_TYPE_SUPPORTS: dict[str, frozenset[str]] = {
    "journalArticle":  frozenset(["volume", "issue", "pages", "publisher"]),
    "preprint":        frozenset(),
    "report":          frozenset(["pages"]),
    "book":            frozenset(["volume", "publisher"]),
    "bookSection":     frozenset(["volume", "pages", "publisher"]),
    "conferencePaper": frozenset(["volume", "issue", "pages", "publisher"]),
    "thesis":          frozenset(),
    "blogPost":        frozenset(),
    "webpage":         frozenset(["publisher"]),
    "podcast":         frozenset(["publisher"]),
}

# Non-Zotero / CSL-style type names that sources (esp. the LLM) sometimes emit,
# mapped to the closest valid Zotero itemType. Anything not in _TYPE_SUPPORTS
# after aliasing is coerced to "webpage" (see _zotero_item_type) so a bad type
# can't make Zotero reject the whole item — which is what happened with the
# LLM-invented "podcastEpisode".
_ITEM_TYPE_ALIASES = {
    "podcastEpisode":   "podcast",
    "audioRecording":   "podcast",
    "magazineArticle":  "webpage",
    "newspaperArticle": "webpage",
    "post":             "blogPost",
    "article":          "journalArticle",
    "webPage":          "webpage",
}


def _zotero_item_type(raw_type: str | None) -> str:
    """Normalize a stored item_type to a Zotero type we map correctly. Applies
    known aliases, then falls back to 'webpage' for anything we don't handle."""
    t = _ITEM_TYPE_ALIASES.get(raw_type or "", raw_type or "journalArticle")
    return t if t in _TYPE_SUPPORTS else "webpage"


# ── Public API ────────────────────────────────────────────────────────────────

def make_zotero_client() -> zotero.Zotero:
    api_key     = config.zotero_api_key()
    library_id  = config.zotero_library_id()
    library_type = config.zotero_library_type()
    if not api_key:
        raise ValueError("ZOTERO_API_KEY not set — add it to .env or the environment")
    if not library_id:
        raise ValueError("ZOTERO_LIBRARY_ID not set — add it to .env or the environment")
    return zotero.Zotero(library_id, library_type, api_key)


def uploadable_papers(db: Database) -> list[Paper]:
    """Papers ready for programmatic upload: right source, have title, not yet pushed.

    translator / crossref_doi records are auto-approved (Zotero's own engine).
    LLM records are lower-confidence, so they only become uploadable once a human
    has accepted them in the GUI (the `reviewed` flag). This guarantees every LLM
    item is eyeballed before it lands in the shared library.
    """
    out = []
    for p in db.all_papers():
        if p.get("zotero_key") or p.get("upload_error"):
            continue                      # error papers are parked for fixing, not Ready
        src = p.get("metadata_source")
        if src in READY_SOURCES:
            out.append(p)
        elif src == "llm" and p.get("reviewed"):
            out.append(p)
    return out


def upload_papers(
    papers: list[Paper],
    zot: zotero.Zotero,
    db: Optional[Database] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """
    Upload papers to Zotero in batches of BATCH_SIZE.

    For each successfully created item, sets paper["zotero_key"], and writes
    zotero_key and uploaded_at back to the DB when one is given. A rejected
    item gets paper["upload_error"]. PDFs are handled separately — all saved PDFs go to
    pdfs_to_upload/ for import via Zotero desktop.

    Returns:
        {total, uploaded, pdfs_attached, failed, errors: [(url, msg), ...]}
    """
    total = len(papers)
    n_ok = n_fail = n_pdf = n_notes = 0
    errors: list[tuple[str, str]] = []

    for batch_start in range(0, total, BATCH_SIZE):
        batch = papers[batch_start : batch_start + BATCH_SIZE]
        items = [paper_to_zotero_item(p) for p in batch]

        try:
            resp = _create_items(zot, items)
        except Exception as exc:
            # Whole batch failed (network error, auth failure, etc.).
            # Don't touch last_error — those papers are still enriched and
            # will be retried on the next upload run automatically.
            msg = f"batch upload failed: {exc}"
            for p in batch:
                errors.append((p["url"], msg))
                p["upload_error"] = msg
            n_fail += len(batch)
            continue

        for idx_str, item_data in resp.get("successful", {}).items():
            paper = batch[int(idx_str)]
            zotero_key = item_data["data"]["key"]
            paper["zotero_key"] = zotero_key
            if db is not None:
                db.mark_uploaded(normalize_url(paper["url"]), zotero_key)
            n_ok += 1

            # Attach the local file for ANY uploaded record that has one. We now
            # LLM-read PDFs/docx (better than Zotero's recognizer for the working
            # papers/reports this lab shares) and upload via the API, so the file
            # is attached here and the Slack notes ride along — no manual drag-in.
            # (Papers with no extractable text stay pdf_saved → drag-in fallback,
            # and pdf_saved isn't uploadable, so it never reaches this loop.)
            # ensure_local_pdf makes the file present even after a cloud restart
            # wiped pdfs/: disk → durable pdf_blobs copy → just-in-time
            # re-download (the pdf_too_large path). Papers that never landed a
            # PDF are skipped outright — no download attempt at upload time
            # (see ensure_local_pdf's docstring: retrying blocked publishers
            # here made every upload run crawl).
            if paper.get("local_pdf_path"):
                local_pdf = pdf_storage.ensure_local_pdf(paper)
                if local_pdf:
                    try:
                        attach_file(zot, zotero_key, local_pdf, _file_name(paper, local_pdf))
                        n_pdf += 1
                    except Exception as exc:
                        errors.append((paper["url"], f"pdf_attach failed: {exc}"))
                else:
                    errors.append((paper["url"],
                                   "pdf_attach skipped: file not on disk and "
                                   "re-download failed (item uploaded without PDF)"))

            # Attach the Slack discussion (original message + thread replies) as a
            # child note. Non-fatal: the parent is already uploaded, so a note
            # failure is recorded but doesn't fail the paper.
            comments = paper.get("comments") or []
            if comments:
                note = {"itemType": "note",
                        "note": _comments_to_note_html(comments),
                        "parentItem": zotero_key}
                try:
                    _create_items(zot, [note])
                    n_notes += 1
                except Exception as exc:
                    errors.append((paper["url"], f"note upload failed: {exc}"))

        for idx_str, err_data in resp.get("failed", {}).items():
            paper = batch[int(idx_str)]
            msg = err_data.get("message") or "unknown error"
            errors.append((paper["url"], f"upload failed: {msg}"))
            # Stamp the per-item rejection onto the paper so it leaves Ready and
            # shows in the Upload tab's fixable error list (vs whole-batch network
            # failures above, which are transient and left to auto-retry).
            paper["upload_error"] = msg
            if db is not None:
                db.update_paper(normalize_url(paper["url"]), upload_error=msg)
            n_fail += 1

        if db is not None:
            db.save()
        if progress_cb:
            progress_cb(min(batch_start + BATCH_SIZE, total), total)

    return {
        "total":         total,
        "uploaded":      n_ok,
        "pdfs_attached": n_pdf,
        "notes_added":   n_notes,
        "failed":        n_fail,
        "errors":        errors,
    }


def _comments_to_note_html(comments: list) -> str:
    """Render a paper's Slack comments (original message + thread replies) as one
    Zotero note in HTML. Text is HTML-escaped and newlines become <br> so a stray
    '<' or '&' in a Slack message can't corrupt the note."""
    parts = ["<p><b>Slack discussion (Eviction Lab)</b></p>"]
    for c in comments:
        author = html.escape(c.get("author") or "?")
        text = html.escape(c.get("text") or "").replace("\n", "<br>")
        tag = " <i>(reply)</i>" if c.get("is_reply") else ""
        parts.append(f"<p><b>{author}</b>{tag}: {text}</p>")
    return "".join(parts)


# ── File attachments ──────────────────────────────────────────────────────────
# Zotero's documented file-upload protocol, done directly rather than through
# pyzotero's attachment helpers (whose filename handling has changed between
# versions and fails silently, returning a "failure" list instead of raising).
# https://www.zotero.org/support/dev/web_api/v3/file_upload

def _file_name(paper: dict, path: str) -> str:
    """A readable stored filename, e.g. "Diamond 2021 - Where is Standard of
    Living the Highest.pdf". Zotero rejects names containing a directory."""
    ext = os.path.splitext(path)[1].lower() or ".pdf"
    authors = paper.get("authors") or []
    first = authors[0].split(",")[0].strip() if authors else ""
    parts = " ".join(x for x in (first, paper.get("year") or "") if x)
    title = re.sub(r"[^\w\s\-]", "", paper.get("title") or "")[:80].strip()
    name = " - ".join(x for x in (parts, title) if x) or "Full Text"
    return re.sub(r"\s+", " ", name) + ext


def attach_file(zot, parent_key: str, path: str, filename: str) -> None:
    """Upload `path` as a stored-file attachment of `parent_key`. Raises on any
    failure, after removing the empty attachment item it created."""
    with open(path, "rb") as f:
        body = f.read()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    resp = _create_items(zot, [{
        "itemType": "attachment", "linkMode": "imported_file", "parentItem": parent_key,
        "title": "Full Text PDF" if ctype == "application/pdf" else filename,
        "contentType": ctype, "filename": filename, "tags": [], "relations": {},
    }])
    if not resp.get("successful"):
        msg = next(iter(resp.get("failed", {}).values()), {}).get("message", "unknown error")
        raise RuntimeError(f"attachment item rejected: {msg}")
    key = resp["successful"]["0"]["data"]["key"]
    base = f"{zot.endpoint}/{zot.library_type}/{zot.library_id}/items/{key}/file"
    headers = {"Zotero-API-Key": zot.api_key, "Zotero-API-Version": "3",
               "If-None-Match": "*"}
    try:
        auth = json.loads(_http("POST", base, urlencode({
            "md5": hashlib.md5(body).hexdigest(), "filename": filename,
            "filesize": len(body), "mtime": int(os.path.getmtime(path) * 1000),
        }).encode(), {**headers, "Content-Type": "application/x-www-form-urlencoded"}))
        if auth.get("exists"):
            return
        _http("POST", auth["url"],
              auth["prefix"].encode("latin-1") + body + auth["suffix"].encode("latin-1"),
              {"Content-Type": auth["contentType"]})
        _http("POST", base, urlencode({"upload": auth["uploadKey"]}).encode(),
              {**headers, "Content-Type": "application/x-www-form-urlencoded"})
    except Exception:
        try:
            zot.delete_item(zot.item(key))
        except Exception:
            pass
        raise


def _http(method: str, url: str, data: bytes, headers: dict) -> bytes:
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as r:
            return r.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


# ── Item mapping ──────────────────────────────────────────────────────────────

def paper_to_zotero_item(paper: Paper) -> dict:
    """Convert a Paper record into a Zotero item dict suitable for create_items."""
    item_type = _zotero_item_type(paper.get("item_type"))

    item: dict = {
        "itemType":     item_type,
        # Upload as-is even when the metadata stage didn't return a title — fall
        # back to the Slack filename, then the URL, so the item is never blank.
        "title":        paper.get("title") or paper.get("slack_file_name") or paper.get("url") or "",
        "creators":     _build_creators(paper.get("authors") or []),
        "abstractNote": paper.get("abstract") or "",
        "url":          paper.get("url") or "",
        "language":     paper.get("language") or "",
        # No tags: uploads carry only bibliographic metadata, the file or link,
        # and the Slack discussion note.
    }

    coll_key = config.zotero_collection_key()
    if coll_key:
        item["collections"] = [coll_key]

    if paper.get("year"):
        item["date"] = paper["year"]

    if paper.get("doi"):
        item["DOI"] = paper["doi"]

    journal = paper.get("journal")
    if journal:
        venue_field = _VENUE_FIELD.get(item_type, "publicationTitle")
        if venue_field:
            item[venue_field] = journal

    supported = _TYPE_SUPPORTS.get(item_type, frozenset(["volume", "issue", "pages", "publisher"]))
    if paper.get("publisher") and "publisher" in supported:
        item["publisher"] = paper["publisher"]
    if paper.get("volume") and "volume" in supported:
        item["volume"] = paper["volume"]
    if paper.get("issue") and "issue" in supported:
        item["issue"] = paper["issue"]
    if paper.get("pages") and "pages" in supported:
        item["pages"] = paper["pages"]
    if paper.get("isbn"):
        item["ISBN"] = paper["isbn"]

    return item


def _build_creators(authors: list[str]) -> list[dict]:
    creators = []
    for name in authors:
        name = name.strip()
        if not name:
            continue
        if ", " in name:
            last, first = name.split(", ", 1)
            creators.append({
                "creatorType": "author",
                "firstName":   first.strip(),
                "lastName":    last.strip(),
            })
        else:
            creators.append({
                "creatorType": "author",
                "firstName":   "",
                "lastName":    name,
            })
    return creators
