"""
scan.py — The scan → review → upload job behind the app's single page.

A Job runs in a background thread on the server, not in the browser session,
so refreshing or closing the page doesn't stop it or lose its results: the page
just reattaches to the current job. Nothing is written to Zotero until upload.
Results live in server memory until the next scan or an app restart.

No Streamlit imports here, so the whole flow is testable without the UI.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import traceback
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from . import metadata
from . import slack_parser
from .records import new_paper, normalize_url
from .zotero_index import (PUBLISHED_TYPES, WORKING_PAPER_TYPES, LibraryIndex,
                          different_versions, doi_key, title_key, titles_match,
                          year_of)


# ── Reading an export ─────────────────────────────────────────────────────────

# Slack day-files are named YYYY-MM-DD.json. Everything else in a full export
# (users.json, channels.json, integration_logs.json, …) is workspace metadata.
_DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
MAX_EXPORT_BYTES = 500 * 1024 * 1024        # uncompressed; guards against zip bombs


class ExportError(ValueError):
    """The uploaded files aren't a usable Slack export."""


class Export:
    """Day-files grouped by channel, plus the user-name map if present."""

    def __init__(self) -> None:
        self.channels: dict[str, list[str]] = {}   # channel → day-file paths
        self.user_map: dict[str, str] = {}


