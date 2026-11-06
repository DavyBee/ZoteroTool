"""Headless page tests for app.py using Streamlit's AppTest."""

from __future__ import annotations

import sys
import time

import pytest
from streamlit.testing.v1 import AppTest

from slack_to_zotero import code_stamp

from slack_to_zotero import scan


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # The modules these tests imported are current, so the page should use them.
    monkeypatch.setattr(sys, "_slack_to_zotero_code", code_stamp(), raising=False)
    from slack_to_zotero import zotero_client
    monkeypatch.setattr(zotero_client, "library_label", lambda: "Lab Library (group 1)")
    monkeypatch.setattr(zotero_client, "collection_label", lambda: "Testing (ABC123)")
    monkeypatch.setenv("ZOTERO_API_KEY", "test")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scan._current = None
    yield
    scan._current = None


def _wait(job, phase):
    end = time.time() + 5
    while job.snapshot()["phase"] != phase:
        assert time.time() < end
        time.sleep(0.01)


def _scanned_job():
    def enrich(p, key, log):
        if p["url"].endswith("junk"):
            return {"metadata_source": "non_paper"}
        return {"metadata_source": "crossref_doi", "title": "A Paper",
                "authors": ["Doe, Jane"], "year": "2024"}

    papers = [{"url": "https://a.org/paper", "comments": []},
              {"url": "https://a.org/junk", "comments": []}]
    assert scan.start_job(papers, None, enrich=enrich,
                          source={"label": "export.zip"})
    job = scan.current_job()
    _wait(job, scan.SCANNED)
    return job


def _page():
    return AppTest.from_file("../app.py", default_timeout=30).run()


def _buttons(at):
    return {b.label: b for b in at.button}


def _text(at):
    return " ".join([e.value for e in at.markdown] + [e.value for e in at.caption]
                    + [e.value for e in at.subheader] + [b.label for b in at.button])


def test_start_page_shows_all_three_steps():
    at = _page()
    assert not at.exception
    assert at.title[0].value == "Slack to Zotero"
    assert [h.value for h in at.subheader] == [
        "1. Slack export", "2. Results of search", "3. Upload to Zotero"]
    text = _text(at)
    assert "Available after the sources search is complete." in text
    assert "Available after review of search results." in text
    assert _buttons(at)["Search for sources"].disabled   # no export chosen yet
    assert not at.warning                             # Zotero is configured


