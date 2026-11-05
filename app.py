"""
app.py — Slack to Zotero.

One page with three steps that stay on the page: the Slack export, the scan
(with the results to review), and the upload. A finished step shrinks to a
summary; nothing is removed. The scan runs as a server-side job (scan.Job)
shared by every session, so a refresh shows the same page.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import zlib

import streamlit as st

# After a push, Streamlit Cloud re-runs this file with the new code but keeps
# the old versions of the modules below loaded, so the two disagree until the
# app is rebooted. Drop the loaded copies when any file has changed since they
# were loaded; they are then imported fresh. (Like a restart, this clears the
# current scan.)
_LOCAL_MODULES = ("records", "config", "http_fetch", "slack_parser", "pdf_storage",
                  "zotero_index", "metadata", "zotero_client", "scan")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _code_stamp() -> tuple:
    return tuple(os.path.getmtime(os.path.join(_HERE, f"{name}.py"))
                 for name in _LOCAL_MODULES)


if getattr(sys, "_slack_to_zotero_code", None) != _code_stamp():
    # Stop a scan still running on the old code, or its threads keep going
    # unseen in the background.
    _old_job = getattr(sys.modules.get("scan"), "_current", None)
    if _old_job is not None and hasattr(_old_job, "stop"):
        _old_job.stop()
    for _name in _LOCAL_MODULES:
        sys.modules.pop(_name, None)
    sys._slack_to_zotero_code = _code_stamp()

import config  # noqa: E402
import scan  # noqa: E402
import zotero_client  # noqa: E402
import zotero_index  # noqa: E402

st.set_page_config(page_title="Slack to Zotero", layout="wide")

DONE_STATE, CURRENT = "done", "current"


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

# Steps not reached yet are faded, with a line saying what they wait for.
_STYLE = """
<style>
[class*="st-key-waiting-"] h3 { opacity: 0.4; }
</style>
"""


def step_header(number: int, title: str, state: str) -> None:
    """A step's heading. A finished step's summary line starts with "Done"
    (see summary()); a step not reached yet uses waiting_step() instead."""
    st.subheader(f"{number}. {title}", anchor=False)


def waiting_step(number: int, title: str, waiting_for: str) -> None:
    with st.container(key=f"waiting-{number}"):
        st.subheader(f"{number}. {title}", anchor=False)
        st.caption(waiting_for)


def summary(parts: list[str], done: bool) -> None:
    st.caption(" · ".join((["Done"] if done else []) + parts))


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
        summary([job.source.get("label") or "Slack export"], done=True)
        if job.snapshot()["phase"] in (scan.SCANNED, scan.DONE, scan.FAILED):
            start_over_control()
        return

    step_header(1, "Slack export", CURRENT)
    files = st.file_uploader(
        "Slack export", type=["zip", "json"], accept_multiple_files=True,
        label_visibility="collapsed")
    st.caption("A Slack export (.zip), or the channel's daily .json files.")

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

    if st.button("Search for sources", type="primary",
                 disabled=not files or not zotero_ready()):
        with tempfile.TemporaryDirectory() as tmp:
            export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
            chosen = channel if channel is not None else next(iter(export.channels))
            day_files = export.channels[chosen]
            papers = scan.collect_papers(day_files, export.user_map)
        if not papers:
            st.info("No links or documents found in this export.")
            return
        label = files[0].name if len(files) == 1 else _plural(len(files), "file")
        source = {"label": label}
        if not start_job(papers, source):
            st.warning("A search or upload is already running.")
        st.rerun()


def start_over_control() -> None:
    if not st.session_state.get("confirm_start_over"):
        if st.button("Start over", type="tertiary", key="start_over"):
            st.session_state.confirm_start_over = True
            st.rerun()
        return
    st.caption("This clears the search results from the page. Items already uploaded stay in Zotero.")
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
    if step in ("Searching", "Uploading"):
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
        row = {"Upload": bool(p.get("include")), "Link": p["url"]}
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


# ── Review cards ──────────────────────────────────────────────────────────────

_DECISION_LABELS = {"correct": "Correct", "edited": "Edited"}
_ITEM_TYPES = ["journalArticle", "report", "preprint", "book", "bookSection",
               "conferencePaper", "thesis", "webpage", "blogPost", "newspaperArticle"]


def _md(text: str) -> str:
    """Escape Markdown so a title like "*Hope* in 2024" shows as written."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~<$])", r"\\\1", text)


