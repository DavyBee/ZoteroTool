"""Tests for the Zotero duplicate check (zotero_index) and its use in a scan,
plus the "Post an article" title hints."""

from __future__ import annotations

import time

import pytest

import scan
import zotero_client
import zotero_index as zi


def item(key, **data):
    return {"key": key, "data": {"key": key, "itemType": "journalArticle", **data}}


@pytest.fixture
def index():
    ix = zi.LibraryIndex("https://www.zotero.org/groups/1")
    for it in [
        item("DOI1", DOI="https://doi.org/10.1234/ABC", title="Evictions"),
        item("EXTRA", itemType="report", extra="Type: x\nDOI: 10.5555/xyz"),
        item("URL1", url="http://www.nber.org/papers/w1?utm_source=a&amp;utm_medium=b"),
        item("SSRN", url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=111"),
        item("TY", title="The Rent Is Too High", date="2021-03-01",
             creators=[{"lastName": "Smith", "firstName": "A"}]),
        item("TA", title="Housing Policy Review", creators=[{"name": "Urban Institute"}]),
        item("TRASH", url="https://trashed.org/p", deleted=1),
        {"key": "N1", "data": {"key": "N1", "itemType": "note", "note": "x"}},
    ]:
        ix.add(it)
    return ix


# ── Normalization ─────────────────────────────────────────────────────────────

def test_url_key_ignores_scheme_www_slash_and_tracking():
    assert zi.url_key("http://www.NBER.org/papers/w1/?utm_source=x") == \
        zi.url_key("https://nber.org/papers/w1")
    assert zi.url_key("https://a.org/P") != zi.url_key("https://a.org/p")
    assert zi.url_key("https://ssrn.com/x?abstract_id=1") != \
        zi.url_key("https://ssrn.com/x?abstract_id=2")


def test_doi_key():
    assert zi.doi_key("https://dx.doi.org/10.1234/AbC.") == "10.1234/abc"
    assert zi.doi_key("doi: 10.1234/x") == "10.1234/x"
    assert zi.doi_key("10.2139/") is None


def test_titles_match():
    assert zi.titles_match("Where the Landlords Are: A Network Approach",
                           "Where the landlords are")
    assert zi.titles_match("The Rent Is Too High!", "the rent is too high")
    assert not zi.titles_match("Evictions and Health", "Land Inequalities in the US")


# ── Matching ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("paper, expected", [
    ({"url": "https://x.org", "doi": "10.1234/abc"}, ("DOI1", "DOI")),
    ({"url": "https://doi.org/10.1234/ABC"}, ("DOI1", "DOI")),
    ({"url": "https://x.org", "doi": "10.5555/XYZ"}, ("EXTRA", "DOI")),
    ({"url": "https://nber.org/papers/w1"}, ("URL1", "URL")),
    ({"url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=111"}, ("SSRN", "URL")),
    ({"url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=222"}, None),
    ({"url": "https://trashed.org/p"}, None),
])
def test_match_identifiers(index, paper, expected):
    assert index.match_identifiers(paper) == expected


def test_title_needs_matching_year_or_author(index):
    base = {"url": "https://other.org", "title": "The rent is too high"}
    assert index.match({**base, "year": "2021"}) == ("TY", "Title")
    assert index.match({**base, "year": "2019"}) is None
    assert index.match({**base, "authors": ["Smith, A"]}) == ("TY", "Title")
    assert index.match({**base, "authors": ["Jones, B"]}) is None
    assert index.match(base) is None
    assert index.match({"url": "https://o.org", "title": "Housing policy review",
                        "authors": ["Urban Institute"]}) == ("TA", "Title")


def test_ignores_notes_and_trash(index):
    assert index.size == 6


def test_item_url(index):
    assert index.item_url("DOI1") == "https://www.zotero.org/groups/1/items/DOI1"


# ── Reading Zotero ────────────────────────────────────────────────────────────

