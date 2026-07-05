"""
test_core.py — Characterization tests for the pure-logic cores.

This is the project's safety net: with no ongoing maintainer, these pin the
behavior that's easy to break silently — URL/DOI normalization, duplicate
detection, the metadata-source taxonomy, and the enrich loop's bookkeeping.
They use an in-memory / temp-file database and never touch the network or
Turso, so they run fast and offline:

    python3 -m pytest test_core.py

Add a case here whenever you fix a bug, so it can't come back.
"""

from __future__ import annotations

import os

import pytest

# Tests must never hit a real Turso instance — force the local-sqlite path.
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)

import app_helpers
import database
import metadata
import zotero_client
from database import Database, normalize_url, _is_real_doi


SSRN = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id={}"


@pytest.fixture
def db(tmp_path):
    """A fresh, file-backed Database (so save()/load() work like production)."""
    return Database.load(str(tmp_path / "t.db"), str(tmp_path / "p.json"))


def add(db, url, **fields):
    """Add a paper and apply enrichment fields; return its canonical key."""
    key, _ = db.add_paper(url)
    if fields:
        db.update_paper(key, **fields)
    return key


# ── normalize_url ─────────────────────────────────────────────────────────────

def test_normalize_url_preserves_query_so_distinct_ssrn_ids_stay_distinct():
    # Regression: three SSRN articles differ ONLY in the ?abstract_id= query.
    # If normalize_url dropped the query they'd collapse into one record.
    a, b, c = (normalize_url(SSRN.format(i)) for i in (4941708, 4554831, 4480068))
    assert a != b != c and a != c


def test_normalize_url_lowercases_scheme_host_but_keeps_path_case():
    assert normalize_url("HTTPS://Example.COM/Path/To") == "https://example.com/Path/To"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://example.com/x/") == "https://example.com/x"


# ── DOI validation / cleaning ───────────────────────────────────────────────────

@pytest.mark.parametrize("doi", ["10.2139/", "10.1093/", "10.2139", "", "not-a-doi"])
def test_bare_or_malformed_dois_are_rejected(doi):
    assert _is_real_doi(doi) is False
    assert metadata._clean_doi(doi) is None


@pytest.mark.parametrize("doi", ["10.2139/ssrn.4445621", "10.1093/sf/soab123"])
def test_real_dois_accepted(doi):
    assert _is_real_doi(doi) is True
    assert metadata._clean_doi(doi) == doi


def test_clean_doi_strips_trailing_punctuation_and_handles_none():
    assert metadata._clean_doi("10.1093/sf/soab123.") == "10.1093/sf/soab123"
    assert metadata._clean_doi(None) is None


# ── find_duplicate_groups ────────────────────────────────────────────────────────

def test_distinct_papers_with_bare_prefix_doi_do_not_group(db):
    # THE bug: SSRN/OUP can hand back a bare registrant prefix ("10.2139/") that
    # identifies the publisher, not the paper. Distinct works must NOT be merged.
    add(db, SSRN.format(4941708), doi="10.2139/", title="Watching the Watchdogs",
        metadata_source="translator")
    add(db, SSRN.format(4554831), doi="10.2139/", title="Something Entirely Different",
        metadata_source="translator")
    assert db.find_duplicate_groups() == []


def test_same_real_doi_groups_as_duplicate(db):
    add(db, "https://a.example/x", doi="10.1093/sf/soab123", title="Paper A",
        metadata_source="translator")
    add(db, "https://b.example/y", doi="10.1093/sf/soab123", title="Paper A (preprint)",
        metadata_source="crossref_doi")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_same_title_without_doi_groups(db):
    add(db, "https://a.example/x", title="Eviction and Children",
        metadata_source="llm")
    add(db, "https://b.example/y", title="eviction and children!",  # punct/case differ
        metadata_source="llm")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_tweets_are_never_duplicates(db):
    add(db, "https://x.com/user/status/111", title="t", metadata_source="link")
    add(db, "https://x.com/user/status/222", title="t", metadata_source="link")
    assert db.find_duplicate_groups() == []


def test_dismissed_duplicate_group_is_not_resurfaced(db):
    k1 = add(db, "https://a.example/x", title="Eviction and Children",
              metadata_source="llm")
    k2 = add(db, "https://b.example/y", title="eviction and children!",
              metadata_source="llm")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1
    db.dismiss_duplicate_group([k1, k2])
    assert db.find_duplicate_groups() == []
    # Both papers are untouched — dismissal doesn't delete anything.
    assert db.get_paper("https://a.example/x") is not None
    assert db.get_paper("https://b.example/y") is not None