def _go_to(url: str) -> None:
    st.session_state.card_url = url
    st.session_state.pop("editing_url", None)


def _decide(job: scan.Job, items: list[dict], i: int, decision: str,
            fields: dict | None = None) -> None:
    job.decide(items[i]["url"], decision, fields)
    # Move on to the next item without a decision, if there is one.
    later = [q for q in items[i + 1:] + items[:i] if not q.get("decision")]
    _go_to(later[0]["url"] if later else items[i]["url"])
    st.rerun()


def review_cards(job: scan.Job, all_items: list[dict], locked: bool) -> None:
    undecided = [q for q in all_items if not q.get("decision")]
    marked = [q for q in all_items if q.get("decision") in ("correct", "edited")]
    removed = [q for q in all_items if q.get("decision") == "removed"]
    if undecided:
        _card(job, undecided, locked)
    else:
        st.caption("All items reviewed.")
    _decided_list("Marked for upload", marked, job, locked)
    _decided_list("Removed", removed, job, locked)


def _decided_list(label: str, items: list[dict], job: scan.Job, locked: bool) -> None:
    """A collapsed list of decided items, each with Undo to send it back."""
    if not items:
        return
    with st.expander(f"{label} ({len(items)})"):
        for q in items:
            c1, c2 = st.columns([6, 1], vertical_alignment="center")
            c1.write(q.get("title") or q.get("slack_file_name") or q["url"])
            frozen = locked or bool(q.get("zotero_key"))
            if c2.button("Undo", key=f"undo_{zlib.crc32(q['url'].encode())}",
                         disabled=frozen):
                job.decide(q["url"], None)
                _go_to(q["url"])
                st.rerun()


def _card(job: scan.Job, items: list[dict], locked: bool) -> None:
    urls = [p["url"] for p in items]
    current = st.session_state.get("card_url")
    i = urls.index(current) if current in urls else 0
    p = items[i]

    nav = st.container(horizontal=True, vertical_alignment="center")
    if nav.button("Previous", disabled=i == 0, key="card_prev"):
        _go_to(urls[i - 1])
        st.rerun()
    if nav.button("Next", disabled=i == len(items) - 1, key="card_next"):
        _go_to(urls[i + 1])
        st.rerun()
    nav.caption(f"{i + 1} of {len(items)} · {len(items)} left to review")

    with st.container(border=True):
        badges = st.container(horizontal=True)
        with badges:
            st.badge(p.get("reason") or "Review", color="orange")
            if p.get("decision"):
                st.badge(_DECISION_LABELS[p["decision"]], color="green")
            if p.get("zotero_key"):
                st.badge("Uploaded", color="blue")
        _field("Title", p.get("title") or p.get("slack_file_name"))
        _field("Authors", _all_authors(p.get("authors") or []))
        _field("Year", p.get("year"))
        _field("Website" if p.get("item_type") == "webpage" else "Journal",
               p.get("journal") or (scan.site_name(p["url"])
                                    if p.get("item_type") == "webpage" else None))
        st.markdown(f"**Link:** [{_md(_short_link(p['url']))}]({p['url']})")
        _field("Link summary", p.get("summary"))
        comments = p.get("comments") or []
        if comments:
            first = comments[0]
            text = (first.get("text") or "").strip()
            author = _md(first.get("author") or "")
            body = (_md(text[:400] + ("..." if len(text) > 400 else "")) if text
                    else ":gray[only the link, no text]")
            st.markdown(f"**Original Slack message:** {author}: {body}")

        frozen = locked or bool(p.get("zotero_key"))
        if st.session_state.get("editing_url") == p["url"] and not frozen:
            _edit_form(job, items, i)
            return
        actions = st.container(horizontal=True)
        if actions.button("Correct", type="primary", disabled=frozen, key="card_correct"):
            _decide(job, items, i, "correct")
        if actions.button("Edit", disabled=frozen, key="card_edit"):
            st.session_state.editing_url = p["url"]
            st.rerun()
        if actions.button("Remove", disabled=frozen, key="card_remove"):
            _decide(job, items, i, "removed")