def read_export(uploads: list[tuple[str, bytes]], workdir: str) -> Export:
    """Write the day-files from uploaded .json files and/or .zip exports into
    `workdir`, grouped by channel. Loose .json uploads go under the channel ""."""
    export = Export()
    total = 0

    def add(channel: str, name: str, data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > MAX_EXPORT_BYTES:
            raise ExportError("Export is too large.")
        files = export.channels.setdefault(channel, [])
        # Our own file names, never the archive's paths (no path traversal).
        folder = os.path.join(workdir, f"c{list(export.channels).index(channel)}")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(data)
        files.append(path)

    for name, data in uploads:
        base = os.path.basename(name)
        if base.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(_bytes_io(data))
            except zipfile.BadZipFile:
                raise ExportError(f"{base} is not a valid zip file.")
            with zf:
                for info in zf.infolist():
                    parts = [p for p in info.filename.split("/") if p]
                    if info.is_dir() or not parts or parts[0] == "__MACOSX":
                        continue
                    leaf = parts[-1]
                    if leaf == "users.json" and len(parts) == 1:
                        export.user_map.update(
                            slack_parser.read_users_json(zf.read(info)))
                    elif _DAY_FILE_RE.match(leaf):
                        channel = parts[-2] if len(parts) >= 2 else ""
                        add(channel, leaf, zf.read(info))
        elif base == "users.json":
            export.user_map.update(slack_parser.read_users_json(data))
        elif _DAY_FILE_RE.match(base):
            add("", base, data)

    if not export.channels:
        raise ExportError("No Slack day-files (YYYY-MM-DD.json) found.")
    return export


def _bytes_io(data: bytes):
    import io
    return io.BytesIO(data)


def collect_papers(paths: list[str], user_map: dict[str, str]) -> list[dict]:
    """Parse day-files into one paper per source. Links are merged by
    normalized URL and by DOI; each keeps the union of its Slack comments."""
    papers: dict[str, dict] = {}
    by_doi: dict[str, str] = {}

    def upsert(url: str, doi: Optional[str], comments: list) -> dict:
        key = normalize_url(url)
        if key not in papers and doi and doi.lower() in by_doi:
            key = by_doi[doi.lower()]
        if key not in papers:
            papers[key] = new_paper(url, doi)
            if doi:
                by_doi[doi.lower()] = key
        p = papers[key]
        seen = {c["ts"] for c in p["comments"]}
        p["comments"].extend(c for c in comments if c["ts"] not in seen)
        return p

    for pf in slack_parser.parse_files(paths, user_map):
        for link in pf["links"]:
            p = upsert(link["url"], link["doi"], link["comments"])
            if link.get("hints") and not p.get("slack_hints"):
                p["slack_hints"] = link["hints"]
        for fa in pf["files"]:
            p = upsert(fa["url"], None, fa["comments"])
            p.setdefault("slack_file_url", fa["download_url"])
            p.setdefault("slack_file_name", fa["name"])

    for p in papers.values():
        p["comments"].sort(key=lambda c: float(c.get("ts") or 0))
    return list(papers.values())


# ── Sorting results ───────────────────────────────────────────────────────────

READY, REVIEW, EXISTING = "ready", "review", "existing"
# Sources that found real bibliographic metadata.
FOUND_SOURCES = ("crossref_doi", "translator", "citation_tags", "llm")


def classify(paper: dict) -> tuple[str, str]:
    """(bucket, reason) for an enriched paper. Review is only for items the
    app couldn't handle; each carries a short reason. Existing items say what
    matched. Tweets and other web pages are Ready and upload as links."""
    src = paper.get("metadata_source")
    if paper.get("existing_key"):
        return EXISTING, paper.get("existing_how", "")
    if paper.get("not_scanned"):
        return REVIEW, "Not searched"
    if paper.get("scan_error"):
        return REVIEW, "Error during search"
    if src in ("crossref_doi", "translator", "citation_tags"):
        if paper.get("title"):
            return READY, ""
        return REVIEW, "No title found"
    if src == "llm":
        # With little page text the AI is guessing, unless the title typed into
        # the Slack post confirms it.
        if paper.get("low_context") and not paper.get("hint_confirmed"):
            return REVIEW, "Page could not be accessed"
        return READY, ""
    if src == "link":
        return READY, ""
    if src == "slack_hint":
        return REVIEW, "Title from Slack post only"
    if src == "non_paper":
        return REVIEW, "Likely not a source"
    if src == "needs_manual":
        if paper.get("slack_file_url"):
            return REVIEW, "Slack file could not be downloaded"
        return REVIEW, "Page could not be accessed"
    if src == "pdf_saved":
        return REVIEW, "PDF has no readable text"
    return REVIEW, "No metadata found"


def apply_hints(paper: dict, title_search: Callable[..., Optional[tuple]]) -> None:
    """Use the title typed into a "Post an article" Slack form.

    If enrichment found metadata, a matching typed title confirms it (which
    matters when the AI had little to read). If enrichment found nothing, the
    typed title is searched on CrossRef; failing that it becomes the item's
    title, and the item goes to Review."""
    hints = paper.get("slack_hints") or {}
    hint = hints.get("title")
    if not hint:
        return
    if paper.get("metadata_source") in FOUND_SOURCES \
            and paper.get("title"):
        if titles_match(paper["title"], hint):
            paper["hint_confirmed"] = True
        return
    author = re.split(r",|&| and ", hints.get("authors") or "")[0].strip() or None
    found = title_search(hint, author) or (title_search(hint) if author else None)
    if found:
        source, fields = found
        for k, v in fields.items():
            if v not in (None, "", [], {}):
                paper[k] = v
        paper["metadata_source"] = source
        paper["hint_confirmed"] = True
        paper.pop("low_context", None)
    else:
        paper["title"] = hint
        paper["metadata_source"] = "slack_hint"


# ── The job ───────────────────────────────────────────────────────────────────

SCANNING, SCANNED, UPLOADING, DONE, FAILED = (
    "scanning", "scanned", "uploading", "done", "failed")
WORKERS = 4


class Job:
    """One scan of one export, then its upload. Thread-safe: the background
    thread mutates it under `lock`; the page reads it under `lock`.

    load_index / refresh_index read the Zotero target (see zotero_index). They
    are optional so the flow can be tested offline; the app always passes them."""

    def __init__(self, papers: list[dict], anthropic_key: Optional[str],
                 audit_log_path: Optional[str] = None,
                 enrich: Callable[..., dict] = metadata.enrich_paper,
                 load_index: Optional[Callable[[], LibraryIndex]] = None,
                 refresh_index: Optional[Callable[[LibraryIndex], None]] = None,
                 title_search: Callable[..., Optional[tuple]] = metadata.search_crossref_title,
                 source: Optional[dict] = None):
        self.papers = papers
        # What was scanned, for the page's summary: {"label", "files", "channel"}.
        self.source = source or {}
        self.n_sources = len(papers)
        self.finished_at: Optional[float] = None
        self.anthropic_key = anthropic_key
        self.audit_log_path = audit_log_path
        self._enrich = enrich
        self._load_index = load_index
        self._refresh_index = refresh_index
        self._title_search = title_search
        self.index: Optional[LibraryIndex] = None
        self.lock = threading.RLock()
        self.phase = SCANNING
        self.step = "Reading Zotero library" if load_index else "Searching"
        self.total = len(papers)
        self.done = 0
        self.log: deque[str] = deque(maxlen=1000)
        self.stop_requested = False
        self._running = threading.Event()        # cleared while paused
        self._running.set()
        self._paused_at: Optional[float] = None
        self._paused_for = 0.0
        self.started_at = time.time()
        self.upload_result: Optional[dict] = None
        self.fatal_error: Optional[str] = None
        self.skipped_existing = 0
        self._thread: Optional[threading.Thread] = None

    # Scan ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_scan, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self.lock:
            self.stop_requested = True
        self._running.set()                      # let paused workers finish

    def pause(self) -> None:
        """Hold the search after the items in progress; resume() continues it."""
        with self.lock:
            if not self.stop_requested and self._running.is_set():
                self._paused_at = time.time()
                self._running.clear()

    def resume(self) -> None:
        with self.lock:
            if not self._running.is_set():
                self._paused_for += time.time() - (self._paused_at or time.time())
                self._paused_at = None
            self._running.set()

    def search_seconds(self) -> float:
        """How long the search took, not counting time paused."""
        with self.lock:
            end = self.finished_at or time.time()
            paused = self._paused_for + (end - self._paused_at if self._paused_at else 0)
            return max(end - self.started_at - paused, 0)

    def _mark_existing(self, paper: dict, hit: tuple[str, str]) -> None:
        paper["existing_key"], paper["existing_how"] = hit
        if self.index is not None:
            paper["existing_url"] = self.index.item_url(hit[0])
            paper["existing_title"] = self.index.titles.get(hit[0])
        paper["bucket"], paper["reason"] = classify(paper)
        paper["include"] = False

    def _enrich_one(self, paper: dict) -> dict:
        self._running.wait()
        if self.stop_requested:
            return {"metadata_source": "none", "not_scanned": True}
        try:
            fields = self._enrich(paper, self.anthropic_key, self.audit_log_path)
            trial = {**paper, **fields}
            apply_hints(trial, self._title_search)
            return {k: v for k, v in trial.items() if paper.get(k) is not v}
        except Exception as exc:
            return {"metadata_source": "none", "scan_error": f"{type(exc).__name__}: {exc}"}

    def _run_scan(self) -> None:
        metadata.llm_problem = None
        if self._load_index is not None:
            try:
                index = self._load_index()
            except Exception as exc:
                with self.lock:
                    from .zotero_client import explain_error
                    self.fatal_error = f"Could not read the Zotero library. {explain_error(exc)}"
                    self.finished_at = time.time()
                    self.phase = FAILED
                return
            with self.lock:
                self.index = index
                self.step = "Searching"

        try:
            to_scan = []
            with self.lock:
                for p in self.papers:
                    hit = self.index.match_identifiers(p) if self.index else None
                    if hit:
                        self._mark_existing(p, hit)
                        self.done += 1
                        self.log.append(f"{_label(p)}  {p['url']}")
                    else:
                        to_scan.append(p)

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(self._enrich_one, p): p for p in to_scan}
                for fut in as_completed(futures):
                    paper = futures[fut]
                    fields = fut.result()
                    with self.lock:
                        paper.update(fields)
                        _tidy(paper)
                        hit = self.index.match(paper) if self.index else None
                        if hit:
                            self._mark_existing(paper, hit)
                        else:
                            paper["bucket"], paper["reason"] = classify(paper)
                            paper["include"] = paper["bucket"] == READY
                        self.done += 1
                        self.log.append(f"{_label(paper)}  {paper['url']}")
        except Exception:
            with self.lock:
                self.fatal_error = traceback.format_exc()
        with self.lock:
            for p in self.papers:                  # anything the pool never reached
                if "bucket" not in p:
                    p.update(not_scanned=True, metadata_source="none")
                    p["bucket"], p["reason"] = classify(p)
                    p["include"] = False
            try:
                self._merge_duplicates()
                self._label_working_papers()
            except Exception:
                self.fatal_error = traceback.format_exc()
            self.finished_at = time.time()
            self.phase = SCANNED

    def _label_working_papers(self) -> None:
        """Add "(working paper)" to a working paper's title when its published
        version, with the same title, is in this search or already in Zotero."""
        published = {title_key(p.get("title")) for p in self.papers
                     if p.get("item_type") in PUBLISHED_TYPES and p.get("title")}
        for p in self.papers:
            if p.get("item_type") not in WORKING_PAPER_TYPES or not p.get("title"):
                continue
            if p["title"].endswith("(working paper)"):
                continue
            t = title_key(p["title"])
            if t in published or (self.index is not None and self.index.has_published(p["title"])):
                p["title"] = f"{p['title']} (working paper)"

    def _merge_duplicates(self) -> None:
        """Two links in one export can turn out to be the same work once
        enriched (a doi.org link and the publisher's page). Keep one, preferring
        Ready, and give it the other's Slack comments and PDF."""
        rank = {EXISTING: 0, READY: 1, REVIEW: 2}
        by_doi: dict[str, dict] = {}
        by_title: dict[str, list[dict]] = {}
        keep: list[dict] = []
        order = sorted(self.papers, key=lambda p: (
            rank.get(p.get("bucket"), 3), not p.get("doi"), not p.get("year")))
        for p in order:
            d = doi_key(p.get("doi"))
            t, y = title_key(p.get("title")), year_of(p.get("year"))
            target = by_doi.get(d) if d else None
            if target is None and t:
                target = next((q for q in by_title.get(t, [])
                               if _same_work(p, q, t, y)), None)
            if target is None:
                if d:
                    by_doi[d] = p
                if t:
                    by_title.setdefault(t, []).append(p)
                keep.append(p)
                continue
            ts = {c["ts"] for c in target.get("comments", [])}
            target.setdefault("comments", []).extend(
                c for c in p.get("comments", []) if c["ts"] not in ts)
            target["comments"].sort(key=lambda c: float(c.get("ts") or 0))
            if not target.get("local_pdf_path") and p.get("local_pdf_path"):
                target["local_pdf_path"] = p["local_pdf_path"]
                target["pdf_size_bytes"] = p.get("pdf_size_bytes")
        self.papers = keep

    # Review ----------------------------------------------------------------

    def items(self, bucket: str) -> list[dict]:
        with self.lock:
            rows = [p for p in self.papers if p.get("bucket") == bucket]
        key = (lambda p: (p.get("reason", ""), _sort_title(p))) if bucket == REVIEW \
            else _sort_title
        return sorted(rows, key=key)

    def delete_files(self) -> None:
        """Delete the PDFs this search downloaded. Uploaded ones are already
        attached in Zotero; the rest go with the cleared results."""
        from . import config
        folder = config.PDFS_DIR.resolve()
        for p in self.papers:
            path = p.get("local_pdf_path")
            if not path:
                continue
            try:
                target = Path(path).resolve()
                if target.parent == folder:          # only files the app saved
                    target.unlink(missing_ok=True)
            except OSError:
                pass

    def _changeable(self) -> bool:
        """Items can change only between a scan and an upload, not while
        either runs (a click from a page drawn earlier can still arrive)."""
        return self.phase in (SCANNED, DONE)

    def set_include(self, url: str, include: bool) -> None:
        with self.lock:
            if not self._changeable():
                return
            for p in self.papers:
                if p["url"] == url and p.get("bucket") != EXISTING:
                    if include and p.get("decision") == "removed":
                        return                     # Undo the removal first
                    p["include"] = include
                    return

    # Review decisions. Correct and Edit upload the item; Remove leaves it out
    # (not a source, handled another way such as the Zotero Connector, or
    # simply not wanted).
    DECISIONS = ("correct", "edited", "removed")

    def decide(self, url: str, decision: Optional[str],
               fields: Optional[dict] = None) -> None:
        """Record a review decision (None clears it), applying edited fields."""
        assert decision is None or decision in self.DECISIONS
        with self.lock:
            if not self._changeable():
                return
            for p in self.papers:
                if p["url"] == url and p.get("bucket") == REVIEW:
                    if fields:
                        p.update({k: v for k, v in fields.items()})
                    if decision is None:
                        p.pop("decision", None)
                    else:
                        p["decision"] = decision
                    p["include"] = decision in ("correct", "edited")
                    return

    def move_to_review(self, url: str) -> None:
        """Send a Ready item to Review, to decide on it there."""
        with self.lock:
            if not self._changeable():
                return
            for p in self.papers:
                if p["url"] == url and p.get("bucket") == READY \
                        and not p.get("zotero_key"):
                    p["bucket"], p["reason"] = REVIEW, "Moved from Ready"
                    p["include"] = False
                    p.pop("decision", None)
                    return

    def edit(self, url: str, fields: dict) -> None:
        """Change a Ready or Review item's details (without deciding on it)."""
        with self.lock:
            if not self._changeable():
                return
            for p in self.papers:
                if p["url"] == url and p.get("bucket") in (READY, REVIEW) \
                        and not p.get("zotero_key"):
                    if all(p.get(k) == v for k, v in fields.items()):
                        return
                    p.update(fields)
                    p["was_edited"] = True
                    if p.get("decision") == "correct":
                        p["decision"] = "edited"
                    return

    def selected(self) -> list[dict]:
        with self.lock:
            return [p for p in self.papers
                    if p.get("include") and not p.get("zotero_key")
                    and p.get("bucket") != EXISTING]

    # Upload ----------------------------------------------------------------

    def start_upload(self, make_client: Callable[[], object],
                     upload: Optional[Callable[..., dict]] = None) -> None:
        with self.lock:
            if self.phase not in (SCANNED, DONE):
                return
            papers = self.selected()
            for p in papers:
                p.pop("upload_error", None)
                _prepare_for_upload(p)
            self.phase = UPLOADING
            self.step = "Checking Zotero" if self._refresh_index else "Uploading"
            self.total, self.done = len(papers), 0
            self.log.clear()
        if upload is None:
            from . import zotero_client
            upload = zotero_client.upload_papers
        self._thread = threading.Thread(
            target=self._run_upload, args=(papers, make_client, upload), daemon=True)
        self._thread.start()

    def _recheck(self, papers: list[dict]) -> list[dict]:
        """Drop anything added to Zotero since the scan read it, and anything
        that is the same work as another item in this upload (two items can
        be edited into one). Ready items are kept over Review items."""
        if self._refresh_index is not None and self.index is not None:
            try:
                self._refresh_index(self.index)
            except Exception:
                pass                       # still check against what was read before
        from .zotero_client import paper_to_zotero_item
        batch = LibraryIndex()
        out = []
        with self.lock:
            for p in sorted(papers, key=lambda p: p.get("bucket") != READY):
                hit = self.index.match(p) if self.index is not None else None
                if hit:
                    self._mark_existing(p, hit)
                    continue
                twin = batch.match(p)
                if twin:
                    self._mark_same_as(p, twin[0])
                    continue
                batch.add({**paper_to_zotero_item(p), "key": p["url"]})
                out.append(p)
            out.sort(key=papers.index)
            self.skipped_existing = len(papers) - len(out)
            self.total = len(out)
            self.step = "Uploading"
        return out

    def _mark_same_as(self, paper: dict, url: str) -> None:
        """Leave out an item that duplicates another one in the same upload."""
        other = next(p for p in self.papers if p["url"] == url)
        paper.update(existing_key=url, existing_how="Same as another item in this upload",
                     existing_title=other.get("title"), existing_url=url)
        paper["bucket"], paper["reason"] = classify(paper)
        paper["include"] = False

    def _run_upload(self, papers, make_client, upload) -> None:
        def progress(done: int, total: int) -> None:
            with self.lock:
                self.done = done

        def log(line: str) -> None:
            with self.lock:
                self.log.append(line)
        try:
            papers = self._recheck(papers)
            result = upload(papers, make_client(), progress_cb=progress, log_cb=log) if papers \
                else {"uploaded": 0, "failed": 0, "errors": []}
        except Exception as exc:
            result = {"uploaded": 0, "failed": len(papers),
                      "errors": [("", f"{type(exc).__name__}: {exc}")]}
            for p in papers:
                p.setdefault("upload_error", str(exc))
        with self.lock:
            self.upload_result = result
            self.phase = DONE

    # Status ----------------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "step": self.step,
                    "done": self.done, "total": self.total,
                    "log": list(self.log), "fatal_error": self.fatal_error,
                    "upload_result": self.upload_result,
                    "skipped_existing": self.skipped_existing,
                    "stopping": self.stop_requested and self.phase == SCANNING,
                    "paused": not self._running.is_set() and self.phase == SCANNING,
                    "llm_problem": metadata.llm_problem if self.anthropic_key else None}


