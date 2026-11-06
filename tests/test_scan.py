"""Tests for scan.py: reading exports, merging, classifying, and the job."""

from __future__ import annotations

import io
import json
import time
import zipfile

import pytest

from slack_to_zotero import scan


def _msg(uid, name, ts, text, **extra):
    return {"type": "message", "user": uid, "ts": ts, "text": text,
            "user_profile": {"real_name": name}, **extra}


def _zip(files: dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, json.dumps(content))
    return buf.getvalue()


def _wait(job, phase, timeout=5):
    end = time.time() + timeout
    while job.snapshot()["phase"] != phase:
        assert time.time() < end, f"job stuck in {job.snapshot()['phase']}"
        time.sleep(0.01)


# ── read_export ───────────────────────────────────────────────────────────────

def test_read_export_from_zip_groups_channels_and_reads_users(tmp_path):
    data = _zip({
        "users.json": [{"id": "U9", "real_name": "Workflow Only"}],
        "channels.json": [],
        "its-lit/2024-01-01.json": [_msg("U1", "Ana", "1.0", "<https://a.org>")],
        "its-lit/2024-01-02.json": [],
        "general/2024-01-01.json": [],
        "__MACOSX/its-lit/._2024-01-01.json": [],
    })
    export = scan.read_export([("export.zip", data)], str(tmp_path))
    assert sorted(export.channels) == ["general", "its-lit"]
    assert len(export.channels["its-lit"]) == 2
    assert export.user_map == {"U9": "Workflow Only"}


def test_read_export_from_loose_json_files(tmp_path):
    uploads = [("2024-01-01.json", b"[]"), ("notes.txt.json", b"[]")]
    export = scan.read_export(uploads, str(tmp_path))
    assert list(export.channels) == [""]
    assert len(export.channels[""]) == 1


def test_read_export_rejects_files_with_no_day_files(tmp_path):
    with pytest.raises(scan.ExportError):
        scan.read_export([("channels.json", b"[]")], str(tmp_path))


def test_read_export_ignores_archive_paths(tmp_path):
    data = _zip({"../../evil/2024-01-01.json": []})
    export = scan.read_export([("x.zip", data)], str(tmp_path))
    [path] = export.channels["evil"]
    assert path.startswith(str(tmp_path))


# ── collect_papers ────────────────────────────────────────────────────────────

def test_collect_merges_by_url_and_doi_and_unions_comments(tmp_path):
    day = tmp_path / "2024-01-01.json"
    day.write_text(json.dumps([
        _msg("U1", "Ana", "1.0", "<https://doi.org/10.1234/abc>"),
        _msg("U2", "Ben", "2.0", "<https://publisher.org/10.1234/abc>"),
        _msg("U3", "Cy", "3.0", "<https://a.org/p?utm_source=x>"),
        _msg("U4", "Di", "4.0", "<https://a.org/p>"),
    ]))
    papers = scan.collect_papers([str(day)], {})
    assert len(papers) == 2
    by_url = {p["url"]: p for p in papers}
    assert [c["author"] for c in by_url["https://doi.org/10.1234/abc"]["comments"]] == ["Ana", "Ben"]
    assert [c["author"] for c in by_url["https://a.org/p"]["comments"]] == ["Cy", "Di"]


def test_collect_uses_users_json_for_workflow_submitters(tmp_path):
    day = tmp_path / "2024-01-01.json"
    day.write_text(json.dumps([{
        "type": "message", "subtype": "bot_message", "ts": "1.0",
        "text": "*Post article* submission from <@U9> *title* T "
                "*article link* <https://a.org/x>"}]))
    [p] = scan.collect_papers([str(day)], {"U9": "Workflow Only"})
    assert p["comments"][0]["author"] == "Workflow Only"
    assert p["slack_hints"] == {"title": "T"}