def _field(label: str, value) -> None:
    shown = _md(str(value)) if value else ":gray[Not found]"
    st.markdown(f"**{label}:** {shown}")


def _all_authors(authors: list[str]) -> str:
    names = [a.strip() for a in authors if a and a.strip()]
    return "; ".join(names[:8]) + (" et al." if len(names) > 8 else "")


def _short_link(url: str) -> str:
    """'sciencedirect.com/topics/economics/instrumental-…' for display."""
    text = re.sub(r"^https?://(www\.)?", "", url)
    return text if len(text) <= 60 else text[:57] + "..."


def _edit_form(job: scan.Job, items: list[dict], i: int) -> None:
    p = items[i]
    k = zlib.crc32(p["url"].encode())      # one set of fields per item
    with st.form(key=f"edit_{k}", border=False):
        title = st.text_input("Title", value=p.get("title") or p.get("slack_file_name") or "",
                              key=f"edit_title_{k}")
        authors = st.text_input("Authors", value="; ".join(p.get("authors") or []),
                                help="Last, First; Last, First", key=f"edit_authors_{k}")
        c1, c2 = st.columns(2)
        year = c1.text_input("Year", value=p.get("year") or "", key=f"edit_year_{k}")
        kind = p.get("item_type") if p.get("item_type") in _ITEM_TYPES else "journalArticle"
        item_type = c2.selectbox("Type", _ITEM_TYPES, index=_ITEM_TYPES.index(kind))
        journal = st.text_input("Journal, publisher, or website", value=p.get("journal") or "",
                                key=f"edit_journal_{k}")
        row = st.container(horizontal=True)
        saved = row.form_submit_button("Save", type="primary")
        cancelled = row.form_submit_button("Cancel")
    if saved:
        fields = {"title": title.strip() or None,
                  "authors": [a.strip() for a in authors.split(";") if a.strip()],
                  "year": year.strip() or None, "item_type": item_type,
                  "journal": journal.strip() or None}
        _decide(job, items, i, "edited", fields)
    if cancelled:
        st.session_state.pop("editing_url", None)
        st.rerun()


def _matching(items: list[dict], query: str) -> list[dict]:
    """Items containing every word of the search."""
    words = query.lower().split()
    if not words:
        return items

    def text(p: dict) -> str:
        return " ".join([p.get("title") or "", " ".join(p.get("authors") or []),
                         p.get("journal") or "", p["url"], p.get("reason") or "",
                         p.get("summary") or ""]).lower()
    return [p for p in items if all(w in text(p) for w in words)]


def _count(shown: list, total: list) -> str:
    return str(len(total)) if len(shown) == len(total) else f"{len(shown)} of {len(total)}"