def _same_work(p: dict, q: dict, t: str, y: Optional[str]) -> bool:
    """Whether two items with the same title are one work: a web page without
    authors next to the paper it describes, or the same year (three words or
    more), or one year unknown (four words or more). Different years are
    different versions, such as a working paper and the published article."""
    words = len(t.split())
    qy = year_of(q.get("year"))
    if different_versions(p.get("item_type"), q.get("item_type")):
        return False
    if not p.get("authors") and p.get("item_type") == "webpage" and words >= 3:
        return True
    if y and qy:
        return y == qy and words >= 3
    return words >= 4


_TEXT_FIELDS = ("title", "year", "journal", "volume", "issue", "pages", "publisher",
                "doi", "isbn", "abstract", "summary", "language")


def _as_text(value) -> Optional[str]:
    """A field value as text, or None. Lookups (the AI especially) sometimes
    return a number, a one-item list, or something unusable."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _as_names(value) -> list[str]:
    """Authors as a list of names; a single string is split on semicolons."""
    if isinstance(value, str):
        value = value.split(";")
    if not isinstance(value, list):
        return []
    return [n for n in (_as_text(v) for v in value if not isinstance(v, list)) if n]


def _tidy(p: dict) -> None:
    """Make every field the expected type, and clean the title, journal, and
    names whichever lookup produced them (HTML tags such as <br> and entities
    such as &amp; turn up). An "article" with no authors, no year, and no DOI
    is usually a journal homepage or issue page; save it as a web page."""
    for field in _TEXT_FIELDS:
        if field in p:
            p[field] = _as_text(p[field])
    if "authors" in p:
        p["authors"] = _as_names(p["authors"])
    for field in ("title", "journal", "publisher"):    # also "&amp;" and the like
        if p.get(field):
            p[field] = metadata.clean_title(p[field])
    if p.get("authors"):
        p["authors"] = [metadata.clean_title(a) or a for a in p["authors"]]
    if p.get("item_type") in ("journalArticle", "preprint", "conferencePaper") \
            and not p.get("authors") and not p.get("year") and not doi_key(p.get("doi")):
        p["item_type"] = "webpage"


def site_name(url: str) -> str:
    """The link's domain without "www.", e.g. "sciencedirect.com"."""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _prepare_for_upload(p: dict) -> None:
    """An item with no title (a link, an inaccessible page) is saved as a web
    page titled with its URL, so Zotero never rejects it for its type."""
    if not p.get("title") and not p.get("slack_file_name"):
        p["title"] = p["url"]
        p["item_type"] = "webpage"
    elif not p.get("title"):
        p["title"] = p["slack_file_name"]
    if p.get("metadata_source") in ("link", "non_paper", "needs_manual", "none") \
            and not p.get("item_type"):
        p["item_type"] = "webpage"
    # A web page with no site name gets its domain (Zotero's Website field).
    if p.get("item_type") == "webpage" and not p.get("journal"):
        p["journal"] = site_name(p["url"])


