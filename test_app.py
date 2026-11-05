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
    assert "**Ready (1)**" in text                   # the results are still there
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
