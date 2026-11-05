"""
scan.py — The scan → review → upload job behind the app's single page.

A Job runs in a background thread on the server, not in the browser session,
so refreshing or closing the page doesn't stop it or lose its results: the page
just reattaches to the current job. Nothing is written to Zotero until upload.
Results live in server memory until the next scan or an app restart.

No Streamlit imports here, so the whole flow is testable without the UI.
"""

from __future__ import annotations

import os
import re
import threading
import time
import traceback
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import metadata
import slack_parser
from database import _new_paper, normalize_url
from url_expander import is_tweet_url
from zotero_index import (LibraryIndex, doi_key, title_key, titles_match,
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
            papers[key] = _new_paper(url, doi)
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
    """(bucket, reason) for an enriched paper. Review items carry a short reason
    shown in the app; Ready items have none; Existing items say what matched."""
    src = paper.get("metadata_source")
    if paper.get("existing_key"):
        return EXISTING, paper.get("existing_how", "")
    if paper.get("not_scanned"):
        return REVIEW, "Not scanned"
    if paper.get("scan_error"):
        return REVIEW, "Error during scan"
    if src in FOUND_SOURCES and paper.get("title") \
            and paper.get("hint_mismatch"):
        return REVIEW, "Title differs from Slack post"
    if src in FOUND_SOURCES and paper.get("title") \
            and paper.get("item_type") == "webpage" and not paper.get("authors"):
        return REVIEW, "Web page, not a paper"
    if src in ("crossref_doi", "translator", "citation_tags"):
        if paper.get("title"):
            return READY, ""
        return REVIEW, "No title found"
    if src == "llm":
        if paper.get("low_context") and not paper.get("hint_confirmed"):
            return REVIEW, "Little text to read"
        return READY, ""
    if src == "slack_hint":
        return REVIEW, "Title from Slack post only"
    if src == "link":
        return REVIEW, "Tweet" if is_tweet_url(paper["url"]) else "Web page, not a paper"
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

    If enrichment found metadata, the typed title confirms it or flags it for
    review. If enrichment found nothing, the typed title is searched on CrossRef;
    failing that it becomes the item's title, and the item goes to Review."""
    hints = paper.get("slack_hints") or {}
    hint = hints.get("title")
    if not hint:
        return
    if paper.get("metadata_source") in FOUND_SOURCES \
            and paper.get("title"):
        if titles_match(paper["title"], hint):
            paper["hint_confirmed"] = True
        else:
            paper["hint_mismatch"] = True
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
                 title_search: Callable[..., Optional[tuple]] = metadata.search_crossref_title):
        self.papers = papers
        self.anthropic_key = anthropic_key
        self.audit_log_path = audit_log_path
        self._enrich = enrich
        self._load_index = load_index
        self._refresh_index = refresh_index
        self._title_search = title_search
        self.index: Optional[LibraryIndex] = None
        self.lock = threading.RLock()
        self.phase = SCANNING
        self.step = "Reading Zotero library" if load_index else "Scanning"
        self.total = len(papers)
        self.done = 0
        self.log: deque[str] = deque(maxlen=1000)
        self.stop_requested = False
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

    def _mark_existing(self, paper: dict, hit: tuple[str, str]) -> None:
        paper["existing_key"], paper["existing_how"] = hit
        if self.index is not None:
            paper["existing_url"] = self.index.item_url(hit[0])
            paper["existing_title"] = self.index.titles.get(hit[0])
        paper["bucket"], paper["reason"] = classify(paper)
        paper["include"] = False

    def _enrich_one(self, paper: dict) -> dict:
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
        if self._load_index is not None:
            try:
                index = self._load_index()
            except Exception as exc:
                with self.lock:
                    self.fatal_error = f"Could not read the Zotero library. {exc}"
                    self.phase = FAILED
                return
            with self.lock:
                self.index = index
                self.step = "Scanning"

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

        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(self._enrich_one, p): p for p in to_scan}
                for fut in as_completed(futures):
                    paper = futures[fut]
                    fields = fut.result()
                    with self.lock:
                        paper.update(fields)
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
            self._merge_duplicates()
            self.phase = SCANNED

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
            if target is None and t and len(t.split()) >= 4:
                # Same title, and the years agree or one is unknown (a landing
                # page and the article it describes). Different known years
                # are different editions and are kept apart.
                target = next((q for q in by_title.get(t, [])
                               if not y or not year_of(q.get("year"))
                               or year_of(q.get("year")) == y), None)
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

    def set_include(self, url: str, include: bool) -> None:
        with self.lock:
            for p in self.papers:
                if p["url"] == url and p.get("bucket") != EXISTING:
                    p["include"] = include
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
        if upload is None:
            import zotero_client
            upload = zotero_client.upload_papers
        self._thread = threading.Thread(
            target=self._run_upload, args=(papers, make_client, upload), daemon=True)
        self._thread.start()

    def _recheck(self, papers: list[dict]) -> list[dict]:
        """Drop anything added to Zotero since the scan read it."""
        if self._refresh_index is None or self.index is None:
            return papers
        try:
            self._refresh_index(self.index)
        except Exception:
            return papers                  # the upload itself will report it
        out = []
        with self.lock:
            for p in papers:
                hit = self.index.match(p)
                if hit:
                    self._mark_existing(p, hit)
                else:
                    out.append(p)
            self.skipped_existing = len(papers) - len(out)
            self.total = len(out)
            self.step = "Uploading"
        return out

    def _run_upload(self, papers, make_client, upload) -> None:
        def progress(done: int, total: int) -> None:
            with self.lock:
                self.done = done
        papers = self._recheck(papers)
        try:
            result = upload(papers, make_client(), progress_cb=progress) if papers \
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
                    "stopping": self.stop_requested and self.phase == SCANNING}


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
        _current = Job(papers, anthropic_key, audit_log_path, **kwargs)
        _current.start()
        return True


def clear_job() -> None:
    """Discard the finished job's results. A running job is left alone."""
    global _current
    with _current_lock:
        if _current is not None and _current.snapshot()["phase"] in (SCANNED, DONE, FAILED):
            _current = None