def test_missing_zotero_config_is_reported(monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY")
    at = _page()
    assert "Zotero isn't set up" in at.warning[0].value


def test_finished_scan_keeps_every_step_on_the_page():
    _scanned_job()
    at = _page()                                     # a fresh session = a refresh
    assert not at.exception
    text = _text(at)
    assert "Done · export.zip" in text
    assert "1 ready · 1 review · 0 already in Zotero" in text
    labels = [e.label for e in at.expander]
    assert ["Ready (1)", "Left to review (1)", "Removed (0)"] == labels[:3]
    assert "Upload 1 ready item to Zotero" in _buttons(at)


def test_start_over_asks_before_clearing():
    _scanned_job()
    at = _page()
    _buttons(at)["Start over"].click().run()
    assert scan.current_job() is not None            # nothing cleared yet
    assert "Clear results" in _buttons(at)
    _buttons(at)["Cancel"].click().run()
    assert "Start over" in _buttons(at)
    _buttons(at)["Start over"].click().run()
    _buttons(at)["Clear results"].click().run()
    assert scan.current_job() is None
    assert "Search for sources" in _buttons(at)


def test_results_stay_after_upload_and_more_can_be_uploaded():
    job = _scanned_job()
    job.start_upload(lambda: None, upload=lambda ps, zot, **kw: (
        [p.__setitem__("zotero_key", "K") for p in ps]
        and {"uploaded": len(ps), "failed": 0, "errors": []}))
    _wait(job, scan.DONE)
    at = _page()
    text = _text(at)
    assert "Done · Uploaded 1" in text
    assert "**Ready" not in text                     # uploaded items leave Ready
    assert "Left to review (1)" in [e.label for e in at.expander]   # Review stays
    assert "New search" in _buttons(at)
    assert "Upload 1 ready item to Zotero" not in _buttons(at)   # nothing left selected

    job.set_include("https://a.org/junk", True)       # pick something from Review
    at = _page()
    assert "1 more selected." in _text(at)
    assert "Upload 1 ready item to Zotero" in _buttons(at)


def test_no_emoji_in_page_text():
    _scanned_job()
    text = _text(_page())
    assert all(ord(ch) < 0x2190 for ch in text), text


def test_changed_code_is_loaded_fresh(monkeypatch):
    saved = {m: mod for m, mod in sys.modules.items() if m.split(".")[0] == "slack_to_zotero"}
    stopped = []

    class RunningJob:
        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(scan, "_current", RunningJob())
    try:
        monkeypatch.setattr(sys, "_slack_to_zotero_code", ("old",), raising=False)
        at = _page()
        assert not at.exception
        assert sys.modules["slack_to_zotero.scan"] is not saved["slack_to_zotero.scan"]  # imported again
        assert stopped == [True]                             # the old scan was stopped
        assert sys._slack_to_zotero_code == code_stamp()
    finally:
        sys.modules.update(saved)


def test_settings_show_library_and_collection_names():
    from streamlit import cache_data
    cache_data.clear()
    texts = [t.value for t in _page().text]
    assert "Zotero library   Lab Library (group 1)" in texts
    assert "Collection       Testing (ABC123)" in texts


def test_search_filters_the_results():
    _scanned_job()
    at = _page()
    at.text_input(key="results_query").input("paper").run()
    labels = [e.label for e in at.expander]
    assert "Ready (1)" in labels and "Left to review (0)" in labels
    at.text_input(key="results_query").input("nothing matches this").run()
    assert "Ready (0 of 1)" in [e.label for e in at.expander]


# ── Review cards ──────────────────────────────────────────────────────────────

def _junk(job):
    return next(p for p in job.papers if p["url"].endswith("junk"))


def test_review_card_correct_adds_the_item_to_the_upload():
    job = _scanned_job()
    at = _page()
    assert "1 of 1" in _text(at)
    _buttons(at)["Mark ready for upload"].click().run()
    assert _junk(job)["decision"] == "correct" and _junk(job)["include"]
    assert "Upload 2 ready items to Zotero" in _buttons(at)
    assert "All items reviewed." in _text(at)
    assert "Ready (2)" in [e.label for e in at.expander]       # joins Ready


def test_review_card_remove_leaves_the_item_out_and_can_be_undone():
    job = _scanned_job()
    at = _page()
    assert "Not a source" not in _buttons(at)
    _buttons(at)["Remove"].click().run()
    assert _junk(job)["decision"] == "removed" and not _junk(job)["include"]
    assert "Upload 1 ready item to Zotero" in _buttons(at)
    assert "All items reviewed." in _text(at)
    assert "Removed (1)" in [e.label for e in at.expander]
    assert _buttons(at)["Undo"].disabled             # until a row is ticked


def test_review_card_edit_saves_the_fields_and_uploads():
    job = _scanned_job()
    at = _page()
    _buttons(at)["Edit"].click().run()
    import zlib
    k = zlib.crc32(_junk(job)["url"].encode())
    at.text_input(key=f"edit_title_{k}").input("The Real Title")
    at.text_input(key=f"edit_first_{k}_0").input("Jane")
    at.text_input(key=f"edit_last_{k}_0").input("Doe").run()
    for _ in range(3):
        at.button(key=f"edit_add_{k}").click().run()
    at.text_input(key=f"edit_first_{k}_1").input("Rich")
    at.text_input(key=f"edit_last_{k}_1").input("Roe")
    at.text_input(key=f"edit_last_{k}_2").input("Urban Institute").run()
    at.button(key=f"edit_drop_{k}_1").click().run()      # remove Roe
    assert at.text_input(key=f"edit_last_{k}_0").value == "Doe"   # others kept
    at.text_input(key=f"edit_year_{k}").input("2023")
    _buttons(at)["Save edits"].click().run()
    p = _junk(job)
    assert (p["title"], p["authors"], p["year"]) == \
        ("The Real Title", ["Doe, Jane", "Urban Institute"], "2023")   # empty row left out
    assert "decision" not in p and "Mark ready for upload" in _buttons(at)   # back to the card
    _buttons(at)["Mark ready for upload"].click().run()
    assert p["decision"] == "edited"
    assert p["include"] and "Upload 2 ready items to Zotero" in _buttons(at)


def test_review_table_view_is_still_available():
    _scanned_job()
    at = _page()
    at.radio(key="review_view").set_value("Table").run()
    assert not at.exception
    assert "card_correct" not in [b.key for b in at.button]


# ── Confirming an upload ──────────────────────────────────────────────────────

def test_upload_asks_first_when_items_are_left_out():
    job = _scanned_job()                    # 1 ready, 1 review item not marked
    at = _page()
    _buttons(at)["Upload 1 ready item to Zotero"].click().run()
    text = " ".join(m.value for m in at.markdown)
    assert "Upload 1 item to Zotero?" in text
    assert "1 in Review isn't marked ready for upload." in text
    assert job.snapshot()["phase"] == scan.SCANNED        # nothing uploaded yet
    _buttons(at)["Cancel"].click().run()
    assert job.snapshot()["phase"] == scan.SCANNED


def test_upload_goes_straight_through_when_nothing_is_left_out(monkeypatch):
    job = _scanned_job()
    job.decide(_junk(job)["url"], "removed")
    started = []
    monkeypatch.setattr(job, "start_upload", lambda make: started.append(True))
    at = _page()
    _buttons(at)["Upload 1 ready item to Zotero"].click().run()
    assert started == [True]


def test_card_shows_only_the_original_slack_message():
    job = _scanned_job()
    junk = _junk(job)
    junk["comments"] = [{"author": "Lorae", "ts": "1", "text": "", "is_reply": False},
                        {"author": "Ben", "ts": "2", "text": "agreed", "is_reply": True}]
    text = _text(_page())
    assert "**Original Slack message:** *(No message)*" in text
    assert "agreed" not in text and "replies" not in text


def test_card_quotes_a_real_slack_message():
    job = _scanned_job()
    _junk(job)["comments"] = [{"author": "Ana", "ts": "1", "text": "Worth a read", "is_reply": False}]
    assert '**Original Slack message:** Ana: "Worth a read"' in _text(_page())


def test_review_sections_are_collapsible_lists():
    _scanned_job()
    at = _page()
    assert "Left to review (1)" in [e.label for e in at.expander]
    _buttons(at)["Remove"].click().run()
    labels = [e.label for e in at.expander]
    assert "Left to review (0)" in labels and "Removed (1)" in labels


def test_review_table_marks_and_removes_selected_rows():
    import app as page
    job = _scanned_job()
    p = _junk(job)
    page.mark_for_upload(job, [p])
    assert p["decision"] == "correct" and p["include"]
    job.decide(p["url"], None)
    job.edit(p["url"], {"title": "Fixed"})
    page.mark_for_upload(job, [p])
    assert p["decision"] == "edited"


def test_review_table_view_has_action_buttons():
    _scanned_job()
    at = _page()
    at.radio(key="review_view").set_value("Table").run()
    buttons = _buttons(at)
    assert not at.exception
    assert buttons["Mark ready for upload"].disabled and buttons["Remove"].disabled
    assert "Edit" not in buttons                        # edited in the table


def test_editing_a_marked_item_in_the_table_records_the_edit():
    import app as page
    job = _scanned_job()
    p = _junk(job)
    job.decide(p["url"], "correct")
    page.apply_review_edits(job, [p["url"]], {0: {"Title": "Better title", "Select": True}})
    assert p["title"] == "Better title" and p["decision"] == "edited"
    page.apply_review_edits(job, [p["url"]], {0: {"Select": True}})   # ticks alone change nothing
    assert p["title"] == "Better title"


def test_search_ignores_accents_and_finds_file_names():
    import app as page
    items = [{"url": "https://a.org/1", "title": "Health Shocks", "authors": ["Colmsjö, Anna"]},
             {"url": "https://files.slack.com/2", "slack_file_name": "Ahmad Livshits 2024.pdf"}]
    assert [p["url"] for p in page._matching(items, "colmsjo")] == ["https://a.org/1"]
    assert [p["url"] for p in page._matching(items, "LIVSHITS")] == ["https://files.slack.com/2"]


def test_card_navigation_waits_while_editing():
    job = _scanned_job()
    job.papers.append({"url": "https://a.org/junk2", "comments": [], "bucket": scan.REVIEW,
                       "reason": "No metadata found", "include": False})
    at = _page()
    assert not _buttons(at)["Next"].disabled
    _buttons(at)["Edit"].click().run()
    assert _buttons(at)["Next"].disabled
    _buttons(at)["Cancel"].click().run()
    assert not _buttons(at)["Next"].disabled


def test_card_handles_links_with_brackets_and_multiline_messages():
    job = _scanned_job()
    junk = _junk(job)
    junk["url"] = "https://en.wikipedia.org/wiki/Eviction_(law)"
    junk["comments"] = [{"author": "Ana", "ts": "1", "text": "First line\n\nSecond line\n# not a heading",
                         "is_reply": False}]
    text = _text(_page())
    assert "](<https://en.wikipedia.org/wiki/Eviction_(law)>)" in text
    assert '"First line Second line \\# not a heading"' in text


def test_new_search_asks_first_when_items_would_be_lost():
    job = _scanned_job()                    # after upload, the Review item is left
    job.start_upload(lambda: None, upload=lambda ps, zot, **kw: (
        [p.__setitem__("zotero_key", "K") for p in ps]
        and {"uploaded": len(ps), "failed": 0, "errors": []}))
    _wait(job, scan.DONE)
    at = _page()
    _buttons(at)["New search"].click().run()
    assert scan.current_job() is job                   # not cleared yet
    assert "1 item not uploaded will be cleared" in _text(at)
    _buttons(at)["Cancel"].click().run()
    assert scan.current_job() is job
    _buttons(at)["New search"].click().run()
    _buttons(at)["Clear results"].click().run()
    assert scan.current_job() is None


def test_failed_batches_are_not_listed_as_file_warnings():
    job = _scanned_job()

    def upload(ps, zot, **kw):
        for p in ps:
            p["upload_error"] = "Couldn't reach Zotero. Check the connection, then retry."
        return {"uploaded": 0, "failed": len(ps),
                "errors": [(p["url"], p["upload_error"]) for p in ps]}
    job.start_upload(lambda: None, upload=upload)
    _wait(job, scan.DONE)
    at = _page()
    assert not [e for e in at.expander if e.label.startswith("Warnings")]
    assert "Retry failed" in _buttons(at)


def test_a_failed_search_can_be_tried_again():
    attempts = []

    def load_index():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("network down")
        from slack_to_zotero.zotero_index import LibraryIndex
        return LibraryIndex()

    assert scan.start_job([{"url": "https://a.org/paper", "comments": []}], None,
                          load_index=load_index, title_search=lambda *a: None,
                          enrich=lambda p, *a: {"metadata_source": "translator", "title": "A Paper"},
                          source={"label": "export.zip"})
    _wait(scan.current_job(), scan.FAILED)
    at = _page()
    assert "Couldn't reach Zotero" in at.error[0].value
    _buttons(at)["Try again"].click().run()
    _wait(scan.current_job(), scan.SCANNED)
    assert scan.current_job().items(scan.READY)[0]["title"] == "A Paper"


def test_search_waits_while_a_table_has_unsaved_edits():
    _scanned_job()
    at = _page()
    assert not at.text_input(key="results_query").disabled
    at.session_state["t_left_0"] = {"edited_rows": {0: {"Select": True}}}
    at.run()
    assert not at.text_input(key="results_query").disabled       # ticks aren't edits
    at.session_state["t_left_0"] = {"edited_rows": {0: {"Title": "New"}}}
    at.run()
    assert at.text_input(key="results_query").disabled
    assert "Save or cancel the table edits to search." in _text(at)


@pytest.mark.parametrize("seed", range(3))
def test_random_clicking_never_breaks_the_page(monkeypatch, seed):
    """Click random enabled buttons (and switch views) hundreds of times, the
    way an impatient person might. The page must never show an error, and
    the results must stay consistent."""
    import random

    from slack_to_zotero import zotero_client

    sent = []

    def fake_upload(papers, zot, progress_cb=None, log_cb=None):
        for p in papers:
            sent.append(p["url"])
            p["zotero_key"] = f"K{len(sent)}"
        return {"uploaded": len(papers), "failed": 0, "errors": []}
    monkeypatch.setattr(zotero_client, "upload_papers", fake_upload)
    monkeypatch.setattr(zotero_client, "make_zotero_client", lambda: None)

    def enrich(p, *a):
        i = int(p["url"].rsplit("/", 1)[1])
        if i % 3 == 0:
            return {"metadata_source": "none"}
        return {"metadata_source": "translator", "title": f"Paper {i}",
                "authors": ["Doe, Jane"], "year": "2024"}
    assert scan.start_job([{"url": f"https://a.org/{i}", "comments": []} for i in range(12)],
                          None, enrich=enrich, title_search=lambda *a: None,
                          source={"label": "export.zip"})
    _wait(scan.current_job(), scan.SCANNED)

    rnd = random.Random(seed)
    at = _page()
    for step in range(300):
        job = scan.current_job()
        if job is not None and job.snapshot()["phase"] in (scan.UPLOADING, scan.SCANNING):
            _wait(job, scan.DONE if job.snapshot()["phase"] == scan.UPLOADING else scan.SCANNED)
            at.run()
        buttons = [b for b in at.button if not b.disabled]
        radios = [r for r in at.radio if r.key == "review_view"]
        if radios and rnd.random() < 0.1:
            radios[0].set_value(rnd.choice(["Cards", "Table"])).run()
        elif buttons:
            rnd.choice(buttons).click().run()
        else:
            at.run()
        assert not at.exception, (step, [e.value for e in at.exception])
        if job is not None:
            for p in job.papers:
                if p.get("decision") == "removed":
                    assert not p.get("include")
    assert len(sent) == len(set(sent))                     # nothing uploaded twice


@pytest.mark.parametrize("seed", range(3))
def test_random_table_actions_keep_results_consistent(seed):
    """What the table buttons do to ticked rows, in random order on random
    rows: Move to Review, Remove, Mark ready for upload, Undo, Save edits."""
    import random

    import app as page

    def enrich(p, *a):
        i = int(p["url"].rsplit("/", 1)[1])
        return ({"metadata_source": "none"} if i % 3 == 0 else
                {"metadata_source": "translator", "title": f"Paper {i}",
                 "authors": ["Doe, Jane"], "year": "2024"})
    assert scan.start_job([{"url": f"https://a.org/{i}", "comments": []} for i in range(15)],
                          None, enrich=enrich, title_search=lambda *a: None)
    job = scan.current_job()
    _wait(job, scan.SCANNED)
    rnd = random.Random(seed)
    for _ in range(500):
        ready = job.items(scan.READY)
        left = [p for p in job.items(scan.REVIEW) if not p.get("decision")]
        decided = [p for p in job.items(scan.REVIEW) if p.get("decision")]
        table, action = rnd.choice([(ready, "move"), (ready, "remove"), (left, "mark"),
                                    (left, "remove"), (decided, "undo"), (ready, "edit"),
                                    (left, "edit")])
        if not table:
            continue
        ticked = rnd.sample(table, rnd.randint(1, len(table)))
        if action == "edit":
            urls = [p["url"] for p in table]
            page.apply_review_edits(job, urls, {
                urls.index(p["url"]): {"Title": rnd.choice(["Same", "", "  New  "]),
                                       "Authors": rnd.choice(["Doe,Jane", "", "A; B"]),
                                       "Year": rnd.choice(["2024", "", "x"])}
                for p in ticked})
        else:
            page.table_action(job, action, ticked)
        for p in job.papers:
            assert p["bucket"] in (scan.READY, scan.REVIEW)
            if p["bucket"] == scan.READY:
                assert p["include"] and not p.get("decision")
            elif p.get("decision") in ("correct", "edited"):
                assert p["include"]
            else:
                assert not p["include"]
            assert isinstance(p.get("authors", []), list)
    assert set(map(id, job.selected())) == {id(p) for p in job.papers if p["include"]}
    scan._current = None


def test_stop_asks_in_a_dialog_then_clears_the_search():
    import threading
    gate = threading.Event()

    def enrich(p, *a):
        gate.wait(5)
        return {"metadata_source": "none"}
    assert scan.start_job([{"url": "https://a.org/1", "comments": []}], None,
                          enrich=enrich, title_search=lambda *a: None)
    try:
        at = _page()
        assert "Stop and clear" not in _buttons(at)
        _buttons(at)["Stop"].click().run()
        assert not at.exception
        assert "This stops the search" in _text(at)
        assert scan.current_job().snapshot()["paused"]      # paused while asking
        _buttons(at)["Cancel"].click().run()
        assert "Stop and clear" not in _buttons(at)
        assert not scan.current_job().snapshot()["paused"]  # Cancel resumes it
        scan.current_job().pause()                          # paused by the user:
        at.run()
        _buttons(at)["Stop"].click().run()
        _buttons(at)["Cancel"].click().run()
        assert scan.current_job().snapshot()["paused"]      # Cancel keeps it paused
        _buttons(at)["Stop"].click().run()
        _buttons(at)["Stop and clear"].click().run()
        assert not at.exception
        assert scan.current_job() is None
    finally:
        gate.set()


def test_marked_item_shows_in_ready_and_move_to_review_sends_it_back():
    import app as page
    job = _scanned_job()
    at = _page()
    _buttons(at)["Mark ready for upload"].click().run()
    assert "Ready (2)" in [e.label for e in at.expander]
    page.table_action(job, "move", [_junk(job)])
    assert not _junk(job).get("decision") and not _junk(job)["include"]
    at.run()
    labels = [e.label for e in at.expander]
    assert "Ready (1)" in labels and "Left to review (1)" in labels
    assert "Upload 1 ready item to Zotero" in _buttons(at)


def test_card_edit_fills_author_boxes_from_the_stored_names():
    job = _scanned_job()
    _junk(job)["authors"] = ["Zucman, Gabriel", "Urban Institute"]
    at = _page()
    _buttons(at)["Edit"].click().run()
    import zlib
    k = zlib.crc32(_junk(job)["url"].encode())
    boxes = {b.key: b.value for b in at.text_input}
    assert boxes[f"edit_first_{k}_0"] == "Gabriel" and boxes[f"edit_last_{k}_0"] == "Zucman"
    assert boxes[f"edit_first_{k}_1"] == "" and boxes[f"edit_last_{k}_1"] == "Urban Institute"
    _buttons(at)["Cancel"].click().run()
    _junk(job)["authors"] = ["Saez, Emmanuel"]            # changed meanwhile
    _buttons(at)["Edit"].click().run()
    boxes = {b.key: b.value for b in at.text_input}
    assert boxes[f"edit_last_{k}_0"] == "Saez" and f"edit_last_{k}_1" not in boxes
    at.button(key=f"edit_add_{k}").click().run()
    assert at.text_input(key=f"edit_last_{k}_1").value == ""   # no leftover name
