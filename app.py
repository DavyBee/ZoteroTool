"""
app.py — Slack to Zotero.

One page, two steps: scan a Slack export, then review and upload to Zotero.
The scan runs as a server-side job (scan.Job) shared by every session, so a
refresh, a closed tab, or a second visitor all see the same job.
"""

from __future__ import annotations

import tempfile

import streamlit as st

import config
import scan
import zotero_client
import zotero_index

st.set_page_config(page_title="Slack to Zotero", layout="wide")


# ── Shared job ────────────────────────────────────────────────────────────────

def current_job() -> scan.Job | None:
    return scan.current_job()


def _load_index() -> zotero_index.LibraryIndex:
    return zotero_index.load(zotero_client.make_zotero_client(),
                             config.zotero_library_type(), config.zotero_library_id(),
                             config.zotero_collection_key())


def _refresh_index(index: zotero_index.LibraryIndex) -> None:
    zotero_index.refresh(index, zotero_client.make_zotero_client(),
                         config.zotero_collection_key())


def start_job(papers: list[dict]) -> bool:
    return scan.start_job(papers, config.anthropic_api_key(), str(config.LLM_AUDIT_PATH),
                          load_index=_load_index, refresh_index=_refresh_index)


def clear_job() -> None:
    scan.clear_job()


def zotero_ready() -> bool:
    return bool(config.zotero_api_key() and config.zotero_library_id())


# ── Step 1: scan ──────────────────────────────────────────────────────────────

def render_start() -> None:
    files = st.file_uploader(
        "Slack export", type=["zip", "json"], accept_multiple_files=True,
        help="A .zip export, or a channel's daily .json files.")

    export_channels: list[str] = []
    if files:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
                export_channels = sorted(export.channels)
        except scan.ExportError as exc:
            st.error(str(exc))
            return

    channel = None
    if len(export_channels) > 1:
        channel = st.selectbox("Channel", export_channels)

    if not config.anthropic_api_key():
        st.caption("Anthropic key not set. Items that need the AI step will go to Review.")

    if st.button("Scan", type="primary", disabled=not files or not zotero_ready()):
        with tempfile.TemporaryDirectory() as tmp:
            export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
            chosen = channel if channel is not None else next(iter(export.channels))
            papers = scan.collect_papers(export.channels[chosen], export.user_map)
        if not papers:
            st.info("No links or documents found in this export.")
            return
        if not start_job(papers):
            st.warning("A scan or upload is already running.")
        st.rerun()


# ── Progress (scanning / uploading) ───────────────────────────────────────────

@st.fragment(run_every=1.0)
def render_progress(job: scan.Job) -> None:
    snap = job.snapshot()
    if snap["phase"] not in (scan.SCANNING, scan.UPLOADING):
        st.rerun()                       # finished: redraw the whole page
    step = snap["step"]
    if step in ("Scanning", "Uploading"):
        total = max(snap["total"], 1)
        st.progress(min(snap["done"] / total, 1.0),
                    text=f"{step} {snap['done']} of {snap['total']}")
    else:
        st.progress(0, text=step)
    if snap["phase"] == scan.SCANNING:
        if snap["stopping"]:
            st.caption("Stopping after the current items.")
        elif st.button("Stop"):
            job.stop()
    if snap["log"]:
        with st.expander("Log"):
            st.code("\n".join(reversed(snap["log"][-300:])), language=None)


# ── Step 2: review and upload ─────────────────────────────────────────────────

def _on_edit(key: str, urls: list[str]) -> None:
    job = current_job()
    if job is None:
        return
    for row, change in st.session_state[key]["edited_rows"].items():
        if "Upload" in change:
            job.set_include(urls[int(row)], bool(change["Upload"]))


def _short_authors(authors: list[str]) -> str:
    """Last names, "et al." after three: "Borg, Passaro, Hermo et al."."""
    last = [a.split(",")[0].strip() for a in authors if a and a.strip()]
    return ", ".join(last[:3]) + (" et al." if len(last) > 3 else "")


def _table(job: scan.Job, items: list[dict], key: str, review: bool) -> None:
    rows = []
    for p in items:
        row = {"Upload": bool(p.get("include"))}
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
    st.data_editor(
        rows, key=key, hide_index=True,
        disabled=[c for c in (rows[0] if rows else {}) if c != "Upload"],
        column_config={
            "Upload": st.column_config.CheckboxColumn(width=70),
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


def render_results(job: scan.Job) -> None:
    ready, review = job.items(scan.READY), job.items(scan.REVIEW)

    st.subheader(f"Ready ({len(ready)})", anchor=False)
    if ready:
        st.caption("Metadata found. Clear a box to leave an item out.")
        _table(job, ready, "t_ready", review=False)

    st.subheader(f"Review ({len(review)})", anchor=False)
    if review:
        st.caption("Low confidence or likely not a source. "
                   "Only selected items are uploaded.")
        _table(job, review, "t_review", review=True)

    existing = job.items(scan.EXISTING)
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

    n = len(job.selected())
    if not ready and not review:
        st.write("Nothing new to upload.")
        if st.button("Start over", type="primary"):
            clear_job()
            st.rerun()
        return
    c1, c2 = st.columns([1, 5])
    if c1.button(f"Upload {n} to Zotero", type="primary",
                 disabled=n == 0 or not zotero_ready()):
        job.start_upload(zotero_client.make_zotero_client)
        st.rerun()
    if c2.button("Start over", type="tertiary"):
        clear_job()
        st.rerun()


def render_done(job: scan.Job) -> None:
    r = job.snapshot()["upload_result"] or {}
    failed = [p for p in job.papers if p.get("include") and p.get("upload_error")]
    st.subheader("Upload finished", anchor=False)
    st.write(f"Uploaded {r.get('uploaded', 0)} items to Zotero.")
    skipped = job.snapshot()["skipped_existing"]
    if skipped:
        st.write(f"Skipped {skipped} that were added to Zotero after the scan.")
    if failed:
        st.error(f"{len(failed)} failed.")
        st.dataframe(
            [{"Title": p.get("title") or p["url"], "Error": p["upload_error"]}
             for p in failed],
            hide_index=True)
    other = [msg for url, msg in r.get("errors", [])
             if not msg.startswith(("upload failed", "batch upload failed"))]
    if other:
        with st.expander(f"Warnings ({len(other)})"):
            st.write("These items were uploaded without some of their files or notes.")
            st.code("\n".join(other), language=None)

    if failed:
        c1, c2 = st.columns([1, 5])
        if c1.button("Retry failed", type="primary"):
            job.start_upload(zotero_client.make_zotero_client)
            st.rerun()
        new_scan = c2.button("New scan", type="tertiary")
    else:
        new_scan = st.button("New scan", type="primary")
    if new_scan:
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

    job = current_job()
    if job is None:
        render_start()
    else:
        snap = job.snapshot()
        if snap["phase"] == scan.FAILED:
            st.error(snap["fatal_error"])
            if st.button("Start over", type="primary"):
                clear_job()
                st.rerun()
            render_settings()
            return
        if snap["fatal_error"]:
            st.error("The scan stopped because of an error.")
            with st.expander("Details"):
                st.code(snap["fatal_error"], language=None)
        if snap["phase"] in (scan.SCANNING, scan.UPLOADING):
            render_progress(job)
        elif snap["phase"] == scan.SCANNED:
            render_results(job)
        else:
            render_done(job)

    render_settings()


main()