def _label(p: dict) -> str:
    return {READY: "Ready     ", EXISTING: "In Zotero "}.get(p.get("bucket"), "Review    ")


def _sort_title(p: dict) -> str:
    return (p.get("title") or p.get("slack_file_name") or p["url"]).lower()


# ── The current job (one per server process) ──────────────────────────────────
# Module state, not Streamlit session state: every browser session sees the same
# job, and a refresh can't lose it. Only an app restart clears it.

_current: Optional[Job] = None
_current_lock = threading.Lock()


def current_job() -> Optional[Job]:
    return _current


def start_job(papers: list[dict], anthropic_key: Optional[str],
              audit_log_path: Optional[str] = None, **kwargs) -> bool:
    """Start a scan unless one is already running or uploading."""
    global _current
    with _current_lock:
        if _current is not None and _current.snapshot()["phase"] in (SCANNING, UPLOADING):
            return False
        # A copy as found in the export, for Try again after a failed start.
        original = copy.deepcopy(papers)
        _current = Job(papers, anthropic_key, audit_log_path, **kwargs)
        _current.restart = lambda: Job(copy.deepcopy(original), anthropic_key,
                                       audit_log_path, **kwargs)
        _current.start()
        return True


def retry_job() -> bool:
    """Start a search that failed (e.g. Zotero was unreachable) again, with
    the same export."""
    global _current
    with _current_lock:
        if _current is None or _current.snapshot()["phase"] != FAILED \
                or not getattr(_current, "restart", None):
            return False
        _current = _current.restart()
        _current.start()
        return True


