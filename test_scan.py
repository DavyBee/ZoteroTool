"""Tests for scan.py: reading exports, merging, classifying, and the job."""

from __future__ import annotations

import io
import json
import time
import zipfile

import pytest

import scan


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
    from slack_parser import tweet_title
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

from records import normalize_url
from slack_parser import is_tweet_url


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
    import zotero_client
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


def test_unchecked_ready_items_wait_in_review_after_an_upload():
    job = scan.Job(_papers(2), None, title_search=lambda *a: None,
                   enrich=lambda p, *a: {"metadata_source": "translator", "title": p["url"]})
    job.start()
    _wait(job, scan.SCANNED)
    job.set_include("https://a.org/1", False)
    job.start_upload(lambda: FakeZotero())
    _wait(job, scan.DONE)
    left = next(p for p in job.papers if p["url"] == "https://a.org/1")
    assert (left["bucket"], left["reason"]) == (scan.REVIEW, "Left out of upload")


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
