"""Headless page tests for app.py using Streamlit's AppTest."""

from __future__ import annotations

import os
import sys
import time

import pytest
from streamlit.testing.v1 import AppTest

import scan


LOCAL_MODULES = ("records", "config", "http_fetch", "slack_parser", "pdf_storage",
                 "zotero_index", "metadata", "zotero_client", "scan")


def _code_stamp():
    here = os.path.dirname(os.path.abspath(__file__))
    return tuple(os.path.getmtime(os.path.join(here, f"{m}.py")) for m in LOCAL_MODULES)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # The modules these tests imported are current, so the page should use them.
    monkeypatch.setattr(sys, "_slack_to_zotero_code", _code_stamp(), raising=False)
    import zotero_client
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
    return AppTest.from_file("app.py", default_timeout=30).run()


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
    assert "Zotero is not configured" in at.warning[0].value


def test_finished_scan_keeps_every_step_on_the_page():
    _scanned_job()
    at = _page()                                     # a fresh session = a refresh
    assert not at.exception
    text = _text(at)
    assert "Done · export.zip" in text
    assert "1 ready · 1 review · 0 already in Zotero" in text
    assert "**Ready (1)**" in text and "**Review (1)**" in text
    assert "Upload 1 to Zotero" in _buttons(at)


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
    assert "**Review (1)**" in text                  # Review stays
    assert "New search" in _buttons(at)
    assert "Upload 1 to Zotero" not in _buttons(at)   # nothing left selected

    job.set_include("https://a.org/junk", True)       # pick something from Review
    at = _page()
    assert "1 more selected." in _text(at)
    assert "Upload 1 to Zotero" in _buttons(at)


def test_no_emoji_in_page_text():
    _scanned_job()
    text = _text(_page())
    assert all(ord(ch) < 0x2190 for ch in text), text


def test_changed_code_is_loaded_fresh(monkeypatch):
    saved = {m: sys.modules[m] for m in LOCAL_MODULES if m in sys.modules}
    stopped = []

    class RunningJob:
        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(scan, "_current", RunningJob())
    try:
        monkeypatch.setattr(sys, "_slack_to_zotero_code", ("old",), raising=False)
        at = _page()
        assert not at.exception
        assert sys.modules["scan"] is not saved["scan"]      # imported again
        assert stopped == [True]                             # the old scan was stopped
        assert sys._slack_to_zotero_code == _code_stamp()
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
    text = _text(at)
    assert "**Ready (1)**" in text and "**Review (0 of 1)**" in text
    at.text_input(key="results_query").input("nothing matches this").run()
    assert "**Ready (0 of 1)**" in _text(at)


# ── Review cards ──────────────────────────────────────────────────────────────

def _junk(job):
    return next(p for p in job.papers if p["url"].endswith("junk"))


def test_review_card_correct_adds_the_item_to_the_upload():
    job = _scanned_job()
    at = _page()
    assert "1 of 1 · 1 left to review" in _text(at)
    _buttons(at)["Correct"].click().run()
    assert _junk(job)["decision"] == "correct" and _junk(job)["include"]
    assert "Upload 2 to Zotero" in _buttons(at)
    assert "All items reviewed." in _text(at)
    assert "Marked for upload (1)" in [e.label for e in at.expander]


def test_review_card_remove_leaves_the_item_out_and_can_be_undone():
    job = _scanned_job()
    at = _page()
    assert "Not a source" not in _buttons(at)
    _buttons(at)["Remove"].click().run()
    assert _junk(job)["decision"] == "removed" and not _junk(job)["include"]
    assert "Upload 1 to Zotero" in _buttons(at)
    assert "All items reviewed." in _text(at)
    assert "Removed (1)" in [e.label for e in at.expander]
    _buttons(at)["Undo"].click().run()
    assert "decision" not in _junk(job)
    assert "1 of 1 · 1 left to review" in _text(at)


def test_review_card_edit_saves_the_fields_and_uploads():
    job = _scanned_job()
    at = _page()
    _buttons(at)["Edit"].click().run()
    import zlib
    k = zlib.crc32(_junk(job)["url"].encode())
    at.text_input(key=f"edit_title_{k}").input("The Real Title")
    at.text_input(key=f"edit_authors_{k}").input("Doe, Jane; Roe, Rich")
    at.text_input(key=f"edit_year_{k}").input("2023")
    _buttons(at)["Save"].click().run()
    p = _junk(job)
    assert (p["decision"], p["title"], p["authors"], p["year"]) == \
        ("edited", "The Real Title", ["Doe, Jane", "Roe, Rich"], "2023")
    assert p["include"] and "Upload 2 to Zotero" in _buttons(at)


def test_review_table_view_is_still_available():
    _scanned_job()
    at = _page()
    at.radio(key="review_view").set_value("Table").run()
    assert not at.exception
    assert "Correct" not in _buttons(at)


# ── Confirming an upload ──────────────────────────────────────────────────────

def test_upload_asks_first_when_items_are_left_out():
    job = _scanned_job()                    # 1 ready, 1 review item not marked
    at = _page()
    _buttons(at)["Upload 1 to Zotero"].click().run()
    text = " ".join(m.value for m in at.markdown)
    assert "Upload 1 item to Zotero?" in text
    assert "1 in Review isn't marked for upload." in text
    assert job.snapshot()["phase"] == scan.SCANNED        # nothing uploaded yet
    _buttons(at)["Cancel"].click().run()
    assert job.snapshot()["phase"] == scan.SCANNED


def test_upload_goes_straight_through_when_nothing_is_left_out(monkeypatch):
    job = _scanned_job()
    job.decide(_junk(job)["url"], "removed")
    started = []
    monkeypatch.setattr(job, "start_upload", lambda make: started.append(True))
    at = _page()
    _buttons(at)["Upload 1 to Zotero"].click().run()
    assert started == [True]


def test_card_shows_only_the_original_slack_message():
    job = _scanned_job()
    junk = _junk(job)
    junk["comments"] = [{"author": "Lorae", "ts": "1", "text": "", "is_reply": False},
                        {"author": "Ben", "ts": "2", "text": "agreed", "is_reply": True}]
    text = _text(_page())
    assert "**Original Slack message:** Lorae: :gray[only the link, no text]" in text
    assert "agreed" not in text and "replies" not in text