# ── classify ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fields, bucket, reason", [
    ({"metadata_source": "crossref_doi", "title": "T"}, "ready", ""),
    ({"metadata_source": "translator", "title": "T"}, "ready", ""),
    ({"metadata_source": "translator"}, "review", "No title found"),
    ({"metadata_source": "llm", "title": "T"}, "ready", ""),
    ({"metadata_source": "llm", "title": "T", "low_context": True}, "review", "Page could not be accessed"),
    ({"metadata_source": "link", "title": "Journal homepage"}, "ready", ""),
    ({"metadata_source": "non_paper"}, "review", "Likely not a source"),
    ({"metadata_source": "needs_manual"}, "review", "Page could not be accessed"),
    ({"metadata_source": "pdf_saved"}, "review", "PDF has no readable text"),
    ({"metadata_source": "none"}, "review", "No metadata found"),
    ({"metadata_source": "none", "scan_error": "boom"}, "review", "Error during search"),
])
def test_classify(fields, bucket, reason):
    assert scan.classify({"url": "https://a.org", **fields}) == (bucket, reason)


def test_tweets_are_ready_and_titled_by_handle():
    from slack_to_zotero.slack_parser import tweet_title
    assert scan.classify({"url": "https://x.com/u/status/1",
                          "metadata_source": "link"}) == ("ready", "")
    assert tweet_title("https://x.com/MatthewStaiger/status/1770873531364257837").startswith(
        "Post by @MatthewStaiger, ")


# ── Job ───────────────────────────────────────────────────────────────────────

def _papers(n):
    return [{"url": f"https://a.org/{i}", "comments": []} for i in range(n)]


def test_job_scans_every_paper_and_sorts_results():
    def enrich(p, key, log):
        i = int(p["url"].rsplit("/", 1)[1])
        if i == 2:
            raise RuntimeError("network down")
        if i % 2:
            return {"metadata_source": "non_paper"}
        return {"metadata_source": "crossref_doi", "title": f"Paper {i}"}

    job = scan.Job(_papers(6), None, enrich=enrich)
    job.start()
    _wait(job, scan.SCANNED)
    ready, review = job.items(scan.READY), job.items(scan.REVIEW)
    assert [p["title"] for p in ready] == ["Paper 0", "Paper 4"]
    assert len(review) == 4
    assert {p["reason"] for p in review} == {"Error during search", "Likely not a source"}
    assert all(p["include"] for p in ready)
    assert not any(p["include"] for p in review)
    assert job.snapshot()["done"] == 6


def test_job_stop_leaves_remaining_items_unscanned():
    gate = []

    def enrich(p, key, log):
        while not gate:
            time.sleep(0.005)
        return {"metadata_source": "crossref_doi", "title": "T"}

    job = scan.Job(_papers(40), None, enrich=enrich)
    job.start()
    job.stop()
    gate.append(1)
    _wait(job, scan.SCANNED)
    not_scanned = [p for p in job.items(scan.REVIEW) if p["reason"] == "Not searched"]
    assert len(not_scanned) >= 40 - scan.WORKERS
    assert job.snapshot()["done"] == 40


class FakeZotero:
    def __init__(self, reject=()):
        self.reject = set(reject)
        self.created = []

    def create_items(self, items):
        resp = {"successful": {}, "failed": {}}
        for i, item in enumerate(items):
            if item.get("title") in self.reject:
                resp["failed"][str(i)] = {"message": "bad item"}
            else:
                key = f"K{len(self.created)}"
                self.created.append(item)
                resp["successful"][str(i)] = {"data": {"key": key}}
        return resp


def test_upload_sends_only_selected_items_and_retries_failures():
    def enrich(p, key, log):
        i = int(p["url"].rsplit("/", 1)[1])
        if i == 3:
            return {"metadata_source": "link"}
        return {"metadata_source": "translator", "title": f"Paper {i}"}

    job = scan.Job(_papers(4), None, enrich=enrich)
    job.start()
    _wait(job, scan.SCANNED)
    job.set_include("https://a.org/1", False)          # leave one Ready item out
    job.set_include("https://a.org/3", True)           # include the link

    zot = FakeZotero(reject={"Paper 2"})
    job.start_upload(lambda: zot)
    _wait(job, scan.DONE)
    titles = [i["title"] for i in zot.created]
    assert sorted(titles) == ["Paper 0", "https://a.org/3"]
    link = next(i for i in zot.created if i["title"] == "https://a.org/3")
    assert link["itemType"] == "webpage"
    assert job.snapshot()["upload_result"]["failed"] == 1

    zot.reject.clear()
    job.start_upload(lambda: zot)                      # retry: only the failure
    _wait(job, scan.DONE)
    assert [i["title"] for i in zot.created][-1] == "Paper 2"
    assert len(zot.created) == 3