def test_dismissal_does_not_suppress_a_group_that_gains_a_new_member(db):
    k1 = add(db, "https://a.example/x", title="Eviction and Children",
              metadata_source="llm")
    k2 = add(db, "https://b.example/y", title="eviction and children!",
              metadata_source="llm")
    db.dismiss_duplicate_group([k1, k2])
    assert db.find_duplicate_groups() == []
    # A third paper with the same title joins — new signature, not covered by
    # the old dismissal, so the (now 3-paper) group should resurface.
    add(db, "https://c.example/z", title="Eviction and Children!!",
        metadata_source="llm")
    groups = db.find_duplicate_groups()
    assert len(groups) == 1 and len(groups[0]) == 3


# ── Metadata-source taxonomy (single source of truth in database.py) ────────────

def test_taxonomy_constants_are_consistent():
    assert set(database.READY_SOURCES) <= set(database.CITABLE_SOURCES)
    assert "llm" in database.CITABLE_SOURCES        # citable…
    assert "llm" not in database.READY_SOURCES      # …but not auto-approved


def test_uploadable_respects_taxonomy(db):
    add(db, "https://a.example/x", title="T", metadata_source="translator")
    add(db, "https://b.example/llm-unreviewed", title="T", metadata_source="llm")
    add(db, "https://c.example/llm-reviewed", title="T", metadata_source="llm",
        reviewed=True)
    urls = {p["url"] for p in zotero_client.uploadable_papers(db)}
    assert "https://a.example/x" in urls             # translator → ready
    assert "https://c.example/llm-reviewed" in urls  # reviewed llm → ready
    assert "https://b.example/llm-unreviewed" not in urls  # unreviewed llm → held back


def test_uploadable_does_not_itself_check_title(db):
    # Documents real behavior: uploadable_papers gates on source + reviewed only,
    # NOT title (its docstring's "have title" is guaranteed upstream by the
    # translator junk filter, not re-checked here). Pinned so a future refactor
    # that "tidies" the docstring doesn't accidentally change the gate.
    add(db, "https://d.example/no-title", metadata_source="translator")
    urls = {p["url"] for p in zotero_client.uploadable_papers(db)}
    assert "https://d.example/no-title" in urls


# ── Tweet expansion (network stubbed) ────────────────────────────────────────────

def _stub_tweet(monkeypatch, syndication, page_gone):
    import url_expander
    monkeypatch.setattr(url_expander, "_fetch_syndication", lambda tid: syndication)
    monkeypatch.setattr(url_expander, "_fetch_page_gone", lambda url: page_gone)
    return url_expander.expand_tweet("https://x.com/u/status/1")


def test_deleted_tweet_tombstone_is_gone(monkeypatch):
    # X returns HTTP 200 + a TweetTombstone body for deleted tweets (only
    # never-existed ids 404), so the tombstone must map to "gone", not "empty" —
    # otherwise dead tweets are kept as Links instead of going to Junk.
    # Either marker alone must be enough (robust to partial format changes).
    for body in ({"__typename": "TweetTombstone"}, {"tombstone": {"text": "x"}}):
        assert _stub_tweet(monkeypatch, (body, False), True) == ("gone", None)
        # …even when the page itself can't be reached for a second opinion.
        assert _stub_tweet(monkeypatch, (body, False), None) == ("gone", None)


def test_page_status_is_the_liveness_authority(monkeypatch):
    # If X someday changes the syndication format entirely, dead-or-alive is
    # still decided by the tweet page's own HTTP status (format-independent).
    unrecognized = ({"some_future_field": 1}, False)
    assert _stub_tweet(monkeypatch, unrecognized, True) == ("gone", None)
    assert _stub_tweet(monkeypatch, unrecognized, False) == ("empty", None)
    # Can't reach the page either → degrade to Links (visible), never Junk.
    assert _stub_tweet(monkeypatch, unrecognized, None) == ("empty", None)


def test_live_tweet_never_junked_when_syndication_dies(monkeypatch):
    # Even if the syndication endpoint is retired and 404s every request, a
    # tweet whose page still loads must stay visible as a Link, not be junked.
    assert _stub_tweet(monkeypatch, (None, True), False) == ("empty", None)
    # And a transient syndication failure stays a transient error.
    assert _stub_tweet(monkeypatch, (None, False), None) == ("error", None)


# ── Enrich loop orchestration (network stubbed) ─────────────────────────────────

def test_run_enrich_drives_every_pending_paper_once(db, monkeypatch):
    for i in range(3):
        db.add_paper(f"https://a.example/{i}")

    calls = []

    def fake_enrich_paper(paper, anthropic_key, audit_log_path):
        calls.append(paper["url"])
        return {"metadata_source": "translator", "title": "T", "attempts": 1}

    monkeypatch.setattr(app_helpers, "enrich_paper", fake_enrich_paper)
    steps = list(app_helpers.run_enrich(db, anthropic_key=None, audit_path="x"))

    assert len(calls) == 3                    # each pending paper enriched exactly once
    assert len(set(calls)) == 3               # no double-processing (the rerun-race bug)
    assert all(s["counts"]["ok"] <= 3 for s in steps)
    assert all(p["metadata_source"] == "translator" for p in db.all_papers())