# ── Saved results (for testing) ───────────────────────────────────────────────
# A finished search can be saved to a file and uploaded in step 1 instead of
# an export: the page then shows those results without searching again.

SAVED_MARK = "slack_to_zotero_saved_results"
# Set during a scan or upload, or tied to this computer; worked out again on load.
_NOT_SAVED = ("bucket", "reason", "include", "decision", "was_edited", "zotero_key",
              "upload_error", "existing_key", "existing_how", "existing_url",
              "existing_title", "local_pdf_path", "pdf_size_bytes", "not_scanned",
              "scan_error")


def saved_results(papers: list[dict], label: str) -> bytes:
    kept = [{k: v for k, v in p.items() if k not in _NOT_SAVED} for p in papers]
    return json.dumps({SAVED_MARK: 1, "label": label, "papers": kept},
                      ensure_ascii=False, indent=1).encode()


def read_saved_results(data: bytes) -> Optional[tuple[list[dict], str]]:
    """(papers, label) if `data` is a saved-results file, else None."""
    try:
        saved = json.loads(data)
    except ValueError:
        return None
    if not isinstance(saved, dict) or SAVED_MARK not in saved:
        return None
    papers = saved.get("papers")
    if not isinstance(papers, list):
        return None
    papers = [p for p in papers if isinstance(p, dict) and isinstance(p.get("url"), str)]
    return papers, saved.get("label") or "Saved results"


def load_saved_job(papers: list[dict], label: str, **kwargs) -> bool:
    """Show saved results as a finished search. Each item is checked against
    Zotero again and sorted again, but nothing is looked up."""
    return start_job(papers, None, enrich=lambda *a: {},
                     title_search=lambda *a, **k: None,
                     source={"label": label}, **kwargs)


def discard_job() -> None:
    """Stop a running search and drop it and its results. Its threads finish
    the items in progress in the background and are then gone."""
    global _current
    with _current_lock:
        if _current is not None and _current.snapshot()["phase"] == SCANNING:
            _current.stop()
            _current.delete_files()
            _current = None


def clear_job() -> None:
    """Discard the finished job's results. A running job is left alone."""
    global _current
    with _current_lock:
        if _current is not None and _current.snapshot()["phase"] in (SCANNED, DONE, FAILED):
            _current.delete_files()
            _current = None