class FakeZot:
    def __init__(self, items, version=10):
        self.items, self.version, self.calls = items, version, []

    def last_modified_version(self):
        return self.version

    def top(self, **kw):
        self.calls.append(("top", kw))
        return self._select(kw)

    def collection_items_top(self, coll, **kw):
        self.calls.append(("coll", coll, kw))
        return self._select(kw)

    def _select(self, kw):
        since = kw.get("since", 0)
        return [i for i in self.items if i["data"].get("version", 1) > since]

    def everything(self, result):
        return result

    def key_info(self):
        return {"username": "someone"}


def test_load_and_refresh_reads_only_new_items():
    zot = FakeZot([item("A", url="https://a.org", version=5)])
    ix = zi.load(zot, "user", "9", collection="COLL")
    assert ix.web_base == "https://www.zotero.org/someone"
    assert zot.calls[0][:2] == ("coll", "COLL")
    assert ix.match_identifiers({"url": "https://a.org"})

    zi.refresh(ix, zot, "COLL")                      # version unchanged: no request
    assert len(zot.calls) == 1
    zot.items.append(item("B", url="https://b.org", version=12))
    zot.version = 12
    zi.refresh(ix, zot, "COLL")
    assert zot.calls[-1][2]["since"] == 10
    assert ix.match_identifiers({"url": "https://b.org"})
    assert ix.version == 12


# ── In a scan ─────────────────────────────────────────────────────────────────

def _wait(job, phases, timeout=5):
    end = time.time() + timeout
    while job.snapshot()["phase"] not in phases:
        assert time.time() < end, job.snapshot()["phase"]
        time.sleep(0.01)


def _job(papers, enrich, index=None, **kw):
    kw.setdefault("title_search", lambda *a: None)
    job = scan.Job(papers, None, enrich=enrich,
                   load_index=(lambda: index) if index is not None else None, **kw)
    job.start()
    _wait(job, (scan.SCANNED, scan.FAILED))
    return job


def test_known_items_skip_enrichment_and_show_as_existing(index):
    called = []

    def enrich(p, *a):
        called.append(p["url"])
        return {"metadata_source": "crossref_doi", "title": "The Rent Is Too High",
                "year": "2021"}

    job = _job([{"url": "https://nber.org/papers/w1", "comments": []},
                {"url": "https://new.org/rent", "comments": []},
                {"url": "https://new.org/fresh", "comments": []}],
               lambda p, *a: enrich(p) if p["url"].endswith("rent")
               else (called.append(p["url"]) or {"metadata_source": "translator",
                                                 "title": "Fresh"}),
               index)
    assert "https://nber.org/papers/w1" not in called         # matched before enrich
    existing = {p["url"]: p for p in job.items(scan.EXISTING)}
    assert existing["https://nber.org/papers/w1"]["reason"] == "URL"
    assert existing["https://new.org/rent"]["reason"] == "Title"
    assert existing["https://new.org/rent"]["existing_url"].endswith("/items/TY")
    assert [p["title"] for p in job.items(scan.READY)] == ["Fresh"]
    assert [p["url"] for p in job.selected()] == ["https://new.org/fresh"]
    job.set_include("https://nber.org/papers/w1", True)       # can't select existing
    assert len(job.selected()) == 1


def test_unreadable_library_fails_the_scan():
    def boom():
        raise ConnectionError("403 Forbidden")
    job = scan.Job([{"url": "https://a.org", "comments": []}], None,
                   enrich=lambda *a: {}, load_index=boom)
    job.start()
    _wait(job, (scan.FAILED,))
    assert "Could not read the Zotero library" in job.snapshot()["fatal_error"]
    scan._current = job
    scan.clear_job()
    assert scan.current_job() is None


def test_items_added_to_zotero_after_the_scan_are_skipped_on_upload():
    ix = zi.LibraryIndex()
    papers = [{"url": f"https://a.org/{i}", "comments": []} for i in range(2)]

    def refresh(index):
        index.add(item("LATE", url="https://a.org/1"))

    job = scan.Job(papers, None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]},
                   load_index=lambda: ix, refresh_index=refresh)
    job.start()
    _wait(job, (scan.SCANNED,))
    sent = []
    job.start_upload(lambda: None, upload=lambda ps, zot, progress_cb: (
        sent.extend(ps) or {"uploaded": len(ps), "failed": 0, "errors": []}))
    _wait(job, (scan.DONE,))
    assert [p["url"] for p in sent] == ["https://a.org/0"]
    assert job.snapshot()["skipped_existing"] == 1


