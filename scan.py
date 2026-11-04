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

READY, REVIEW = "ready", "review"


def classify(paper: dict) -> tuple[str, str]:
    """(bucket, reason) for an enriched paper. Review items carry a short reason
    shown in the app; Ready items have none."""
    src = paper.get("metadata_source")
    if paper.get("not_scanned"):
        return REVIEW, "Not scanned"
    if paper.get("scan_error"):
        return REVIEW, "Error during scan"
    if src in ("crossref_doi", "translator"):
        if paper.get("title"):
            return READY, ""
        return REVIEW, "No title found"
    if src == "llm":
        if paper.get("low_context"):
            return REVIEW, "Little text to read"
        return READY, ""
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


# ── The job ───────────────────────────────────────────────────────────────────

SCANNING, SCANNED, UPLOADING, DONE = "scanning", "scanned", "uploading", "done"
WORKERS = 4


class Job:
    """One scan of one export, then its upload. Thread-safe: the background
    thread mutates it under `lock`; the page reads it under `lock`."""

    def __init__(self, papers: list[dict], anthropic_key: Optional[str],
                 audit_log_path: Optional[str] = None,
                 enrich: Callable[..., dict] = metadata.enrich_paper):
        self.papers = papers
        self.anthropic_key = anthropic_key
        self.audit_log_path = audit_log_path
        self._enrich = enrich
        self.lock = threading.RLock()
        self.phase = SCANNING
        self.total = len(papers)
        self.done = 0
        self.log: deque[str] = deque(maxlen=1000)
        self.stop_requested = False
        self.started_at = time.time()
        self.upload_result: Optional[dict] = None
        self.fatal_error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # Scan ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_scan, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self.lock:
            self.stop_requested = True

    def _enrich_one(self, paper: dict) -> dict:
        if self.stop_requested:
            return {"metadata_source": "none", "not_scanned": True}
        try:
            return self._enrich(paper, self.anthropic_key, self.audit_log_path)
        except Exception as exc:
            return {"metadata_source": "none", "scan_error": f"{type(exc).__name__}: {exc}"}

    def _run_scan(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(self._enrich_one, p): p for p in self.papers}
                for fut in as_completed(futures):
                    paper = futures[fut]
                    fields = fut.result()
                    with self.lock:
                        paper.update(fields)
                        bucket, reason = classify(paper)
                        paper["bucket"], paper["reason"] = bucket, reason
                        paper["include"] = bucket == READY
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
            self.phase = SCANNED

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
                if p["url"] == url:
                    p["include"] = include
                    return

    def selected(self) -> list[dict]:
        with self.lock:
            return [p for p in self.papers
                    if p.get("include") and not p.get("zotero_key")]

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
            self.total, self.done = len(papers), 0
        if upload is None:
            import zotero_client
            upload = zotero_client.upload_papers
        self._thread = threading.Thread(
            target=self._run_upload, args=(papers, make_client, upload), daemon=True)
        self._thread.start()

    def _run_upload(self, papers, make_client, upload) -> None:
        def progress(done: int, total: int) -> None:
            with self.lock:
                self.done = done
        try:
            result = upload(papers, make_client(), progress_cb=progress)
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
            return {"phase": self.phase, "done": self.done, "total": self.total,
                    "log": list(self.log), "fatal_error": self.fatal_error,
                    "upload_result": self.upload_result,
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
    return "Ready " if p.get("bucket") == READY else "Review"


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
        if _current is not None and _current.snapshot()["phase"] in (SCANNED, DONE):
            _current = None
