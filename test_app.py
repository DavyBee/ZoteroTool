"""Headless UI tests for app.py using Streamlit's AppTest."""

from __future__ import annotations

import time

import pytest
from streamlit.testing.v1 import AppTest

import scan


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "test")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scan._current = None
    yield
    scan._current = None


def _scanned_job():
    def enrich(p, key, log):
        if p["url"].endswith("junk"):
            return {"metadata_source": "non_paper"}
        return {"metadata_source": "crossref_doi", "title": "A Paper",
                "authors": ["Doe, Jane"], "year": "2024"}

    papers = [{"url": "https://a.org/paper", "comments": []},
              {"url": "https://a.org/junk", "comments": []}]
    assert scan.start_job(papers, None, enrich=enrich)
    job = scan.current_job()
    end = time.time() + 5
    while job.snapshot()["phase"] != scan.SCANNED:
        assert time.time() < end
        time.sleep(0.01)
    return job


def _buttons(at):
    return {b.label: b for b in at.button}


def test_start_page():
    at = AppTest.from_file("app.py", default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Slack to Zotero"
    assert _buttons(at)["Scan"].disabled             # nothing uploaded yet
    assert not at.warning                             # Zotero is configured


def test_missing_zotero_config_is_reported(monkeypatch):
    monkeypatch.delenv("ZOTERO_API_KEY")
    at = AppTest.from_file("app.py", default_timeout=30).run()
    assert "Zotero is not configured" in at.warning[0].value


def test_results_survive_a_new_session_and_start_over_clears_them():
    _scanned_job()
    at = AppTest.from_file("app.py", default_timeout=30).run()           # a fresh session = a refresh
    assert not at.exception
    headers = [h.value for h in at.subheader]
    assert headers == ["Ready (1)", "Review (1)"]
    assert "Upload 1 to Zotero" in _buttons(at)

    _buttons(at)["Start over"].click().run()
    assert scan.current_job() is None
    assert "Scan" in _buttons(at)


def test_no_emoji_in_page_text():
    _scanned_job()
    at = AppTest.from_file("app.py", default_timeout=30).run()
    text = " ".join(
        [e.value for e in at.markdown] + [e.value for e in at.caption]
        + [e.value for e in at.subheader] + [b.label for b in at.button])
    assert all(ord(ch) < 0x2190 for ch in text), text
