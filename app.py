"""
app.py — Slack to Zotero.

One page with three steps that stay on the page: the Slack export, the scan
(with the results to review), and the upload. A finished step shrinks to a
summary; nothing is removed. The scan runs as a server-side job (scan.Job)
shared by every session, so a refresh shows the same page.
"""

from __future__ import annotations

import tempfile
import time

import streamlit as st

import config
import scan
import zotero_client
import zotero_index

st.set_page_config(page_title="Slack to Zotero", layout="wide")

DONE_STATE, CURRENT, LATER = "done", "current", "later"


# ── Shared job ────────────────────────────────────────────────────────────────

def _load_index() -> zotero_index.LibraryIndex:
    return zotero_index.load(zotero_client.make_zotero_client(),
                             config.zotero_library_type(), config.zotero_library_id(),
                             config.zotero_collection_key())


def _refresh_index(index: zotero_index.LibraryIndex) -> None:
    zotero_index.refresh(index, zotero_client.make_zotero_client(),
                         config.zotero_collection_key())


def start_job(papers: list[dict], source: dict) -> bool:
    return scan.start_job(papers, config.anthropic_api_key(), str(config.LLM_AUDIT_PATH),
                          load_index=_load_index, refresh_index=_refresh_index,
                          source=source)


def clear_job() -> None:
    scan.clear_job()
    st.session_state.pop("confirm_start_over", None)


def zotero_ready() -> bool:
    return bool(config.zotero_api_key() and config.zotero_library_id())


# ── Layout helpers ────────────────────────────────────────────────────────────

def step_header(number: int, title: str, state: str) -> None:
    text = f"{number}  {title}"
    if state == LATER:
        st.subheader(f":gray[{text}]", anchor=False)
        return
    left, right = st.columns([6, 1])
    left.subheader(text, anchor=False)
    if state == DONE_STATE:
        right.markdown(":gray[Done]")