# ── Recheck sources ("Check sources again" on Access issues / Likely junk) ─────

def test_recheck_moves_recovered_paper_and_keeps_failed_check_in_place(db, monkeypatch):
    add(db, "https://a.example/recovered", metadata_source="needs_manual")
    add(db, "https://b.example/still-blocked", metadata_source="needs_manual")

    def fake_enrich(paper, anthropic_key, audit_log_path):
        if "recovered" in paper["url"]:
            return {"metadata_source": "translator", "title": "T", "attempts": 1}
        # An unclassifiable run (LLM API failure / no key) comes back "none".
        return {"metadata_source": "none",
                "last_error": "llm: API call failed", "attempts": 1}

    monkeypatch.setattr(app_helpers, "enrich_paper", fake_enrich)
    counts = app_helpers.recheck_sources(db, app_helpers.triage_papers(db),
                                         None, "x")
    assert counts["ok"] == 1 and counts["fail"] == 1
    assert db.get_paper("https://a.example/recovered")["metadata_source"] == "translator"
    # A failed check must NOT dump the paper back to Pending — it stays put.
    assert db.get_paper("https://b.example/still-blocked")["metadata_source"] == "needs_manual"


def test_recheck_reexpands_a_junked_tweet_that_is_public_again(db, monkeypatch):
    # THE motivating case: a tweet that was private (→ Likely junk) is public
    # now. Rechecking must re-expand it — not run the cascade, whose tweet
    # short-circuit would blindly mark it a Link without ever checking.
    tweet = "https://x.com/u/status/1"
    add(db, tweet, metadata_source="non_paper", tweet_expanded=True)
    monkeypatch.setattr(app_helpers, "expand_tweet",
                        lambda url: ("ok", "https://paper.example/found"))
    counts = app_helpers.recheck_sources(db, app_helpers.junk_papers(db),
                                         None, "x")
    assert counts["link"] == 1 and counts["new"] == 1
    assert db.get_paper(tweet)["metadata_source"] == "link"
    target = db.get_paper("https://paper.example/found")
    assert target is not None
    assert target["metadata_source"] == "none"     # queued for the next Enrich


def test_recheck_leaves_a_still_private_tweet_in_junk(db, monkeypatch):
    tweet = "https://x.com/u/status/2"
    add(db, tweet, metadata_source="non_paper", tweet_expanded=True)
    monkeypatch.setattr(app_helpers, "expand_tweet", lambda url: ("gone", None))
    counts = app_helpers.recheck_sources(db, app_helpers.junk_papers(db),
                                         None, "x")
    assert counts["non"] == 1
    assert db.get_paper(tweet)["metadata_source"] == "non_paper"


# ── Durable PDF blobs (pdf_blobs table + ensure_local_pdf self-heal) ──────────
# On the hosted deploy pdfs/ is wiped on every restart; PDFs ≤ PDF_BLOB_MAX_BYTES
# are mirrored into the DB at save time and rehydrated on demand. Larger PDFs
# are flagged pdf_too_large and re-downloaded just-in-time instead.

import pdf_storage
import config as _config

_PDF_BYTES = b"%PDF-1.4 fake body"


