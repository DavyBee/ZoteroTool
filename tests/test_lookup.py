"""Tests for metadata lookups: citation tags, SSRN DOIs, the page cache, the
lookup order, and reading the AI reply."""

from __future__ import annotations

import sys
import types

import pytest

from slack_to_zotero import http_fetch
from slack_to_zotero import metadata


@pytest.fixture(autouse=True)
def fresh_cache():
    http_fetch.clear_cache()
    yield
    http_fetch.clear_cache()


NBER_PAGE = b"""<html><head>
<meta name="citation_title" content="Estimating Racial Disparities When Race is Not Observed">
<meta name="citation_author" content="Cory McCartan">
<meta name="citation_author" content="Robin Fisher">
<meta name="citation_doi" content="10.3386/w32373">
<meta name="citation_technical_report_institution" content="National Bureau of Economic Research">
<meta name="citation_publication_date" content="2024/04/01">
</head><body>...</body></html>"""

ARXIV_PAGE = """<html><head>
<meta content="Beyond Eviction Prediction: Leveraging Local Spatiotemporal Public Records" name="citation_title"/>
<meta name="citation_author" content="Tegan, Mohan"/>
<meta name="citation_author" content="Clément de Chaisemartin"/>
<meta name="citation_date" content="2024/01/29"/>
<meta name="citation_arxiv_id" content="2401.16440"/>
<meta name="CITATION_ABSTRACT" content="We study &amp; predict evictions."/>
</head></html>""".encode("utf-8")


# ── Citation tags ─────────────────────────────────────────────────────────────

def test_tags_from_an_arxiv_style_page():
    tags = metadata.parse_citation_tags(ARXIV_PAGE)
    f = metadata.tags_to_fields(tags)
    assert f["title"].startswith("Beyond Eviction Prediction")
    assert f["authors"] == ["Tegan, Mohan", "de Chaisemartin, Clément"]
    assert (f["item_type"], f["journal"], f["year"]) == ("preprint", "arXiv", "2024")
    assert f["abstract"] == "We study & predict evictions."
    assert f["doi"] is None


def test_tags_need_a_title_and_an_author():
    assert metadata.tags_to_fields(metadata.parse_citation_tags(
        b'<meta name="citation_title" content="A Journal Homepage">')) is None
    assert metadata.tags_to_fields(metadata.parse_citation_tags(b"<html>no tags</html>")) is None


@pytest.mark.parametrize("raw, expected", [
    ("Rebecca Diamond", "Diamond, Rebecca"),
    ("Diamond, Rebecca", "Diamond, Rebecca"),
    ("Clément de Chaisemartin", "de Chaisemartin, Clément"),
    ("Ludwig van der Rohe", "van der Rohe, Ludwig"),
    ("Martin Luther King Jr.", "King, Martin Luther Jr."),
    ("Plato", "Plato"),
])
def test_tag_author_names(raw, expected):
    assert metadata._tag_author(raw) == expected


def _serve(monkeypatch, pages: dict):
    def fake(url, *, max_bytes, timeout=None, retries=1):
        body = pages.get(url)
        return (body, "text/html; charset=utf-8", 200) if body else (None, "", 403)
    monkeypatch.setattr(http_fetch, "_fetch", fake)


def test_tag_doi_goes_to_crossref_when_titles_agree(monkeypatch):
    _serve(monkeypatch, {"https://nber.org/w": NBER_PAGE})
    monkeypatch.setattr(metadata, "lookup_doi", lambda doi: ("crossref_doi", {
        "title": "Estimating Racial Disparities When Race is Not Observed", "doi": doi}))
    source, fields = metadata.fetch_citation_metadata("https://nber.org/w")
    assert source == "crossref_doi" and fields["doi"] == "10.3386/w32373"


def test_tag_doi_is_ignored_when_crossref_title_differs(monkeypatch):
    _serve(monkeypatch, {"https://nber.org/w": NBER_PAGE})
    monkeypatch.setattr(metadata, "lookup_doi", lambda doi: ("crossref_doi", {
        "title": "Some Other Work Entirely"}))
    source, fields = metadata.fetch_citation_metadata("https://nber.org/w")
    assert source == "citation_tags"
    assert fields["authors"] == ["McCartan, Cory", "Fisher, Robin"]
    assert fields["item_type"] == "report"


def test_blocked_page_gives_nothing(monkeypatch):
    _serve(monkeypatch, {})
    assert metadata.fetch_citation_metadata("https://blocked.org") is None