def test_same_work_twice_in_one_export_is_merged():
    papers = [
        {"url": "https://doi.org/10.1234/w", "comments": [{"ts": "1", "author": "A", "text": ""}]},
        {"url": "https://pub.org/w", "comments": [{"ts": "2", "author": "B", "text": ""}]},
    ]
    job = _job(papers, lambda p, *a: {"metadata_source": "crossref_doi", "title": "W",
                                      "doi": "10.1234/W"})
    [p] = job.items(scan.READY)
    assert [c["author"] for c in p["comments"]] == ["A", "B"]


# ── Workflow title hints ──────────────────────────────────────────────────────

HINT = {"title": "Where the Landlords Are", "authors": "Preis & Lee"}


def _hinted(enrich_fields, title_search=lambda *a: None):
    job = _job([{"url": "https://a.org", "comments": [], "slack_hints": HINT}],
               lambda p, *a: dict(enrich_fields), title_search=title_search)
    [p] = job.papers
    return p


def test_hint_confirms_low_context_ai_result():
    p = _hinted({"metadata_source": "llm", "low_context": True,
                 "title": "Where the Landlords Are: A Network Approach"})
    assert p["bucket"] == scan.READY


def test_hint_mismatch_goes_to_review():
    p = _hinted({"metadata_source": "translator", "title": "Journal Homepage"})
    assert (p["bucket"], p["reason"]) == (scan.REVIEW, "Title differs from Slack post")


def test_hint_title_is_searched_on_crossref_when_nothing_found():
    calls = []

    def search(title, author=None):
        calls.append((title, author))
        return ("crossref_doi", {"title": "Where the Landlords Are: A Network Approach",
                                 "doi": "10.1/x", "year": "2024"})

    p = _hinted({"metadata_source": "needs_manual"}, search)
    assert calls == [("Where the Landlords Are", "Preis")]
    assert p["bucket"] == scan.READY and p["doi"] == "10.1/x"


def test_hint_title_used_when_crossref_finds_nothing():
    p = _hinted({"metadata_source": "needs_manual"})
    assert p["title"] == "Where the Landlords Are"
    assert (p["bucket"], p["reason"]) == (scan.REVIEW, "Title from Slack post only")


# ── Upload content ────────────────────────────────────────────────────────────

def test_uploaded_items_have_no_tags():
    it = zotero_client.paper_to_zotero_item(
        {"url": "https://a.org", "title": "T", "keywords": ["eviction", "rent"]})
    assert "tags" not in it


def test_existing_items_show_the_zotero_title():
    ix = zi.LibraryIndex()
    ix.add(item("K", title="Stored Title", url="https://a.org/x"))
    job = _job([{"url": "https://a.org/x", "comments": []}], lambda *a: {}, ix)
    [p] = job.items(scan.EXISTING)
    assert p["existing_title"] == "Stored Title"


def test_file_name_is_readable_and_has_no_directory():
    name = zotero_client._file_name(
        {"title": "Where is it? A/B Test", "authors": ["Diamond, Rebecca"], "year": "2021"},
        "/tmp/pdfs/abc123.pdf")
    assert name == "Diamond 2021 - Where is it AB Test.pdf"
    assert zotero_client._file_name({}, "x/y.docx") == "Full Text.docx"