@pytest.fixture
def blob_env(tmp_path, monkeypatch):
    """Point the PDF store and the default DB at tmp_path so blob writes from
    pdf_storage (which uses config.DB_PATH) stay isolated and offline."""
    monkeypatch.setattr(_config, "PDFS_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(_config, "DB_PATH", tmp_path / "blobs.db")
    return str(tmp_path / "blobs.db")


def test_pdf_blob_round_trip_and_delete(tmp_path):
    dbp = str(tmp_path / "b.db")
    assert database.save_pdf_blob("abc.pdf", _PDF_BYTES, db_path=dbp)
    assert database.load_pdf_blob("abc.pdf", db_path=dbp) == _PDF_BYTES
    database.delete_pdf_blob("abc.pdf", db_path=dbp)
    assert database.load_pdf_blob("abc.pdf", db_path=dbp) is None


def test_saving_a_pdf_mirrors_it_into_the_db(blob_env):
    _config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    target = _config.PDFS_DIR / "deadbeef.pdf"
    got = pdf_storage._write_if_within_limit(target, _PDF_BYTES)
    assert got == (str(target), len(_PDF_BYTES))
    assert database.load_pdf_blob("deadbeef.pdf", db_path=blob_env) == _PDF_BYTES


def test_oversized_pdf_is_not_mirrored(blob_env, monkeypatch):
    monkeypatch.setattr(_config, "PDF_BLOB_MAX_BYTES", 4)
    _config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    target = _config.PDFS_DIR / "big.pdf"
    got = pdf_storage._write_if_within_limit(target, _PDF_BYTES)
    assert got is not None                       # still saved to disk
    assert database.load_pdf_blob("big.pdf", db_path=blob_env) is None


def test_ensure_local_pdf_rehydrates_from_blob_after_restart(blob_env):
    # Simulate the cloud restart: blob in the DB, file gone from disk.
    path = str(_config.PDFS_DIR / "cafe.pdf")
    database.save_pdf_blob("cafe.pdf", _PDF_BYTES, db_path=blob_env)
    paper = {"url": "https://x.example/p", "local_pdf_path": path}
    assert pdf_storage.ensure_local_pdf(paper) == path
    with open(path, "rb") as f:
        assert f.read() == _PDF_BYTES


def test_ensure_local_pdf_redownloads_when_no_blob(blob_env, monkeypatch):
    # The pdf_too_large path: nothing durable, so it re-downloads via save_pdf.
    path = str(_config.PDFS_DIR / "beef.pdf")
    calls = []

    def fake_save_pdf(url):
        calls.append(url)
        return (path, 7)

    monkeypatch.setattr(pdf_storage, "save_pdf", fake_save_pdf)
    paper = {"url": "https://x.example/big", "local_pdf_path": path}
    assert pdf_storage.ensure_local_pdf(paper) == path
    assert calls == ["https://x.example/big"]


def test_ensure_local_pdf_uses_slack_download_for_slack_uploads(blob_env, monkeypatch):
    path = str(_config.PDFS_DIR / "5111.pdf")
    monkeypatch.setattr(pdf_storage, "save_slack_file",
                        lambda dl, key, name: (path, 7))
    monkeypatch.setattr(pdf_storage, "save_pdf",
                        lambda url: pytest.fail("must not hit the URL path"))
    paper = {"url": "https://sl.example/permalink", "local_pdf_path": path,
             "slack_file_url": "https://files.slack/private", "slack_file_name": "a.pdf"}
    assert pdf_storage.ensure_local_pdf(paper) == path


def test_delete_local_pdfs_also_deletes_the_blob(blob_env):
    _config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    target = _config.PDFS_DIR / "gone.pdf"
    pdf_storage._write_if_within_limit(target, _PDF_BYTES)
    assert database.load_pdf_blob("gone.pdf", db_path=blob_env) is not None
    assert pdf_storage.delete_local_pdfs(str(target)) == 1
    assert database.load_pdf_blob("gone.pdf", db_path=blob_env) is None


def test_enrich_flags_pdf_too_large(blob_env, monkeypatch):
    # A PDF over the durable cap enriches normally but carries pdf_too_large,
    # and the log line says so.
    monkeypatch.setattr(_config, "PDF_BLOB_MAX_BYTES", 4)
    _config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    big = _config.PDFS_DIR / "huge.pdf"
    big.write_bytes(_PDF_BYTES)
    monkeypatch.setattr(metadata, "save_pdf",
                        lambda url: (str(big), len(_PDF_BYTES)))
    monkeypatch.setattr(metadata, "fetch_translator", lambda url: None)
    fields = metadata.enrich_paper({"url": "https://x.example/huge"}, None)
    assert fields["pdf_too_large"] is True
    assert fields["metadata_source"] == "pdf_saved"
    _, label = app_helpers._outcome(fields)
    assert "too large" in label


def test_enrich_leaves_small_pdfs_unflagged(blob_env, monkeypatch):
    _config.PDFS_DIR.mkdir(parents=True, exist_ok=True)
    small = _config.PDFS_DIR / "small.pdf"
    small.write_bytes(_PDF_BYTES)
    monkeypatch.setattr(metadata, "save_pdf",
                        lambda url: (str(small), len(_PDF_BYTES)))
    monkeypatch.setattr(metadata, "fetch_translator", lambda url: None)
    fields = metadata.enrich_paper({"url": "https://x.example/small"}, None)
    assert not fields.get("pdf_too_large")


def test_ensure_local_pdf_lucky_shot_when_paper_never_had_a_pdf(blob_env, monkeypatch):
    # Access-issues case: no local_pdf_path at all. ensure_local_pdf should take
    # one more shot at the download in case the traffic blocker relents.
    path = str(_config.PDFS_DIR / "lucky.pdf")
    monkeypatch.setattr(pdf_storage, "save_pdf", lambda url: (path, 7))
    assert pdf_storage.ensure_local_pdf({"url": "https://blocked.example/p"}) == path
    # Still blocked → None, quietly.
    monkeypatch.setattr(pdf_storage, "save_pdf", lambda url: None)
    assert pdf_storage.ensure_local_pdf({"url": "https://blocked.example/p"}) is None
    # No URL at all → no attempt, no crash.
    assert pdf_storage.ensure_local_pdf({}) is None