# ── SSRN ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, doi", [
    ("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4554831", "10.2139/ssrn.4554831"),
    ("https://ssrn.com/abstract=3980758", "10.2139/ssrn.3980758"),
    ("https://papers.ssrn.com/abstract=123", "10.2139/ssrn.123"),
    ("https://papers.ssrn.com/sol3/Delivery.cfm?abstractid=55", "10.2139/ssrn.55"),
    ("https://www.ssrn.com/index.cfm/en/", None),
    ("https://notssrn.com/abstract=1", None),
])
def test_ssrn_doi(url, doi):
    assert metadata.ssrn_doi(url) == doi


# ── Lookup order ──────────────────────────────────────────────────────────────

def _stub_stages(monkeypatch, calls, **results):
    monkeypatch.setattr(metadata, "save_pdf", lambda url: None)

    def stage(name):
        def fn(*a, **k):
            calls.append(name)
            return results.get(name)
        return fn
    monkeypatch.setattr(metadata, "lookup_doi", stage("crossref"))
    monkeypatch.setattr(metadata, "fetch_citation_metadata", stage("tags"))
    monkeypatch.setattr(metadata, "fetch_translator", stage("citoid"))


def test_ssrn_is_resolved_before_any_page_read(monkeypatch):
    calls = []
    _stub_stages(monkeypatch, calls, crossref=("crossref_doi", {"title": "T"}))
    out = metadata.enrich_paper(
        {"url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1"}, None, "/dev/null")
    assert calls == ["crossref"] and out["metadata_source"] == "crossref_doi"


def test_unconfirmed_ssrn_doi_falls_through(monkeypatch):
    calls = []
    _stub_stages(monkeypatch, calls, citoid={"itemType": "preprint", "title": "T"})
    metadata.enrich_paper(
        {"url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1"}, None, "/dev/null")
    assert calls == ["crossref", "tags", "citoid"]


def test_citation_tags_come_before_citoid(monkeypatch):
    calls = []
    _stub_stages(monkeypatch, calls, tags=("citation_tags", {"title": "T", "authors": ["A"]}))
    out = metadata.enrich_paper({"url": "https://a.org/p"}, None, "/dev/null")
    assert calls == ["tags"] and out["metadata_source"] == "citation_tags"


# ── Page cache ────────────────────────────────────────────────────────────────

def test_page_is_fetched_once_and_large_bodies_are_not_kept(monkeypatch):
    hits = []

    def fake(url, *, max_bytes, timeout=None, retries=1):
        hits.append(url)
        size = 10 if "small" in url else http_fetch.CACHE_MAX_BODY + 1
        return b"x" * min(size, max_bytes), "text/html", 200

    monkeypatch.setattr(http_fetch, "_fetch", fake)
    http_fetch.fetch("https://small", max_bytes=1000)
    http_fetch.fetch("https://small", max_bytes=500)
    assert hits == ["https://small"]
    http_fetch.fetch("https://big", max_bytes=10**8)
    http_fetch.fetch("https://big", max_bytes=10**8)
    assert hits.count("https://big") == 2


def test_cached_truncated_body_does_not_answer_a_bigger_request(monkeypatch):
    hits = []

    def fake(url, *, max_bytes, timeout=None, retries=1):
        hits.append(max_bytes)
        return b"y" * min(5000, max_bytes), "text/html", 200

    monkeypatch.setattr(http_fetch, "_fetch", fake)
    http_fetch.fetch("https://p", max_bytes=1000)        # truncated at 1000
    body, _, _ = http_fetch.fetch("https://p", max_bytes=4000)
    assert hits == [1000, 4000] and len(body) == 4000


# ── Reading the AI reply ──────────────────────────────────────────────────────

class _Block:
    def __init__(self, type, text=None):
        self.type = type
        if text is not None:
            self.text = text


