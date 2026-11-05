"""
Live checks against the outside services the app depends on. Each uses a real,
stable paper, so a failure points at one specific thing that changed. Run by
the weekly GitHub check; skipped in the normal test run.

    pytest -m live
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

import http_fetch
import metadata

pytestmark = pytest.mark.live

LANDLORDS_DOI = "10.1080/24694452.2023.2277810"
LANDLORDS_TITLE = "Where the Landlords Are"


@pytest.fixture(autouse=True)
def fresh_cache():
    http_fetch.clear_cache()


def test_crossref_doi_lookup():
    found = metadata.lookup_doi(LANDLORDS_DOI)
    assert found, "CrossRef did not return the record for a known DOI"
    assert found[1]["title"].startswith(LANDLORDS_TITLE)


def test_crossref_title_search():
    found = metadata.search_crossref_title("Where the Landlrods Are", "Preis")
    assert found, "CrossRef title search no longer finds a known paper"
    assert found[1]["doi"] == LANDLORDS_DOI


def test_ssrn_doi_rule():
    doi = metadata.ssrn_doi("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3980758")
    found = metadata.lookup_doi(doi)
    assert found, f"SSRN's DOI scheme ({doi}) no longer resolves on CrossRef"
    assert "Two-Way Fixed Effects" in found[1]["title"]


def test_citation_tags_on_a_publisher_page():
    found = metadata.fetch_citation_metadata("https://www.nber.org/papers/w32373")
    assert found, "NBER's page no longer has readable citation_* tags"
    assert found[1].get("doi") == "10.3386/w32373"


def test_citoid():
    raw = metadata.fetch_translator("https://arxiv.org/abs/2401.16440")
    assert raw, "Citoid returned nothing for a known arXiv paper"
    assert "Eviction" in (raw.get("title") or "")


# ── Python end of life ────────────────────────────────────────────────────────
# Python releases a new version every October and supports each for five years
# (PEP 602), so 3.N stops getting security fixes in October of 2016 + N
# (3.12 in 2028, 3.14 in 2030). Streamlit Cloud then moves apps to a newer
# Python, which may break them. This fails six months ahead as a reminder.

def test_python_version_is_not_near_end_of_life():
    version = (pathlib.Path(__file__).parent / ".python-version").read_text().strip()
    minor = int(version.split(".")[1])
    end_of_life = datetime.date(2016 + minor, 10, 1)
    warn_from = end_of_life - datetime.timedelta(days=183)
    assert datetime.date.today() < warn_from, (
        f"Python {version} stops getting security fixes around {end_of_life:%B %Y}. "
        "Redeploy the app on the newest Python Streamlit Cloud offers (delete the app "
        "and deploy it again, choosing the version under Advanced settings), then "
        "update .python-version and the --python-version in requirements.in.")
