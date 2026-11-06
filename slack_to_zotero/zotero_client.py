"""
zotero_client.py — Create items in the Zotero library.

For each source selected on the page: one item with its bibliographic
metadata, its file attached when one was downloaded, and a child note with
the Slack post and its replies. Nothing else is written (no tags, no extra
collections).
"""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Callable, Optional

from pyzotero import zotero

from . import config
from . import pdf_storage
from .records import Paper


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
# None means the type has no venue field.
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

# Types with an ISBN field (Zotero's schema, https://api.zotero.org/schema).
# Zotero rejects an item that sends ISBN on any other type.
_HAS_ISBN = frozenset(["book", "bookSection", "conferencePaper", "report", "thesis"])

# Type names that aren't Zotero item types (the AI sometimes returns these),
# mapped to the closest one. Anything still unknown becomes "webpage"
# (_zotero_item_type), because Zotero rejects an item with an invalid type.
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
        raise ValueError("ZOTERO_API_KEY is not set in the app's secrets")
    if not library_id:
        raise ValueError("ZOTERO_LIBRARY_ID is not set in the app's secrets")
    return zotero.Zotero(library_id, library_type, api_key)


def library_label() -> str:
    """The library's name with its id, e.g. "Eviction Lab Library (group 123)".
    A personal library is "My Library" in Zotero."""
    kind, lib = config.zotero_library_type(), config.zotero_library_id()
    if kind != "group":
        return f"My Library (user {lib})"
    zot = make_zotero_client()
    data = json.loads(_http("GET", f"{zot.endpoint}/groups/{lib}", None,
                            {"Zotero-API-Key": zot.api_key, "Zotero-API-Version": "3"}))
    return f"{data['data']['name']} (group {lib})"


def collection_label() -> str:
    """The upload collection's name with its key, or "Library root"."""
    key = config.zotero_collection_key()
    if not key:
        return "Library root"
    return f"{make_zotero_client().collection(key)['data']['name']} ({key})"