def _fake_anthropic(monkeypatch, content, stop_reason="end_turn"):
    sent = {}

    class Messages:
        def create(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(content=content, stop_reason=stop_reason)

    class Client:
        def __init__(self, api_key=None):
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    return sent


REPLY = ('{"title": "T", "authors": ["Doe, Jane"], "year": "2024", "journal": null, '
         '"doi": null, "item_type": "journalArticle", "category": "paper", "summary": "s"}')


def test_ai_reply_is_read_even_when_a_thinking_block_comes_first(monkeypatch, tmp_path):
    sent = _fake_anthropic(monkeypatch, [_Block("thinking"), _Block("text", REPLY)])
    out = metadata.fetch_llm("https://a.org", [], "key", str(tmp_path / "audit.jsonl"),
                             page_text="some page text " * 50)
    assert out is not None and out["title"] == "T"
    assert "temperature" not in sent and "output_config" not in sent
    assert sent["max_tokens"] >= 4000


def test_ai_reply_without_text_is_an_error_not_a_crash(monkeypatch, tmp_path):
    _fake_anthropic(monkeypatch, [_Block("thinking")], stop_reason="max_tokens")
    out = metadata.fetch_llm("https://a.org", [], "key", str(tmp_path / "audit.jsonl"),
                             page_text="x " * 50)
    assert out is None
    assert "no text in response" in (tmp_path / "audit.jsonl").read_text()


@pytest.mark.parametrize("doi, cleaned", [
    ("10.2139/", None), ("10.1093/", None), ("10.2139", None), ("", None), ("not-a-doi", None),
    (None, None), ("10.2139/ssrn.4445621", "10.2139/ssrn.4445621"),
    ("10.1093/sf/soab123.", "10.1093/sf/soab123"),
])
def test_clean_doi(doi, cleaned):
    assert metadata._clean_doi(doi) == cleaned


def test_page_title_prefers_og_title(monkeypatch):
    _serve(monkeypatch, {
        "https://a.org": b'<html><head><title>Home | Site</title>'
                         b'<meta property="og:title" content="Housing Policy Debate"></head></html>',
        "https://b.org": b"<html><head><title>  Eviction\n Lab  Data </title></head></html>",
    })
    assert metadata.page_title("https://a.org") == "Housing Policy Debate"
    assert metadata.page_title("https://b.org") == "Eviction Lab Data"
    assert metadata.page_title("https://blocked.org") is None


def test_tweets_get_a_title_without_any_lookup(monkeypatch):
    calls = []
    _stub_stages(monkeypatch, calls)
    out = metadata.enrich_paper({"url": "https://x.com/LevyAntoine/status/1"}, None, "/dev/null")
    assert calls == []
    assert (out["metadata_source"], out["title"], out["item_type"]) == \
        ("link", "Post by @LevyAntoine", "webpage")


def test_link_title_skips_titles_that_are_just_the_site_name(monkeypatch):
    _serve(monkeypatch, {
        "https://osf.io/x": b"<title>OSF</title>",
        "https://jchs.org/report": b"<title>The State of the Nation's Housing</title>",
    })
    assert metadata._link_title("https://osf.io/x", "Preprint on minimum wages.") == \
        "Preprint on minimum wages"
    assert metadata._link_title("https://jchs.org/report", "x") == "The State of the Nation's Housing"
    assert metadata._link_title("https://blocked.org", None) == "https://blocked.org"


def test_links_to_slack_messages_are_not_sources(monkeypatch):
    calls = []
    _stub_stages(monkeypatch, calls)
    out = metadata.enrich_paper(
        {"url": "https://lab.slack.com/archives/C6ARJE84B/p1762635072886359"}, None, "/dev/null")
    assert calls == [] and out["metadata_source"] == "non_paper"
    assert not metadata._is_slack_message("https://lab.slack.com/files/U1/F1/paper.pdf")


def test_titles_lose_html_tags():
    assert metadata.clean_title("<i>Upsold:</i> Real Estate &amp; Inequality") == \
        "Upsold: Real Estate & Inequality"


def test_citoid_title_that_is_only_the_site_name_is_rejected():
    assert metadata._is_translator_junk({"title": "- YouTube", "date": "2024"},
                                        "https://www.youtube.com/watch?v=1")
    assert not metadata._is_translator_junk({"title": "A Real Talk", "date": "2024"},
                                            "https://www.youtube.com/watch?v=1")


@pytest.mark.parametrize("record, venue", [
    ({"type": "posted-content", "DOI": "10.2139/ssrn.1", "publisher": "Elsevier BV"}, "SSRN"),
    ({"type": "journal-article", "DOI": "10.1234/x", "publisher": "Elsevier BV"}, None),
    ({"type": "report", "DOI": "10.3386/w1", "publisher": "NBER"}, "NBER"),
    ({"type": "journal-article", "container-title": ["Demography"], "publisher": "Duke"}, "Demography"),
])
def test_journal_falls_back_to_publisher_only_for_reports_and_preprints(record, venue):
    assert metadata.crossref_to_fields({"title": ["T"], **record})["journal"] == venue


def test_pdf_text_extraction_stops_after_enough_text(monkeypatch):
    read = []

    class Page:
        def __init__(self, i):
            self.i = i

        def extract_text(self):
            read.append(self.i)
            return "x" * 1000

    class Reader:
        def __init__(self, _):
            self.pages = [Page(i) for i in range(300)]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    text = metadata._extract_pdf_text(b"%PDF", max_chars=5000)
    assert len(read) == 5 and len(text) >= 5000