def _minutes(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60} s" if seconds % 60 else f"{seconds // 60} min"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ── Step 1: Slack export ──────────────────────────────────────────────────────

def step_export(job: scan.Job | None) -> None:
    if job is not None:
        step_header(1, "Slack export", DONE_STATE)
        src = job.source
        parts = [src.get("label") or "Slack export"]
        if src.get("channel"):
            parts.append(f"#{src['channel']}")
        if src.get("files"):
            parts.append(_plural(src["files"], "day-file"))
        parts.append(f"{job.n_sources} links and documents")
        st.caption(" · ".join(parts))
        if job.snapshot()["phase"] in (scan.SCANNED, scan.DONE, scan.FAILED):
            start_over_control()
        return

    step_header(1, "Slack export", CURRENT)
    files = st.file_uploader(
        "Slack export", type=["zip", "json"], accept_multiple_files=True,
        label_visibility="collapsed",
        help="A .zip export, or a channel's daily .json files.")

    channels: list[str] = []
    if files:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
                channels = sorted(export.channels)
        except scan.ExportError as exc:
            st.error(str(exc))
            return

    channel = st.selectbox("Channel", channels) if len(channels) > 1 else None

    if not config.anthropic_api_key():
        st.caption("Anthropic key not set. Items that need the AI step will go to Review.")

    if st.button("Scan", type="primary", disabled=not files or not zotero_ready()):
        with tempfile.TemporaryDirectory() as tmp:
            export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
            chosen = channel if channel is not None else next(iter(export.channels))
            day_files = export.channels[chosen]
            papers = scan.collect_papers(day_files, export.user_map)
        if not papers:
            st.info("No links or documents found in this export.")
            return
        label = files[0].name if len(files) == 1 else _plural(len(files), "file")
        source = {"label": label, "files": len(day_files), "channel": chosen or None}
        if not start_job(papers, source):
            st.warning("A scan or upload is already running.")
        st.rerun()


def start_over_control() -> None:
    if not st.session_state.get("confirm_start_over"):
        if st.button("Start over", type="tertiary", key="start_over"):
            st.session_state.confirm_start_over = True
            st.rerun()
        return
    st.caption("This clears the scan results from the page. Items already uploaded stay in Zotero.")
    c1, c2, _ = st.columns([1, 1, 6])
    if c1.button("Clear results", type="primary", key="start_over_confirm"):
        clear_job()
        st.rerun()
    if c2.button("Cancel", type="tertiary", key="start_over_cancel"):
        st.session_state.pop("confirm_start_over", None)
        st.rerun()


# ── Progress (scanning or uploading, shown inside its step) ───────────────────

@st.fragment(run_every=1.0)
def progress(job: scan.Job, phase: str) -> None:
    snap = job.snapshot()
    if snap["phase"] != phase:
        st.rerun()                       # finished: redraw the whole page
    step = snap["step"]
    if step in ("Scanning", "Uploading"):
        total = max(snap["total"], 1)
        st.progress(min(snap["done"] / total, 1.0),
                    text=f"{step} {snap['done']} of {snap['total']}")
    else:
        st.progress(0, text=step)
    if phase == scan.SCANNING:
        if snap["stopping"]:
            st.caption("Stopping after the current items.")
        elif st.button("Stop"):
            job.stop()
    if snap["log"]:
        with st.expander("Log"):
            st.code("\n".join(reversed(snap["log"][-300:])), language=None)


# ── Step 2: Scan and review ───────────────────────────────────────────────────

def _on_edit(key: str, urls: list[str]) -> None:
    job = scan.current_job()
    if job is None:
        return
    for row, change in st.session_state[key]["edited_rows"].items():
        if "Upload" in change:
            job.set_include(urls[int(row)], bool(change["Upload"]))


def _short_authors(authors: list[str]) -> str:
    """Last names, "et al." after three: "Borg, Passaro, Hermo et al."."""
    last = [a.split(",")[0].strip() for a in authors if a and a.strip()]
    return ", ".join(last[:3]) + (" et al." if len(last) > 3 else "")


def _status(p: dict) -> str:
    if p.get("zotero_key"):
        return "Uploaded"
    if p.get("upload_error"):
        return "Failed"
    return ""


def _table(items: list[dict], key: str, review: bool, show_status: bool,
           locked: bool) -> None:
    rows = []
    for p in items:
        row = {"Upload": bool(p.get("include"))}
        if show_status:
            row["Status"] = _status(p)
        if review:
            row["Reason"] = p.get("reason", "")
        row["Title"] = p.get("title") or p.get("slack_file_name") or p["url"]
        row["Authors"] = _short_authors(p.get("authors") or [])
        if review:
            row["Summary"] = p.get("summary") or ""
        else:
            row["Year"] = p.get("year") or ""
            row["Journal"] = p.get("journal") or ""
        row["Link"] = p["url"]
        rows.append(row)
    columns = list(rows[0]) if rows else []
    st.data_editor(
        rows, key=key, hide_index=True,
        disabled=columns if locked else [c for c in columns if c != "Upload"],
        column_config={
            "Upload": st.column_config.CheckboxColumn(width=70),
            "Status": st.column_config.TextColumn(width=90),
            "Reason": st.column_config.TextColumn(width=170),
            "Title": st.column_config.TextColumn(width="medium" if review else "large"),
            "Authors": st.column_config.TextColumn(width=180),
            "Year": st.column_config.TextColumn(width=50),
            "Journal": st.column_config.TextColumn(width=150),
            "Summary": st.column_config.TextColumn(width=220),
            "Link": st.column_config.LinkColumn(display_text="Open", width=50),
        },
        on_change=_on_edit, args=(key, [p["url"] for p in items]),
    )


def step_scan(job: scan.Job | None) -> None:
    if job is None:
        step_header(2, "Scan", LATER)
        return
    snap = job.snapshot()
    phase = snap["phase"]

    if phase == scan.SCANNING:
        step_header(2, "Scan", CURRENT)
        progress(job, scan.SCANNING)
        return

    if phase == scan.FAILED:
        step_header(2, "Scan", CURRENT)
        st.error(snap["fatal_error"])
        return

    ready, review = job.items(scan.READY), job.items(scan.REVIEW)
    existing = job.items(scan.EXISTING)
    step_header(2, "Scan", DONE_STATE)
    took = _minutes((job.finished_at or time.time()) - job.started_at)
    st.caption(f"Scanned {job.n_sources} in {took} · {len(ready)} ready · "
               f"{len(review)} review · {len(existing)} already in Zotero")
    if snap["fatal_error"]:
        st.error("The scan stopped because of an error. Items it didn't reach are in Review.")
        with st.expander("Details"):
            st.code(snap["fatal_error"], language=None)

    uploaded_any = any(_status(p) for p in ready + review)
    locked = phase == scan.UPLOADING

    st.markdown(f"**Ready ({len(ready)})**")
    if ready:
        st.caption("Metadata found. Clear a box to leave an item out.")
        _table(ready, "t_ready", review=False, show_status=uploaded_any, locked=locked)

    st.markdown(f"**Review ({len(review)})**")
    if review:
        st.caption("Could not be identified, or probably not a source. "
                   "Only selected items are uploaded.")
        _table(review, "t_review", review=True, show_status=uploaded_any, locked=locked)

    if existing:
        with st.expander(f"Already in Zotero ({len(existing)})"):
            st.dataframe(
                [{"Title": p.get("existing_title") or p.get("title") or p["url"],
                  "Matched by": p.get("reason", ""),
                  "In Zotero": p.get("existing_url") or "", "Link": p["url"]}
                 for p in existing],
                hide_index=True,
                column_config={
                    "Title": st.column_config.TextColumn(width="large"),
                    "Matched by": st.column_config.TextColumn(width=100),
                    "In Zotero": st.column_config.LinkColumn(display_text="Open", width=80),
                    "Link": st.column_config.LinkColumn(display_text="Open", width=50),
                })


# ── Step 3: Upload ────────────────────────────────────────────────────────────

def step_upload(job: scan.Job | None) -> None:
    title = "Upload to Zotero"
    if job is None or job.snapshot()["phase"] in (scan.SCANNING, scan.FAILED):
        step_header(3, title, LATER)
        return
    snap = job.snapshot()
    phase = snap["phase"]

    if phase == scan.UPLOADING:
        step_header(3, title, CURRENT)
        progress(job, scan.UPLOADING)
        return

    uploaded = [p for p in job.papers if p.get("zotero_key")]
    failed = [p for p in job.papers
              if p.get("upload_error") and not p.get("zotero_key")]
    n_selected = len(job.selected())
    has_uploaded = phase == scan.DONE

    step_header(3, title, DONE_STATE if has_uploaded and not n_selected else CURRENT)

    if has_uploaded:
        line = f"Uploaded {len(uploaded)}"
        if failed:
            line += f" · {len(failed)} failed"
        skipped = snap["skipped_existing"]
        if skipped:
            line += f" · {skipped} skipped because they were added to Zotero after the scan"
        st.caption(line)
        if failed:
            st.dataframe([{"Title": p.get("title") or p["url"], "Error": p["upload_error"]}
                          for p in failed], hide_index=True)
        warnings = [msg for _url, msg in (snap["upload_result"] or {}).get("errors", [])
                    if not msg.startswith(("upload failed", "batch upload failed"))]
        if warnings:
            with st.expander(f"Warnings ({len(warnings)})"):
                st.write("These items were uploaded without some of their files or notes.")
                st.code("\n".join(warnings), language=None)

    if n_selected:
        what = "more selected" if has_uploaded else "selected"
        st.caption(f"{n_selected} {what}.")
        label = "Retry failed" if failed and n_selected == len(failed) else \
            f"Upload {n_selected} to Zotero"
        if st.button(label, type="primary", disabled=not zotero_ready()):
            job.start_upload(zotero_client.make_zotero_client)
            st.rerun()
    elif not has_uploaded:
        st.caption("Nothing selected to upload.")

    if has_uploaded:
        if st.button("New scan", type="tertiary" if n_selected else "primary",
                     key="new_scan"):
            clear_job()
            st.rerun()


# ── Settings ──────────────────────────────────────────────────────────────────

def render_settings() -> None:
    with st.expander("Settings"):
        lib = config.zotero_library_id()
        st.text(f"Zotero library   {config.zotero_library_type()} {lib}" if lib
                else "Zotero library   not set")
        st.text(f"Collection       {config.zotero_collection_key() or 'library root'}")
        st.text(f"Anthropic key    {'set' if config.anthropic_api_key() else 'not set'}")
        st.text(f"Model            {config.llm_model()}")


# ── Page ──────────────────────────────────────────────────────────────────────

def main() -> None:
    st.title("Slack to Zotero", anchor=False)
    if not zotero_ready():
        st.warning("Zotero is not configured.")

    job = scan.current_job()
    step_export(job)
    st.divider()
    step_scan(job)
    st.divider()
    step_upload(job)
    st.divider()
    render_settings()


main()