def step_scan(job: scan.Job | None) -> None:
    if job is None:
        waiting_step(2, "Results of search", "Available after the sources search is complete.")
        return
    snap = job.snapshot()
    phase = snap["phase"]

    if phase == scan.SCANNING:
        step_header(2, "Results of search", CURRENT)
        progress(job, scan.SCANNING)
        return

    if phase == scan.FAILED:
        step_header(2, "Results of search", CURRENT)
        st.error(snap["fatal_error"])
        return

    ready, review = job.items(scan.READY), job.items(scan.REVIEW)
    existing = job.items(scan.EXISTING)
    step_header(2, "Results of search", DONE_STATE)
    took = _minutes((job.finished_at or time.time()) - job.started_at)
    summary([f"Searched {job.n_sources} in {took}", f"{len(ready)} ready",
             f"{len(review)} review", f"{len(existing)} already in Zotero"], done=True)
    if snap["fatal_error"]:
        st.error("The search stopped because of an error. Items it didn't reach are in Review.")
        with st.expander("Details"):
            st.code(snap["fatal_error"], language=None)

    uploaded_any = any(_status(p) for p in ready + review)
    locked = phase == scan.UPLOADING

    query = st.text_input("Search results", placeholder="Title, author, journal, or link",
                          key="results_query")
    # Uploaded items leave the Ready table; step 3 reports them.
    ready = [p for p in ready if not p.get("zotero_key")]
    shown_ready, shown_review = _matching(ready, query), _matching(review, query)
    # A different key per search, so a ticked box can't carry over to the
    # wrong row when the list changes.
    suffix = zlib.crc32(query.strip().lower().encode())

    if ready or not uploaded_any:
        st.markdown(f"**Ready ({_count(shown_ready, ready)})**")
    if shown_ready:
        st.caption("Metadata found. Clear a box to leave an item out.")
        _table(shown_ready, f"t_ready_{suffix}", review=False,
               show_status=uploaded_any, locked=locked)

    st.markdown(f"**Review ({_count(shown_review, review)})**")
    if shown_review:
        st.caption("Could not be identified, or probably not a source. "
                   "Only items marked Correct or Edited are uploaded.")
        view = st.radio("Review view", ["Cards", "Table"], horizontal=True,
                        label_visibility="collapsed", key="review_view")
        if view == "Cards":
            review_cards(job, shown_review, locked)
        else:
            _table(shown_review, f"t_review_{suffix}", review=True,
                   show_status=uploaded_any, locked=locked)

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
        waiting_step(3, title, "Available after review of search results.")
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

    finished = has_uploaded and not n_selected
    step_header(3, title, DONE_STATE if finished else CURRENT)

    if has_uploaded:
        parts = [f"Uploaded {len(uploaded)}"]
        if failed:
            parts.append(f"{len(failed)} failed")
        skipped = snap["skipped_existing"]
        if skipped:
            parts.append(f"{skipped} skipped because they were added to Zotero after the search")
        summary(parts, done=finished)
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
            unchecked, unmarked = _left_out(job)
            if unchecked or unmarked:
                confirm_upload(job, n_selected, unchecked, unmarked)
            else:
                job.start_upload(zotero_client.make_zotero_client)
                st.rerun()
    elif not has_uploaded:
        st.caption("Nothing selected to upload.")

    if has_uploaded:
        if st.button("New search", type="tertiary" if n_selected else "primary",
                     key="new_search"):
            clear_job()
            st.rerun()


def _left_out(job: scan.Job) -> tuple[int, int]:
    """(unchecked Ready items, Review items not marked for upload), not
    counting anything already uploaded or removed."""
    ready = [p for p in job.items(scan.READY) if not p.get("zotero_key")]
    review = [p for p in job.items(scan.REVIEW)
              if not p.get("zotero_key") and p.get("decision") != "removed"]
    return (sum(1 for p in ready if not p.get("include")),
            sum(1 for p in review if not p.get("include")))


@st.dialog("Upload to Zotero")
def confirm_upload(job: scan.Job, n: int, unchecked: int, unmarked: int) -> None:
    st.write(f"Upload {n} {'item' if n == 1 else 'items'} to Zotero?")
    if unchecked:
        st.write(f"{unchecked} unchecked in Ready won't be uploaded.")
    if unmarked:
        st.write(f"{unmarked} in Review {'isn' if unmarked == 1 else 'aren'}'t "
                 "marked for upload.")
    row = st.container(horizontal=True)
    if row.button("Upload", type="primary", key="confirm_upload"):
        job.start_upload(zotero_client.make_zotero_client)
        st.rerun()
    if row.button("Cancel", key="cancel_upload"):
        st.rerun()


# ── Settings ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _zotero_labels(kind: str, lib: str, collection: str) -> tuple[str, str]:
    """Library and collection names for Settings, looked up once an hour.
    Falls back to the ids if Zotero can't be reached."""
    try:
        library = zotero_client.library_label()
    except Exception:
        library = f"{kind} {lib}"
    try:
        folder = zotero_client.collection_label()
    except Exception:
        folder = collection or "Library root"
    return library, folder


def render_settings() -> None:
    with st.expander("Settings"):
        lib = config.zotero_library_id()
        if zotero_ready():
            library, folder = _zotero_labels(config.zotero_library_type(), lib,
                                             config.zotero_collection_key() or "")
        else:
            library, folder = "not set", config.zotero_collection_key() or "Library root"
        st.text(f"Zotero library   {library}")
        st.text(f"Collection       {folder}")
        st.text(f"Anthropic key    {'set' if config.anthropic_api_key() else 'not set'}")
        st.text(f"Model            {config.llm_model()}")
        st.text("If you need to change any of these settings, contact David Beeson (david.beeson123@gmail.com) to edit the secrets in the streamlit app settings.")


# ── Page ──────────────────────────────────────────────────────────────────────

def main() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)
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