def test_attachment_failure_is_reported_not_silent(monkeypatch, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    class Zot:
        def create_items(self, items):
            if items[0]["itemType"] == "attachment":
                return {"successful": {}, "failed": {"0": {"code": 400, "message": "nope"}}}
            return {"successful": {"0": {"data": {"key": "P1"}}}, "failed": {}}

    paper = {"url": "https://a.org", "title": "T", "local_pdf_path": str(pdf)}
    monkeypatch.setattr(zotero_client.pdf_storage, "ensure_local_pdf", lambda p: str(pdf))
    result = zotero_client.upload_papers([paper], Zot())
    assert result["uploaded"] == 1 and result["pdfs_attached"] == 0
    assert any("nope" in msg for _, msg in result["errors"])


FULL = "Where the Landlords Are: A Network Approach to Landlord-Rental Locations"


@pytest.mark.parametrize("typed, fetched, same", [
    ("Where the Landlrods Are: A Network Approach to Landlord-Rental Locations", FULL, True),
    ("Where the Landlrods Are", FULL, True),
    ("Were the Landlrods Are", FULL, True),
    ("Where Landlords Are: A Network Approach to Landlord Rental Locations", FULL, True),
    ("Land Inequalites in the United States", "Land Inequalities in the United States", True),
    ("WHERE THE LANDLORDS ARE - a network approach", FULL, True),
    ("Evictions and Child Health", FULL, False),
    ("Where the Tenants Are", FULL, False),
    ("Where the Landlords Live", FULL, False),
    ("Journal of Urban Economics", "Where the Landlords Are", False),
    ("Eviction and Health: Part 1", "Eviction and Health: Part 2", False),
    ("Evictions", "Evictions in America: A Survey", False),
])
def test_titles_match_tolerates_typos_not_different_titles(typed, fetched, same):
    assert zi.titles_match(fetched, typed) is same


def test_crossref_title_search_needs_author_or_a_specific_title(monkeypatch):
    import metadata
    monkeypatch.setattr(metadata, "_http_get_json", lambda url: {"message": {"items": [
        {"DOI": "10.1234/x", "title": ["Evictions"]}]}})
    monkeypatch.setattr(metadata, "lookup_doi", lambda doi: (
        "crossref_doi", {"title": "Evictions", "authors": ["Smith, A"]}))
    assert metadata.search_crossref_title("Evictions") is None          # too generic
    assert metadata.search_crossref_title("Evictions", "Jones") is None  # wrong author
    assert metadata.search_crossref_title("Evictions", "Smtih") is not None  # author typo ok


def test_temporary_attachment_failure_is_retried(monkeypatch, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(zotero_client, "ATTACH_RETRY_WAIT", 0)
    calls = []

    def flaky(zot, key, path, name):
        calls.append(1)
        if len(calls) == 1:
            raise zotero_client.TransientError("attachment item rejected: An error occurred")

    monkeypatch.setattr(zotero_client, "attach_file", flaky)
    zotero_client.attach_file_with_retry(None, "K", str(pdf), "a.pdf")
    assert len(calls) == 2


def test_permanent_attachment_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(zotero_client, "ATTACH_RETRY_WAIT", 0)
    calls = []

    def bad(*a):
        calls.append(1)
        raise RuntimeError("HTTP 400: bad filename")

    monkeypatch.setattr(zotero_client, "attach_file", bad)
    with pytest.raises(RuntimeError):
        zotero_client.attach_file_with_retry(None, "K", "x.pdf", "x.pdf")
    assert len(calls) == 1


def test_citoid_retries_rate_limits_but_not_not_found(monkeypatch):
    import io
    from urllib.error import HTTPError
    import metadata
    monkeypatch.setattr(metadata, "CITOID_RETRY_WAIT", 0)
    monkeypatch.setattr(metadata.config, "CITOID_MIN_INTERVAL", 0)
    codes = []

    def fake_urlopen(req, timeout=None):
        code = responses.pop(0)
        codes.append(code)
        if code != 200:
            raise HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return io.BytesIO(b'[{"title": "T"}]')

    monkeypatch.setattr(metadata, "urlopen", fake_urlopen)
    responses = [429, 503, 200]
    assert metadata._citoid_get("https://a.org/1") == {"title": "T"}
    assert codes == [429, 503, 200]
    codes.clear()
    responses = [404, 200]
    assert metadata._citoid_get("https://a.org/2") is None
    assert codes == [404]
