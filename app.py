"""
app.py — Slack to Zotero.

One page with three steps that stay on the page: the Slack export, the scan
(with the results to review), and the upload. A finished step shrinks to a
summary; nothing is removed. The scan runs as a server-side job (scan.Job)
shared by every session, so a refresh shows the same page.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unicodedata
import zlib

import streamlit as st

# After a push, Streamlit Cloud re-runs this file with the new code but keeps
# the old versions of the package's modules loaded, so the two disagree until
# the app is rebooted. Drop the loaded copies when any file in the package has
# changed since they were loaded; they are then imported fresh. (Like a
# restart, this clears the current scan.)
from slack_to_zotero import code_stamp  # noqa: E402

if getattr(sys, "_slack_to_zotero_code", None) != code_stamp():
    # Stop a scan still running on the old code, or its threads keep going
    # unseen in the background.
    _old_job = getattr(sys.modules.get("slack_to_zotero.scan"), "_current", None)
    if _old_job is not None and hasattr(_old_job, "stop"):
        _old_job.stop()
    # The package too: it keeps references to its old modules.
    for _name in [n for n in sys.modules if n.split(".")[0] == "slack_to_zotero"]:
        sys.modules.pop(_name, None)
    sys._slack_to_zotero_code = code_stamp()

from slack_to_zotero import config  # noqa: E402
from slack_to_zotero import scan  # noqa: E402
from slack_to_zotero import zotero_client  # noqa: E402
from slack_to_zotero import zotero_index  # noqa: E402

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
    st.session_state.pop("confirm_stop", None)
    return scan.start_job(papers, config.anthropic_api_key(), str(config.LLM_AUDIT_PATH),
                          load_index=_load_index, refresh_index=_refresh_index,
                          source=source)


def clear_job() -> None:
    scan.clear_job()
    st.session_state.pop("confirm_start_over", None)
    st.session_state.pop("confirm_new_search", None)


def zotero_ready() -> bool:
    return bool(config.zotero_api_key() and config.zotero_library_id())


# ── Layout helpers ────────────────────────────────────────────────────────────

# Steps not reached yet are faded, with a line saying what they wait for.
# Stop, Remove, and Clear results are red; Move to Review is orange. A
# disabled button keeps Streamlit's grey.
_RED = ["stop", "stop_confirm", "card_remove", "t_ready_remove", "t_left_remove",
        "start_over_confirm", "new_search_confirm"]
_ORANGE = ["t_ready_move"]


def _button_color(keys: list[str], color: str, hover: str) -> str:
    sel = ", ".join(f".st-key-{k} button:not(:disabled)" for k in keys)
    hov = ", ".join(f".st-key-{k} button:not(:disabled):hover" for k in keys)
    return (f"{sel} {{ background-color: {color}; border-color: {color}; color: #fff; }}\n"
            f"{hov} {{ background-color: {hover}; border-color: {hover}; color: #fff; }}\n")


_STYLE = ("<style>\n"
          '[class*="st-key-waiting-"] h3 { opacity: 0.4; }\n'
          + _button_color(_RED, "#d93025", "#b3261e")
          + _button_color(_ORANGE, "#e8710a", "#c35f08")
          + "</style>")


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

    saved = scan.read_saved_results(files[0].getvalue()) if len(files or []) == 1 else None
    if saved is not None:
        if st.button("Search for sources", type="primary", disabled=not zotero_ready()):
            if not scan.load_saved_job(*saved, load_index=_load_index,
                                       refresh_index=_refresh_index):
                st.warning("A search or upload is already running.")
            st.rerun()
        return

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
        try:
            with tempfile.TemporaryDirectory() as tmp:
                export = scan.read_export([(f.name, f.getvalue()) for f in files], tmp)
                chosen = channel if channel is not None else next(iter(export.channels))
                day_files = export.channels[chosen]
                papers = scan.collect_papers(day_files, export.user_map)
        except Exception as exc:
            st.error(f"Could not read this export. {type(exc).__name__}: {exc}")
            return
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
    st.caption("This clears the search results for everyone using the app. "
               "Items already uploaded stay in Zotero.")
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
        st.session_state.pop("confirm_stop", None)   # no stale prompt for the next search
        st.rerun()                       # finished: redraw the whole page
    step = "Paused" if snap["paused"] else snap["step"]
    if snap["step"] in ("Searching", "Uploading"):
        total = max(snap["total"], 1)
        st.progress(min(snap["done"] / total, 1.0),
                    text=f"{step} {snap['done']} of {snap['total']}")
    else:
        st.progress(0, text=step)
    if phase == scan.SCANNING:
        search_controls(job, snap)
    if snap["log"]:
        with st.expander("Log"):
            st.code("\n".join(reversed(snap["log"][-300:])), language=None)


def search_controls(job: scan.Job, snap: dict) -> None:
    """Pause or Resume keeps the results so far; Stop clears everything,
    after asking in a dialog (opened from step_scan, outside this fragment)."""
    row = st.container(horizontal=True)
    if snap["paused"]:
        if row.button("Resume", type="primary", key="resume"):
            job.resume()
            st.rerun()
    elif row.button("Pause", key="pause"):
        job.pause()
        st.rerun()
    if row.button("Stop", key="stop"):
        st.session_state.confirm_stop = True
        st.rerun(scope="app")


def _cancel_stop() -> None:
    st.session_state.pop("confirm_stop", None)


@st.dialog("Stop search", on_dismiss=_cancel_stop)
def confirm_stop() -> None:
    st.write("This stops the search and clears its results.")
    row = st.container(horizontal=True)
    if row.button("Stop and clear", key="stop_confirm"):
        _cancel_stop()
        scan.discard_job()
        st.rerun()
    if row.button("Cancel", key="stop_cancel"):
        _cancel_stop()
        st.rerun()


# ── Step 2: Scan and review ───────────────────────────────────────────────────

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
    _bump_table()
    # Move on to the next item without a decision, if there is one.
    later = [q for q in items[i + 1:] + items[:i] if not q.get("decision")]
    _go_to(later[0]["url"] if later else items[i]["url"])


def _stop_editing() -> None:
    st.session_state.pop("editing_url", None)


def _start_editing(url: str) -> None:
    st.session_state.editing_url = url


def _save_card(job: scan.Job, p: dict) -> None:
    k = zlib.crc32(p["url"].encode())
    get = lambda name: (st.session_state.get(f"edit_{name}_{k}") or "").strip()
    job.edit(p["url"], {
        "title": get("title") or None,
        "authors": [a.strip() for a in get("authors").split(";") if a.strip()],
        "year": get("year") or None,
        "item_type": st.session_state.get(f"edit_type_{k}") or p.get("item_type"),
        "journal": get("journal") or None})
    _bump_table()
    _stop_editing()


def review_section(job: scan.Job, items: list[dict], locked: bool) -> None:
    """Review in two collapsible parts: items still to decide (as cards or
    an editable table) and removed items. Items marked ready for upload are
    shown under Ready."""
    undecided = [q for q in items if not q.get("decision")]
    removed = [q for q in items if q.get("decision") == "removed"]

    with st.expander(f"Left to review ({len(undecided)})", expanded=True,
                     key="exp_left"):
        if not undecided:
            st.caption("All items reviewed." if items else "Nothing to review.")
        else:
            st.caption("Could not be identified, or probably not a source. "
                       "Only items marked ready for upload are uploaded.")
            view = st.radio("Review view", ["Cards", "Table"], horizontal=True,
                            label_visibility="collapsed", key="review_view",
                            disabled=_tables_have_unsaved_edits())
            if view == "Cards":
                _card(job, undecided, locked)
            else:
                _review_table(job, undecided, locked)
    _decided_table("Removed", removed, job, locked, "exp_removed")


def _decided_table(label: str, items: list[dict], job: scan.Job, locked: bool,
                   key: str) -> None:
    """A collapsed table of removed items; Undo sends selected rows back."""
    with st.expander(f"{label} ({len(items)})", key=key):
        if not items:
            st.caption("None yet.")
            return
        _item_table(job, items, f"t_{key}", locked, "decided")


def _tables_have_unsaved_edits() -> bool:
    """Whether a table has edited cells not yet saved (ticks don't count)."""
    for key, state in st.session_state.items():
        if isinstance(key, str) and key.startswith("t_") and isinstance(state, dict):
            if any(set(change) - {"Select"} for change in state.get("edited_rows", {}).values()):
                return True
    return False


def _bump_table() -> None:
    """New table keys after an action, so ticks can't carry over to other
    rows as items move between tables."""
    st.session_state.table_version = st.session_state.get("table_version", 0) + 1


def _review_table(job: scan.Job, items: list[dict], locked: bool) -> None:
    _item_table(job, items, "t_left", locked, "left")


def _item_table(job: scan.Job, items: list[dict], name: str, locked: bool,
                mode: str, show_status: bool = False) -> None:
    """Items as an editable table: tick rows in the first column, then act on
    them with the buttons below. Title, authors, year, and journal can be
    changed in place and are kept with Save edits. `mode` is "ready",
    "left" (to review), or "decided" (removed)."""
    rows = []
    for p in items:
        row = {"Select": False, "Link": p["url"]}
        if show_status:
            row["Status"] = _status(p)
        if mode != "ready":
            row["Reason"] = p.get("reason", "")
        row.update({"Title": p.get("title") or p.get("slack_file_name") or "",
                    "Authors": "; ".join(p.get("authors") or []),
                    "Year": p.get("year") or "", "Journal": p.get("journal") or ""})
        if mode != "ready":
            row["Summary"] = p.get("summary") or ""
        rows.append(row)
    urls = [p["url"] for p in items]
    key = f"{name}_{st.session_state.get('table_version', 0)}"
    columns = list(rows[0])
    st.data_editor(
        rows, key=key, hide_index=True,
        disabled=columns if locked else ["Link", "Status", "Reason", "Summary"],
        column_config={
            "Select": st.column_config.CheckboxColumn("", width=40),
            "Link": st.column_config.LinkColumn(display_text="Open", width=50),
            "Status": st.column_config.TextColumn(width=90),
            "Reason": st.column_config.TextColumn(width=170),
            "Title": st.column_config.TextColumn(width="medium" if mode != "ready" else "large"),
            "Authors": st.column_config.TextColumn(width=180, help="Last, First; Last, First"),
            "Year": st.column_config.TextColumn(width=70),
            "Journal": st.column_config.TextColumn(width=150),
            "Summary": st.column_config.TextColumn(width=220),
        },
    )
    changes = st.session_state.get(key, {}).get("edited_rows", {})
    bar = st.container(horizontal=True)
    if any(set(c) - {"Select"} for c in changes.values()):
        bar.button("Save edits", type="primary", key=f"{name}_save",
                   on_click=_save_table, args=(job, urls, key))
        bar.button("Cancel", key=f"{name}_cancel", on_click=_bump_table)
        return
    chosen = [items[int(r)] for r, c in changes.items()
              if c.get("Select") and int(r) < len(items)
              and not items[int(r)].get("zotero_key")]
    n = len(chosen)

    def act(label: str, action: str, primary: bool = False) -> None:
        bar.button(f"{label} ({n})" if n else label,
                   type="primary" if primary else "secondary",
                   disabled=locked or not n, key=f"{name}_{action}",
                   on_click=table_action, args=(job, action, chosen))

    if mode == "decided":
        act("Undo", "undo")
        return
    if mode == "ready":
        act("Move to Review", "move")
    else:
        act("Mark ready for upload", "mark", primary=True)
    act("Remove", "remove")


def table_action(job: scan.Job, action: str, items: list[dict]) -> None:
    """Apply a table button to the ticked rows."""
    for p in items:
        if action == "undo":
            job.decide(p["url"], None)
        elif action == "move":
            if p.get("bucket") == scan.READY:
                job.move_to_review(p["url"])
            else:
                job.decide(p["url"], None)        # marked in Review: back to undecided
        elif action == "mark":
            mark_for_upload(job, [p])
        elif action == "remove":
            job.move_to_review(p["url"])          # a Ready item joins Review first
            job.decide(p["url"], "removed")
    _bump_table()


def _save_table(job: scan.Job, urls: list[str], key: str) -> None:
    apply_review_edits(job, urls, st.session_state[key]["edited_rows"])
    _bump_table()


def apply_review_edits(job: scan.Job, urls: list[str], edited_rows: dict) -> None:
    """Save details typed into a review table."""
    for row, change in edited_rows.items():
        fields = {}
        if "Title" in change:
            fields["title"] = change["Title"].strip() or None
        if "Authors" in change:
            fields["authors"] = [a.strip() for a in change["Authors"].split(";") if a.strip()]
        if "Year" in change:
            fields["year"] = change["Year"].strip() or None
        if "Journal" in change:
            fields["journal"] = change["Journal"].strip() or None
        if fields:
            job.edit(urls[int(row)], fields)


def mark_for_upload(job: scan.Job, items: list[dict]) -> None:
    for p in items:
        job.decide(p["url"], "edited" if p.get("was_edited") else "correct")


def _card(job: scan.Job, items: list[dict], locked: bool) -> None:
    urls = [p["url"] for p in items]
    current = st.session_state.get("card_url")
    i = urls.index(current) if current in urls else 0
    p = items[i]

    # Moving away mid-edit would drop the edits, so finish or cancel first.
    editing_here = st.session_state.get("editing_url") == p["url"]
    nav = st.container(horizontal=True, vertical_alignment="center")
    nav.button("Previous", disabled=i == 0 or editing_here, key="card_prev",
               on_click=_go_to, args=(urls[max(i - 1, 0)],))
    nav.button("Next", disabled=i == len(items) - 1 or editing_here, key="card_next",
               on_click=_go_to, args=(urls[min(i + 1, len(urls) - 1)],))
    nav.caption(f"{i + 1} of {len(items)}")

    with st.container(border=True):
        badges = st.container(horizontal=True)
        with badges:
            st.badge(p.get("reason") or "Review", color="orange")
            if p.get("decision"):
                st.badge(_DECISION_LABELS[p["decision"]], color="green")
            if p.get("zotero_key"):
                st.badge("Uploaded", color="blue")
        frozen = locked or bool(p.get("zotero_key"))
        editing = st.session_state.get("editing_url") == p["url"] and not frozen
        if editing:
            _edit_fields(p)
            lines = []
        else:
            lines = [
                _field("Title", p.get("title") or p.get("slack_file_name")),
                _field("Authors", _all_authors(p.get("authors") or [])),
                _field("Year", p.get("year")),
                _field("Website" if p.get("item_type") == "webpage" else "Journal",
                       p.get("journal") or (scan.site_name(p["url"])
                                            if p.get("item_type") == "webpage" else None)),
            ]
        # <...> around the address keeps one with brackets, like
        # wiki/Eviction_(law), from breaking the link.
        lines += [f"**Link:** [{_md(_short_link(p['url']))}](<{p['url']}>)",
                  _field("Link summary", p.get("summary"))]
        comments = p.get("comments") or []
        if comments:
            first = comments[0]
            # One line, so a message with blank lines keeps the card compact.
            text = " ".join((first.get("text") or "").split())
            if text:
                quote = _md(text[:400] + ("..." if len(text) > 400 else ""))
                body = f'{_md(first.get("author") or "")}: "{quote}"'
            else:
                body = "*(No message)*"
            lines.append(f"**Original Slack message:** {body}")
        # One block with line breaks keeps the card compact.
        st.markdown("  \n".join(lines))

        actions = st.container(horizontal=True)
        if editing:
            actions.button("Save edits", type="primary", key="card_save",
                           on_click=_save_card, args=(job, p))
            actions.button("Cancel", key="card_cancel", on_click=_stop_editing)
            return
        actions.button("Mark ready for upload", type="primary", disabled=frozen,
                       key="card_correct", on_click=_decide,
                       args=(job, items, i, "edited" if p.get("was_edited") else "correct"))
        actions.button("Edit", disabled=frozen, key="card_edit",
                       on_click=_start_editing, args=(p["url"],))
        actions.button("Remove", disabled=frozen, key="card_remove",
                       on_click=_decide, args=(job, items, i, "removed"))


def _edit_fields(p: dict) -> dict:
    """The card's details as inputs, in place of the plain text."""
    k = zlib.crc32(p["url"].encode())      # one set of inputs per item
    title = st.text_input("Title", value=p.get("title") or p.get("slack_file_name") or "",
                          key=f"edit_title_{k}")
    authors = st.text_input("Authors", value="; ".join(p.get("authors") or []),
                            help="Last, First; Last, First", key=f"edit_authors_{k}")
    c1, c2, c3 = st.columns([1, 2, 3])
    year = c1.text_input("Year", value=p.get("year") or "", key=f"edit_year_{k}")
    kind = p.get("item_type") if p.get("item_type") in _ITEM_TYPES else "journalArticle"
    item_type = c2.selectbox("Type", _ITEM_TYPES, index=_ITEM_TYPES.index(kind),
                             key=f"edit_type_{k}")
    journal = c3.text_input("Journal or website", value=p.get("journal") or "",
                            key=f"edit_journal_{k}")
    return {"title": title.strip() or None,
            "authors": [a.strip() for a in authors.split(";") if a.strip()],
            "year": year.strip() or None, "item_type": item_type,
            "journal": journal.strip() or None}


def _field(label: str, value) -> str:
    shown = _md(str(value)) if value else ":gray[Not found]"
    return f"**{label}:** {shown}"


def _all_authors(authors: list[str]) -> str:
    names = [a.strip() for a in authors if a and a.strip()]
    return "; ".join(names[:8]) + (" et al." if len(names) > 8 else "")


def _short_link(url: str) -> str:
    """'sciencedirect.com/topics/economics/instrumental-…' for display."""
    text = re.sub(r"^https?://(www\.)?", "", url)
    return text if len(text) <= 60 else text[:57] + "..."


def _plain(text: str) -> str:
    """Lowercase without accents, so "colmsjo" finds "Colmsjö"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _matching(items: list[dict], query: str) -> list[dict]:
    """Items containing every word of the search."""
    words = _plain(query).split()
    if not words:
        return items

    def text(p: dict) -> str:
        return _plain(" ".join([
            p.get("title") or "", p.get("slack_file_name") or "",
            " ".join(p.get("authors") or []), p.get("journal") or "", p["url"],
            p.get("reason") or "", p.get("summary") or ""]))
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
        if st.session_state.get("confirm_stop"):
            confirm_stop()
        return

    if phase == scan.FAILED:
        step_header(2, "Results of search", CURRENT)
        st.error(snap["fatal_error"])
        if st.button("Try again", type="primary", key="retry_search"):
            scan.retry_job()
            st.rerun()
        return

    ready, review = job.items(scan.READY), job.items(scan.REVIEW)
    existing = job.items(scan.EXISTING)
    step_header(2, "Results of search", DONE_STATE)
    took = _minutes(job.search_seconds())
    summary([f"Searched {job.n_sources} in {took}", f"{len(ready)} ready",
             f"{len(review)} review", f"{len(existing)} already in Zotero"], done=True)
    if snap["fatal_error"]:
        st.error("The search stopped because of an error. Items it didn't reach are in Review.")
        with st.expander("Details"):
            st.code(snap["fatal_error"], language=None)
    if snap["llm_problem"]:
        st.warning(f"{snap['llm_problem']} Items that needed the AI step are in Review; "
                   "search again once it's fixed.")

    uploaded_any = any(_status(p) for p in ready + review)
    locked = phase == scan.UPLOADING

    # Changing the search redraws the tables, which would drop unsaved edits.
    editing = _tables_have_unsaved_edits()
    query = st.text_input("Search results", placeholder="Title, author, journal, or link",
                          key="results_query", type="search", live=True,
                          disabled=editing) or ""
    if editing:
        st.caption("Save or cancel the table edits to search.")
    # Ready shows everything that will be uploaded: items the search found
    # plus those marked ready in Review. Uploaded items leave it; step 3
    # reports them.
    marked = [p for p in review if p.get("decision") in ("correct", "edited")]
    ready = sorted((p for p in ready + marked if not p.get("zotero_key")),
                   key=scan._sort_title)
    shown_ready, shown_review = _matching(ready, query), _matching(review, query)
    # A different key per search, so a ticked box can't carry over to the
    # wrong row when the list changes.
    suffix = zlib.crc32(query.strip().lower().encode())

    if ready or not uploaded_any:
        with st.expander(f"Ready ({_count(shown_ready, ready)})", expanded=True,
                         key="exp_ready"):
            if shown_ready:
                st.caption("Metadata found. These are uploaded unless moved to Review or removed.")
                _item_table(job, shown_ready, f"t_ready_{suffix}", locked, "ready",
                            show_status=uploaded_any)

    review_section(job, shown_review, locked)

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
            parts.append(f"{skipped} skipped as duplicates")
        summary(parts, done=finished)
        if failed:
            st.dataframe([{"Title": p.get("title") or p["url"], "Error": p["upload_error"]}
                          for p in failed], hide_index=True)
        # Problems with an uploaded item's file or note; failed items are listed above.
        warnings = [msg for _url, msg in (snap["upload_result"] or {}).get("errors", [])
                    if msg.startswith(("pdf_attach", "note upload"))]
        if warnings:
            with st.expander(f"Warnings ({len(warnings)})"):
                st.write("These items were uploaded without some of their files or notes.")
                st.code("\n".join(warnings), language=None)

    if n_selected:
        what = "more selected" if has_uploaded else "selected"
        st.caption(f"{n_selected} {what}.")
        label = "Retry failed" if failed and n_selected == len(failed) else \
            f"Upload {n_selected} ready {'item' if n_selected == 1 else 'items'} to Zotero"
        if st.button(label, type="primary", disabled=not zotero_ready()):
            unmarked = _left_out(job)
            if unmarked:
                confirm_upload(job, n_selected, unmarked)
            else:
                job.start_upload(zotero_client.make_zotero_client)
                st.rerun()
    elif not has_uploaded:
        st.caption("Nothing selected to upload.")

    if has_uploaded:
        new_search(job, primary=not n_selected)


def new_search(job: scan.Job, primary: bool) -> None:
    """Clear the results for a new search, asking first if any item that
    hasn't been uploaded or removed would be lost."""
    unsent = sum(1 for p in job.items(scan.READY) + job.items(scan.REVIEW)
                 if not p.get("zotero_key") and p.get("decision") != "removed")
    if not st.session_state.get("confirm_new_search"):
        if st.button("New search", type="primary" if primary else "tertiary",
                     key="new_search"):
            if unsent:
                st.session_state.confirm_new_search = True
            else:
                clear_job()
            st.rerun()
        return
    st.caption(f"{_plural(unsent, 'item')} not uploaded will be cleared, "
               "for everyone using the app.")
    row = st.container(horizontal=True)
    if row.button("Clear results", type="primary", key="new_search_confirm"):
        clear_job()
        st.rerun()
    if row.button("Cancel", type="tertiary", key="new_search_cancel"):
        st.session_state.pop("confirm_new_search", None)
        st.rerun()


def _left_out(job: scan.Job) -> int:
    """Review items not marked for upload, not counting removed or uploaded ones."""
    return sum(1 for p in job.items(scan.REVIEW)
               if not p.get("zotero_key") and p.get("decision") != "removed"
               and not p.get("include"))


@st.dialog("Upload to Zotero")
def confirm_upload(job: scan.Job, n: int, unmarked: int) -> None:
    st.write(f"Upload {n} {'item' if n == 1 else 'items'} to Zotero?")
    st.write(f"{unmarked} in Review {'isn' if unmarked == 1 else 'aren'}'t "
             "marked ready for upload.")
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
        st.warning("Zotero isn't set up. Add the Zotero key and library ID to the app's secrets.")

    job = scan.current_job()
    step_export(job)
    st.divider()
    step_scan(job)
    st.divider()
    step_upload(job)
    st.divider()
    render_settings()


main()