def upload_papers(
    papers: list[Paper],
    zot: zotero.Zotero,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Upload papers to Zotero, BATCH_SIZE at a time: one request creates the
    items, one more adds their Slack notes, then files are attached one by one.
    progress_cb(done, total) and log_cb(line) are called as each item finishes,
    so progress shows from the first few seconds.

    Sets paper["zotero_key"] on each item created and paper["upload_error"]
    on each one Zotero rejects. Attachment and note failures don't fail the
    item; they are listed in the returned errors.

    Returns:
        {total, uploaded, pdfs_attached, notes_added, failed, errors: [(url, msg), ...]}
    """
    total = len(papers)
    n_ok = n_fail = n_pdf = n_notes = 0
    errors: list[tuple[str, str]] = []
    done = 0

    def finished(paper: Paper, status: str) -> None:
        nonlocal done
        done += 1
        if progress_cb:
            progress_cb(done, total)
        if log_cb:
            log_cb(f"{status:10}{paper.get('title') or paper['url']}")

    for batch_start in range(0, total, BATCH_SIZE):
        batch = papers[batch_start : batch_start + BATCH_SIZE]

        try:
            resp = _create_items(zot, [paper_to_zotero_item(p) for p in batch])
        except Exception as exc:
            # Network or authentication failure. "Retry failed" on the page
            # sends these again.
            msg = f"batch upload failed: {exc}"
            for p in batch:
                errors.append((p["url"], msg))
                p["upload_error"] = msg
                n_fail += 1
                finished(p, "Failed")
            continue

        for idx_str, err_data in resp.get("failed", {}).items():
            paper = batch[int(idx_str)]
            msg = err_data.get("message") or "unknown error"
            errors.append((paper["url"], f"upload failed: {msg}"))
            paper["upload_error"] = msg
            n_fail += 1
            finished(paper, "Failed")

        created = []
        for idx_str, item_data in sorted(resp.get("successful", {}).items(),
                                         key=lambda kv: int(kv[0])):
            paper = batch[int(idx_str)]
            paper["zotero_key"] = item_data["data"]["key"]
            created.append(paper)
            n_ok += 1

        # The Slack notes for the whole batch, in one request. A note failure
        # doesn't fail its item, which is already in Zotero.
        with_notes = [p for p in created if p.get("comments")]
        if with_notes:
            notes = [{"itemType": "note", "parentItem": p["zotero_key"],
                      "note": _comments_to_note_html(p["comments"])} for p in with_notes]
            try:
                note_resp = _create_items(zot, notes)
                n_notes += len(note_resp.get("successful", {}))
                for idx_str, err in note_resp.get("failed", {}).items():
                    errors.append((with_notes[int(idx_str)]["url"],
                                   f"note upload failed: {err.get('message')}"))
            except Exception as exc:
                for p in with_notes:
                    errors.append((p["url"], f"note upload failed: {exc}"))

        # Files, one at a time. An item is finished once its file is attached.
        for paper in created:
            if paper.get("local_pdf_path"):
                local_pdf = pdf_storage.ensure_local_pdf(paper)
                if local_pdf:
                    try:
                        attach_file_with_retry(zot, paper["zotero_key"], local_pdf,
                                               _file_name(paper, local_pdf))
                        n_pdf += 1
                    except Exception as exc:
                        errors.append((paper["url"], f"pdf_attach failed: {exc}"))
                else:
                    errors.append((paper["url"],
                                   "pdf_attach skipped: the downloaded file is gone "
                                   "(the app restarted); item uploaded without it"))
            finished(paper, "Uploaded")

    return {
        "total":         total,
        "uploaded":      n_ok,
        "pdfs_attached": n_pdf,
        "notes_added":   n_notes,
        "failed":        n_fail,
        "errors":        errors,
    }


def _comments_to_note_html(comments: list) -> str:
    """The Slack post and its replies as one Zotero note. Text is HTML-escaped
    so a '<' or '&' in a message can't break the note."""
    parts = ["<p><b>Slack replies</b></p>"]
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
        fail = next(iter(resp.get("failed", {}).values()), {})
        msg = fail.get("message", "unknown error")
        code = fail.get("code") or 500
        err = TransientError if code >= 500 or code == 429 else RuntimeError
        raise err(f"attachment item rejected: {msg}")
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


class TransientError(RuntimeError):
    """A Zotero failure worth retrying: 429, 5xx, or a network error."""


ATTACH_ATTEMPTS = 3
ATTACH_RETRY_WAIT = 3.0     # seconds; multiplied by the attempt number


def attach_file_with_retry(zot, parent_key: str, path: str, filename: str) -> None:
    for attempt in range(ATTACH_ATTEMPTS):
        try:
            return attach_file(zot, parent_key, path, filename)
        except TransientError:
            if attempt == ATTACH_ATTEMPTS - 1:
                raise
            time.sleep(ATTACH_RETRY_WAIT * (attempt + 1))


def _http(method: str, url: str, data: bytes, headers: dict) -> bytes:
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as r:
            return r.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        err = TransientError if exc.code >= 500 or exc.code == 429 else RuntimeError
        raise err(f"HTTP {exc.code}: {detail}") from None
    except (URLError, TimeoutError) as exc:
        raise TransientError(f"network error: {exc}") from None


# ── Item mapping ──────────────────────────────────────────────────────────────

def paper_to_zotero_item(paper: Paper) -> dict:
    """Convert a Paper record into a Zotero item dict suitable for create_items."""
    item_type = _zotero_item_type(paper.get("item_type"))

    item: dict = {
        "itemType":     item_type,
        # Without a title, use the Slack file name, then the URL.
        "title":        paper.get("title") or paper.get("slack_file_name") or paper.get("url") or "",
        "creators":     _build_creators(paper.get("authors") or []),
        "abstractNote": paper.get("abstract") or "",
        "url":          paper.get("url") or "",
        "language":     paper.get("language") or "",
        # No tags: uploads carry only bibliographic metadata, the file or link,
        # and the Slack replies note.
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
    if paper.get("isbn") and item_type in _HAS_ISBN:
        item["ISBN"] = paper["isbn"]

    return item


def _build_creators(authors: list[str]) -> list[dict]:
    creators = []
    for name in authors:
        name = name.strip()
        if not name:
            continue
        if "," in name:                           # "Doe, Jane" or "Doe,Jane"
            last, first = (part.strip() for part in name.split(",", 1))
            if not last:                          # ", Jane" or ","
                if first:
                    creators.append({"creatorType": "author", "firstName": "",
                                     "lastName": first})
                continue
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