def test_web_pages_are_ready_and_upload_as_web_pages():
    p = {"url": "https://docs.google.com/document/d/x", "metadata_source": "translator",
         "title": "Lab Bibliography", "item_type": "webpage"}
    assert scan.classify(p) == ("ready", "")
    link = {"url": "https://journal.org", "metadata_source": "link"}
    scan._prepare_for_upload(link)
    assert (link["title"], link["item_type"]) == ("https://journal.org", "webpage")


def test_landing_page_merges_with_article_but_editions_stay_apart():
    papers = [
        {"url": "https://t.com/collections/x", "comments": []},
        {"url": "https://t.com/doi/10.1234/x", "comments": []},
        {"url": "https://jchs.org/2023", "comments": []},
        {"url": "https://jchs.org/2024", "comments": []},
    ]
    meta = {
        "https://t.com/collections/x": {"title": "Where Is the Housing Shortage?",
                                        "item_type": "webpage"},
        "https://t.com/doi/10.1234/x": {"title": "Where Is the Housing Shortage?",
                                        "year": "2025", "doi": "10.1234/x"},
        "https://jchs.org/2023": {"title": "The State of the Nation's Housing", "year": "2023"},
        "https://jchs.org/2024": {"title": "The State of the Nation's Housing", "year": "2024"},
    }
    job = scan.Job(papers, None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", **meta[p["url"]]})
    job.start()
    _wait(job, scan.SCANNED)
    urls = sorted(p["url"] for p in job.papers)
    assert urls == ["https://jchs.org/2023", "https://jchs.org/2024",
                    "https://t.com/doi/10.1234/x"]


# ── URL normalization and tweet detection ────────────────────────────────────

from slack_to_zotero.records import normalize_url
from slack_to_zotero.slack_parser import is_tweet_url


def test_normalize_url_keeps_the_query_so_ssrn_papers_stay_distinct():
    ssrn = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id={}"
    a, b, c = (normalize_url(ssrn.format(i)) for i in (4941708, 4554831, 4480068))
    assert len({a, b, c}) == 3


def test_normalize_url_lowercases_scheme_and_host_only():
    assert normalize_url("HTTPS://Example.COM/Path/To/") == "https://example.com/Path/To"


@pytest.mark.parametrize("url, tweet", [
    ("https://twitter.com/user/status/1785127046756343980/photo/1", True),
    ("https://x.com/user/status/1773475907858272686", True),
    ("https://mobile.twitter.com/user/status/1", True),
    ("https://x.com/user", False),
    ("https://example.com/status/1", False),
])
def test_is_tweet_url(url, tweet):
    assert is_tweet_url(url) is tweet


def test_upload_reports_each_item_and_sends_notes_in_one_request(monkeypatch, tmp_path):
    from slack_to_zotero import zotero_client
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    attached = []
    monkeypatch.setattr(zotero_client, "attach_file_with_retry",
                        lambda zot, key, path, name: attached.append(key))

    class Zot(FakeZotero):
        calls = 0

        def create_items(self, items):
            Zot.calls += 1
            return super().create_items(items)

    comment = [{"ts": "1", "author": "A", "text": "x", "is_reply": False}]
    papers = [{"url": f"https://a.org/{i}", "title": f"P{i}", "comments": comment}
              for i in range(3)]
    papers[1]["local_pdf_path"] = str(pdf)
    progress, lines = [], []
    result = zotero_client.upload_papers(papers, Zot(), progress_cb=lambda d, t: progress.append((d, t)),
                                         log_cb=lines.append)
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert Zot.calls == 2                         # the items, then all three notes
    assert result["notes_added"] == 3 and len(attached) == 1
    assert [l.split()[0] for l in lines] == ["Uploaded"] * 3


def test_article_without_authors_year_or_doi_becomes_a_web_page():
    p = {"url": "https://huduser.gov/cityscape", "item_type": "journalArticle", "title": "Cityscape"}
    scan._tidy(p)
    assert p["item_type"] == "webpage"
    q = {"url": "https://a.org", "item_type": "journalArticle", "title": "T", "year": "2024"}
    scan._tidy(q)
    assert q["item_type"] == "journalArticle"


def _scan_with(meta):
    papers = [{"url": u, "comments": []} for u in meta]
    job = scan.Job(papers, None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", **meta[p["url"]]})
    job.start()
    _wait(job, scan.SCANNED)
    return sorted(p["url"] for p in job.papers)


def test_web_page_merges_into_the_paper_with_its_title_even_with_another_year():
    assert _scan_with({
        "https://t.com/collection": {"title": "Where Is the Housing Shortage?", "year": "2024",
                                     "item_type": "webpage"},
        "https://t.com/article": {"title": "Where Is the Housing Shortage?", "year": "2025",
                                  "authors": ["Doe, J"], "doi": "10.1234/x"},
    }) == ["https://t.com/article"]


def test_short_titles_merge_only_with_the_same_year():
    assert len(_scan_with({
        "https://nber.org/w1": {"title": "Rent Guarantee Insurance", "year": "2024", "authors": ["A"]},
        "https://nber.org/w1.pdf": {"title": "Rent Guarantee Insurance", "year": "2024", "authors": ["A"]},
        "https://nber.org/old": {"title": "Rent Guarantee Insurance", "year": "2019", "authors": ["A"]},
    })) == 2


def test_every_title_is_cleaned_after_lookup():
    p = {"url": "https://a.org", "title": "Cracking Down, Pricing Up <br>", "authors": ["A"]}
    scan._tidy(p)
    assert p["title"] == "Cracking Down, Pricing Up"


def test_decide_sets_and_clears_review_decisions():
    job = scan.Job([{"url": "https://a.org/x", "comments": []}], None,
                   title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "needs_manual"})
    job.start()
    _wait(job, scan.SCANNED)
    [p] = job.papers
    job.decide(p["url"], "edited", {"title": "Fixed"})
    assert (p["decision"], p["title"], p["include"]) == ("edited", "Fixed", True)
    job.decide(p["url"], "removed")
    assert not p["include"]
    with pytest.raises(AssertionError):
        job.decide(p["url"], "not_source")
    job.decide(p["url"], None)
    assert "decision" not in p and not p["include"]


def test_web_page_without_a_site_name_gets_its_domain():
    p = {"url": "https://www.sciencedirect.com/topics/economics/iv", "title": "IV",
         "item_type": "webpage", "metadata_source": "link"}
    scan._prepare_for_upload(p)
    assert p["journal"] == "sciencedirect.com"
    q = {"url": "https://a.org/x", "title": "T", "item_type": "journalArticle"}
    scan._prepare_for_upload(q)
    assert "journal" not in q


def test_ready_items_can_be_edited_moved_to_review_or_removed():
    job = scan.Job(_papers(3), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.start()
    _wait(job, scan.SCANNED)
    a, b, c = job.papers
    job.edit(a["url"], {"title": "Fixed"})
    assert (a["title"], a["bucket"], a["include"]) == ("Fixed", scan.READY, True)
    job.move_to_review(b["url"])
    assert (b["bucket"], b["reason"], b["include"]) == (scan.REVIEW, "Moved from Ready", False)
    job.move_to_review(c["url"])
    job.decide(c["url"], "removed")
    assert (c["bucket"], c["decision"], c["include"]) == (scan.REVIEW, "removed", False)
    assert [p["url"] for p in job.selected()] == [a["url"]]


def test_pause_holds_the_search_and_resume_finishes_it():
    job = scan.Job(_papers(3), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.pause()
    job.start()
    time.sleep(0.1)
    snap = job.snapshot()
    assert snap["paused"] and snap["done"] == 0 and snap["phase"] == scan.SCANNING
    job.resume()
    _wait(job, scan.SCANNED)
    assert all(p["bucket"] == scan.READY for p in job.papers)


def test_stop_discards_the_search():
    job = scan.Job(_papers(3), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.pause()
    scan._current = job
    job.start()
    scan.discard_job()
    assert scan.current_job() is None
    _wait(job, scan.SCANNED)                      # its threads finish and exit
    assert job.snapshot()["done"] == 3


def test_saved_results_load_as_a_finished_search_without_lookups():
    job = scan.Job(_papers(3), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": f"Paper {p['url'][-1]}",
                                         "authors": ["Doe, Jane"], "year": "2024",
                                         "item_type": "journalArticle", "doi": f"10.1/{p['url'][-1]}"})
    job.start()
    _wait(job, scan.SCANNED)
    job.papers[0]["zotero_key"] = "ABC"              # upload outcomes aren't saved
    data = scan.saved_results(job.papers, "full.json")
    papers, label = scan.read_saved_results(data)
    assert label == "full.json" and "zotero_key" not in papers[0]
    assert scan.read_saved_results(b'[{"type": "message"}]') is None   # a Slack day-file
    scan._current = None
    assert scan.load_saved_job(papers, label)
    loaded = scan.current_job()
    _wait(loaded, scan.SCANNED)
    assert [p["bucket"] for p in loaded.papers] == [scan.READY] * 3
    scan._current = None


def test_html_entities_are_cleaned_from_journals_and_authors():
    p = {"title": "Rent &amp; Eviction", "journal": "City &amp; Community",
         "authors": ["O&#39;Brien, Ann"], "item_type": "journalArticle", "year": "2024"}
    scan._tidy(p)
    assert (p["title"], p["journal"], p["authors"]) == \
        ("Rent & Eviction", "City & Community", ["O'Brien, Ann"])


def test_items_edited_into_duplicates_upload_once():
    def enrich(p, key, log):
        i = int(p["url"].rsplit("/", 1)[1])
        if i < 2:
            return {"metadata_source": "translator", "title": f"Paper {i}",
                    "authors": ["Doe, Jane"], "year": "2024", "item_type": "journalArticle"}
        return {"metadata_source": "none"}                   # to Review

    job = scan.Job(_papers(4), None, title_search=lambda *a: None, enrich=enrich)
    job.start()
    _wait(job, scan.SCANNED)
    same = {"title": "Paper 0", "authors": ["Doe, Jane"], "year": "2024",
            "item_type": "journalArticle"}
    for url in ("https://a.org/2", "https://a.org/3"):        # two Review items edited
        job.edit(url, same)                                 # to match a Ready one
        job.decide(url, "edited")
    zot = FakeZotero()
    job.start_upload(lambda: zot)
    _wait(job, scan.DONE)
    assert sorted(i["title"] for i in zot.created) == ["Paper 0", "Paper 1"]
    dup = next(p for p in job.papers if p["url"] == "https://a.org/2")
    assert dup["bucket"] == scan.EXISTING and not dup["include"]


def test_damaged_saved_results_files_are_not_loaded():
    assert scan.read_saved_results(b'{"slack_to_zotero_saved_results": 1}') is None
    assert scan.read_saved_results(b'{"slack_to_zotero_saved_results": 1, "papers": "x"}') is None
    papers, _ = scan.read_saved_results(
        b'{"slack_to_zotero_saved_results": 1, "papers": [{"title": "no link"}, {"url": "https://a.org"}]}')
    assert papers == [{"url": "https://a.org"}]


def test_author_names_typed_without_a_space_after_the_comma():
    from slack_to_zotero.zotero_client import _build_creators
    assert _build_creators(["Doe,Jane", " Roe , Rich ", "National Center for State Courts", ","]) == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"},
        {"creatorType": "author", "firstName": "Rich", "lastName": "Roe"},
        {"creatorType": "author", "firstName": "", "lastName": "National Center for State Courts"},
    ]


def test_items_cannot_change_while_uploading():
    import threading as th
    release = th.Event()

    def slow_upload(papers, zot, progress_cb, log_cb):
        release.wait(5)
        return {"uploaded": len(papers), "failed": 0, "errors": []}

    job = scan.Job(_papers(2), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.start()
    _wait(job, scan.SCANNED)
    job.start_upload(lambda: None, upload=slow_upload)
    _wait(job, scan.UPLOADING)
    a = job.papers[0]
    job.edit(a["url"], {"title": "Changed mid-upload"})
    job.move_to_review(a["url"])
    job.set_include(a["url"], False)
    assert (a["title"], a["bucket"], a["include"]) == ("https://a.org/0", scan.READY, True)
    release.set()
    _wait(job, scan.DONE)


def test_an_upload_that_fails_before_sending_still_finishes(monkeypatch):
    job = scan.Job(_papers(2), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.start()
    _wait(job, scan.SCANNED)

    def broken(papers):
        raise RuntimeError("index unreadable")
    monkeypatch.setattr(job, "_recheck", broken)
    job.start_upload(lambda: FakeZotero())
    _wait(job, scan.DONE)
    result = job.snapshot()["upload_result"]
    assert result["failed"] == 2 and "index unreadable" in result["errors"][0][1]


def test_a_search_that_fails_while_finishing_still_ends(monkeypatch):
    job = scan.Job(_papers(2), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    monkeypatch.setattr(job, "_merge_duplicates", lambda: 1 / 0)
    job.start()
    _wait(job, scan.SCANNED)
    assert "ZeroDivisionError" in job.snapshot()["fatal_error"]


def test_upload_still_checks_known_zotero_items_when_zotero_cant_be_refreshed():
    from slack_to_zotero.zotero_index import LibraryIndex
    ix = LibraryIndex()
    ix.add({"key": "Z1", "title": "Known Paper", "date": "2024", "itemType": "journalArticle",
            "creators": [{"lastName": "Doe"}]})

    def refresh(index):
        raise OSError("network down")

    job = scan.Job(_papers(1), None, title_search=lambda *a: None, load_index=lambda: ix,
                   refresh_index=refresh, enrich=lambda p, *a: {"metadata_source": "none"})
    job.start()
    _wait(job, scan.SCANNED)
    url = job.papers[0]["url"]
    job.edit(url, {"title": "Known Paper", "year": "2024", "authors": ["Doe, Jane"],
                   "item_type": "journalArticle"})
    job.decide(url, "edited")
    zot = FakeZotero()
    job.start_upload(lambda: zot)
    _wait(job, scan.DONE)
    assert zot.created == [] and job.papers[0]["bucket"] == scan.EXISTING


def test_lookup_results_with_wrong_types_are_cleaned():
    p = {"metadata_source": "llm", "title": 42, "authors": "Doe, Jane; Roe, Rich",
         "year": 2024, "journal": ["Demography"], "volume": 12, "issue": None,
         "pages": {"from": 1}, "summary": 3.5, "item_type": "journalArticle"}
    scan._tidy(p)
    assert p["title"] == "42" and p["authors"] == ["Doe, Jane", "Roe, Rich"]
    assert (p["year"], p["journal"], p["volume"], p["issue"], p["pages"], p["summary"]) == \
        ("2024", "Demography", "12", None, None, "3.5")
    q = {"authors": [None, "Doe, Jane", 5, ["x"]], "item_type": "webpage"}
    scan._tidy(q)
    assert q["authors"] == ["Doe, Jane", "5"]


def test_isbn_is_sent_only_for_types_that_have_it():
    from slack_to_zotero.zotero_client import paper_to_zotero_item
    article = paper_to_zotero_item({"url": "u", "title": "T", "item_type": "journalArticle",
                                    "isbn": "9780000000000"})
    book = paper_to_zotero_item({"url": "u", "title": "T", "item_type": "book",
                                 "isbn": "9780000000000"})
    assert "ISBN" not in article and book["ISBN"] == "9780000000000"


def test_zotero_failures_are_explained_plainly():
    from urllib.error import URLError

    from pyzotero import zotero_errors as ze

    from slack_to_zotero.zotero_client import explain_error
    assert "write access" in explain_error(ze.UserNotAuthorisedError("403"))
    assert "Check the IDs" in explain_error(ze.ResourceNotFoundError("404"))
    assert "Couldn't reach Zotero" in explain_error(URLError("down"))
    assert explain_error(ValueError("odd")) == "ValueError: odd"


def test_clearing_results_deletes_their_downloaded_files(monkeypatch, tmp_path):
    from slack_to_zotero import config
    monkeypatch.setattr(config, "PDFS_DIR", tmp_path)
    kept = tmp_path.parent / "not-ours.pdf"
    kept.write_bytes(b"x")
    pdf = tmp_path / "abc.pdf"
    pdf.write_bytes(b"%PDF")

    def enrich(p, *a):
        return {"metadata_source": "translator", "title": p["url"],
                "local_pdf_path": str(pdf if p["url"].endswith("0") else kept)}
    scan._current = None
    assert scan.start_job(_papers(2), None, enrich=enrich, title_search=lambda *a: None)
    _wait(scan.current_job(), scan.SCANNED)
    scan.clear_job()
    assert not pdf.exists() and kept.exists()       # only files in the app's folder
    kept.unlink()


def test_search_waits_out_a_lost_connection_instead_of_misjudging_items(monkeypatch):
    online = {"up": False}
    calls = []

    def enrich(p, *a):
        calls.append(p["url"])
        if not online["up"]:
            return {"metadata_source": "none"}            # lookups fail offline
        return {"metadata_source": "translator", "title": p["url"]}

    monkeypatch.setattr(scan, "CONNECTION_RETRY_SECONDS", 0.05)
    job = scan.Job(_papers(2), None, title_search=lambda *a: None, enrich=enrich,
                   is_online=lambda: online["up"])
    job.start()
    end = time.time() + 5
    while job.snapshot()["step"] != "Waiting for connection":
        assert time.time() < end
        time.sleep(0.01)
    assert job.snapshot()["phase"] == scan.SCANNING
    online["up"] = True
    _wait(job, scan.SCANNED)
    assert all(p["bucket"] == scan.READY for p in job.papers)   # judged once back online


def test_stop_ends_a_search_waiting_for_connection(monkeypatch):
    monkeypatch.setattr(scan, "CONNECTION_RETRY_SECONDS", 0.05)
    job = scan.Job(_papers(2), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "none"}, is_online=lambda: False)
    job.start()
    time.sleep(0.2)
    job.stop()
    _wait(job, scan.SCANNED)


def test_search_time_leaves_out_time_paused():
    job = scan.Job(_papers(1), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": "T"})
    job.pause()
    job.start()
    time.sleep(0.3)
    job.resume()
    job.resume()                                   # a second click changes nothing
    _wait(job, scan.SCANNED)
    assert job.search_seconds() < 0.2


def test_attachment_names_have_no_slashes_from_authors_or_years():
    from slack_to_zotero.zotero_client import _file_name
    name = _file_name({"authors": ["AC/DC Collective"], "year": "2023/24",
                       "title": "Rent: A/B Tests?"}, "/tmp/x.pdf")
    assert "/" not in name and "\\" not in name and name.endswith(".pdf")
    assert _file_name({"title": "///"}, "/tmp/x.pdf") == "Full Text.pdf"


class RateLimitedZotero(FakeZotero):
    """Answers the first `limited` item writes the way pyzotero does on a 429:
    an error from reading the empty reply as JSON."""
    def __init__(self, limited):
        super().__init__()
        self.limited = limited
        self.backoff_until = 0.0

    def create_items(self, items):
        if self.limited:
            self.limited -= 1
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return super().create_items(items)


def _ready_job(n):
    job = scan.Job(_papers(n), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.start()
    _wait(job, scan.SCANNED)
    return job


def test_upload_rides_out_zotero_rate_limiting(monkeypatch):
    from slack_to_zotero import zotero_client
    monkeypatch.setattr(zotero_client.time, "sleep", lambda s: None)
    job = _ready_job(3)
    zot = RateLimitedZotero(limited=2)
    job.start_upload(lambda: zot)
    _wait(job, scan.DONE)
    assert job.snapshot()["upload_result"]["uploaded"] == 3 and len(zot.created) == 3


def test_upload_reports_persistent_rate_limiting_plainly(monkeypatch):
    from slack_to_zotero import zotero_client
    monkeypatch.setattr(zotero_client.time, "sleep", lambda s: None)
    job = _ready_job(2)
    job.start_upload(lambda: RateLimitedZotero(limited=99))
    _wait(job, scan.DONE)
    p = job.papers[0]
    assert "limiting requests" in p["upload_error"] and p["include"]   # ready to Retry


def test_many_people_changing_results_at_once_stays_consistent():
    import random
    import threading as th

    def enrich(p, *a):
        i = int(p["url"].rsplit("/", 1)[1])
        return ({"metadata_source": "translator", "title": f"Paper {i}"} if i % 2
                else {"metadata_source": "none"})
    job = scan.Job(_papers(40), None, title_search=lambda *a: None, enrich=enrich)
    job.start()
    _wait(job, scan.SCANNED)
    urls = [p["url"] for p in job.papers]
    errors = []

    def person(seed):
        rnd = random.Random(seed)
        try:
            for _ in range(300):
                url = rnd.choice(urls)
                action = rnd.randrange(5)
                if action == 0:
                    job.decide(url, rnd.choice([None, "correct", "edited", "removed"]))
                elif action == 1:
                    job.edit(url, {"title": rnd.choice(["Same Title", f"T{rnd.random()}"])})
                elif action == 2:
                    job.move_to_review(url)
                elif action == 3:
                    job.set_include(url, rnd.random() < 0.5)
                else:
                    job.snapshot()
                    job.selected()
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    zot = FakeZotero()
    people = [th.Thread(target=person, args=(i,)) for i in range(8)]
    for t in people:
        t.start()
    job.start_upload(lambda: zot)
    for t in people:
        t.join()
    _wait(job, scan.DONE, timeout=10)
    assert not errors
    created = [i["url"] for i in zot.created]
    assert len(created) == len(set(created))               # nothing uploaded twice
    for p in job.papers:
        assert p["bucket"] in (scan.READY, scan.REVIEW, scan.EXISTING)
        if p.get("decision") == "removed":
            assert not p["include"]
        if p.get("zotero_key"):
            assert p["url"] in created


def test_searching_again_after_an_upload_cut_off_by_a_restart():
    """The app restarted partway through an upload: some items reached Zotero,
    the rest didn't. Searching the same export again must treat the first as
    already in Zotero and offer only the rest, so nothing is duplicated."""
    from slack_to_zotero.zotero_index import LibraryIndex

    def enrich(p, *a):
        i = p["url"].rsplit("/", 1)[1]
        return {"metadata_source": "translator", "title": f"Paper number {i} on rent",
                "authors": ["Doe, Jane"], "year": "2024", "item_type": "journalArticle"}

    first = scan.Job(_papers(120), None, title_search=lambda *a: None, enrich=enrich)
    first.start()
    _wait(first, scan.SCANNED)
    zot = FakeZotero()

    class Restart(Exception):
        pass
    real = zot.create_items
    calls = {"n": 0}

    def cut_off(items):
        calls["n"] += 1
        if calls["n"] > 2:                    # items batch, notes batch, then the restart
            raise Restart()
        return real(items)
    zot.create_items = cut_off
    first.start_upload(lambda: zot)
    _wait(first, scan.DONE)
    reached = len(zot.created)
    assert 0 < reached < 120

    index = LibraryIndex()
    for i, item in enumerate(zot.created):
        index.add({**item, "key": f"K{i}"})
    second = scan.Job(_papers(120), None, title_search=lambda *a: None, enrich=enrich,
                      load_index=lambda: index)
    second.start()
    _wait(second, scan.SCANNED)
    assert len(second.items(scan.EXISTING)) == reached
    assert len(second.items(scan.READY)) == 120 - reached


def test_two_people_pressing_upload_at_once_upload_each_item_once():
    import threading as th
    job = _ready_job(30)
    zot = FakeZotero()
    go = th.Barrier(6)

    def press():
        go.wait()
        job.start_upload(lambda: zot)
    presses = [th.Thread(target=press) for _ in range(6)]
    for t in presses:
        t.start()
    for t in presses:
        t.join()
    _wait(job, scan.DONE)
    titles = [i["title"] for i in zot.created]
    assert len(titles) == 30 == len(set(titles))


def test_two_people_starting_a_search_at_once_start_one():
    import threading as th
    scan._current = None
    go = th.Barrier(6)
    started = []

    def press():
        go.wait()
        started.append(scan.start_job(_papers(5), None, title_search=lambda *a: None,
                                      enrich=lambda p, *a: {"metadata_source": "none"}))
    presses = [th.Thread(target=press) for _ in range(6)]
    for t in presses:
        t.start()
    for t in presses:
        t.join()
    assert started.count(True) == 1
    _wait(scan.current_job(), scan.SCANNED)
    scan._current = None


def test_a_full_zotero_library_gets_a_plain_message(monkeypatch):
    import io
    from urllib.error import HTTPError

    from pyzotero import zotero_errors as ze

    from slack_to_zotero import zotero_client
    assert zotero_client.explain_error(ze.RequestEntityTooLargeError("413")) == \
        "The Zotero library is out of storage."

    def full(*a, **k):
        raise HTTPError("u", 413, "Request Entity Too Large", {}, io.BytesIO(b"quota"))
    monkeypatch.setattr(zotero_client, "urlopen", full)
    with pytest.raises(RuntimeError, match="out of storage"):
        zotero_client._http("POST", "https://api.zotero.org/x", b"", {})
