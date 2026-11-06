"""
Tests for slack_parser.

The synthetic tests use made-up messages shaped like a real Slack export, so
they are safe to run anywhere (CI included). test_real_export runs the parser
over an actual export and checks invariants; it is skipped unless
SLACK_EXPORT_DIR points at a folder of Slack day-files.

    SLACK_EXPORT_DIR="/path/to/export" pytest test_parser.py
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from slack_to_zotero import slack_parser
from slack_to_zotero.slack_parser import clean_url, parse_files, parse_workflow_post


def _write(tmp_path, name, messages):
    path = tmp_path / name
    path.write_text(json.dumps(messages), encoding="utf-8")
    return str(path)


def _user(uid, name, ts, text, **extra):
    return {"type": "message", "user": uid, "ts": ts, "text": text,
            "user_profile": {"real_name": name}, **extra}


WORKFLOW_NEW = (
    "*Post article* submission from <@U1>\n\n"
    "*author or institution*\n*Smith &amp; Jones*\n\n"
    "*title*\n*Evictions and Health*\n\n"
    "*keywords*\n*eviction, health*\n\n"
    "*article link*\n<https://example.org/paper?utm_source=x&amp;id=7>\n\n"
    "*may interest*\n<@U2>\n\n"
)
WORKFLOW_OLD = (
    "*Post article* submission from <@U1> *author or institution* Lee "
    "*keywords* rent, housing *article link* <https://example.org/old> "
    "*may interest* _left blank_"
)


# ── URL cleaning ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("https://www.nber.org/papers/w1?utm_campaign=a&amp;utm_medium=email",
     "https://www.nber.org/papers/w1"),
    ("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=123&amp;utm_source=x",
     "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=123"),
    ("https://x.org/a.pdf?%3Butm_medium=P&%3Butm_source=P", "https://x.org/a.pdf"),
    ("https://twitter.com/u/status/1?s=20&t=abc", "https://twitter.com/u/status/1"),
    ("https://example.org/search?s=20", "https://example.org/search?s=20"),
    ("https://example.org/p?id=1&fbclid=zz", "https://example.org/p?id=1"),
    ("https://example.org/plain", "https://example.org/plain"),
    ("https://www.sciencedirect.com/science/article/pii/S1?casa_token=abc&via%3Dihub=&dgcid=x",
     "https://www.sciencedirect.com/science/article/pii/S1"),
    ("https://www.sciencedirect.com/science/article/pii/S1?via=ihub",
     "https://www.sciencedirect.com/science/article/pii/S1"),
    ("https://example.org/p?via=bus", "https://example.org/p?via=bus"),
])
def test_clean_url(raw, expected):
    assert clean_url(raw) == expected


# ── Workflow posts ────────────────────────────────────────────────────────────

def test_workflow_post_fields_new_layout():
    f = parse_workflow_post(WORKFLOW_NEW)
    assert f["title"] == "Evictions and Health"
    assert f["author or institution"] == "Smith &amp; Jones"
    assert f["keywords"] == "eviction, health"


def test_workflow_post_fields_old_single_line_layout():
    f = parse_workflow_post(WORKFLOW_OLD)
    assert f["author or institution"] == "Lee"
    assert f["keywords"] == "rent, housing"
    assert "may interest" not in f            # "_left blank_" is dropped
    assert "title" not in f


def test_non_workflow_text_is_not_a_workflow_post():
    assert parse_workflow_post("Great *new* paper <https://a.org>") is None


def test_workflow_bot_posts_are_ingested_and_credited_to_submitter(tmp_path):
    path = _write(tmp_path, "2024-01-01.json", [
        _user("U1", "Ana Ruiz", "1.0", "hi"),            # gives U1 a name
        {"type": "message", "subtype": "bot_message", "username": "Post an article",
         "bot_id": "B1", "ts": "2.0", "text": WORKFLOW_NEW},
    ])
    [pf] = parse_files([path])
    [link] = pf["links"]
    assert link["url"] == "https://example.org/paper?id=7"
    assert link["hints"] == {"title": "Evictions and Health",
                             "authors": "Smith & Jones",
                             "keywords": ["eviction", "health"]}
    [c] = link["comments"]
    assert c["author"] == "Ana Ruiz"
    assert "Title: Evictions and Health" in c["text"]
    assert "*" not in c["text"]


# ── Threads ───────────────────────────────────────────────────────────────────

def test_replies_on_a_later_day_attach_to_the_link(tmp_path):
    day1 = _write(tmp_path, "2024-01-01.json", [
        _user("U1", "Ana", "100.0", "Read this <https://a.org/p>",
              thread_ts="100.0", reply_count=2),
    ])
    day2 = _write(tmp_path, "2024-01-02.json", [
        _user("U2", "Ben", "200.0", "agreed", thread_ts="100.0",
              parent_user_id="U1"),
        _user("U3", "Cy", "201.0", "same", thread_ts="100.0",
              parent_user_id="U1", subtype="thread_broadcast"),
    ])
    pf1, pf2 = parse_files([day1, day2])
    [link] = pf1["links"]
    assert [c["author"] for c in link["comments"]] == ["Ana", "Ben", "Cy"]
    assert [c["is_reply"] for c in link["comments"]] == [False, True, True]
    assert pf2["links"] == []                 # reported once, under its first day


def test_link_only_in_a_reply_is_found(tmp_path):
    day1 = _write(tmp_path, "2024-01-01.json", [
        _user("U1", "Ana", "100.0", "anyone have the paper?", thread_ts="100.0"),
    ])
    day2 = _write(tmp_path, "2024-01-02.json", [
        _user("U2", "Ben", "200.0", "<https://a.org/p>", thread_ts="100.0"),
    ])
    pf1, pf2 = parse_files([day1, day2])
    assert pf1["links"] == []
    [link] = pf2["links"]
    assert len(link["comments"]) == 2


def test_channel_bookkeeping_is_skipped(tmp_path):
    path = _write(tmp_path, "d.json", [
        {"type": "message", "subtype": "channel_join", "user": "U1", "ts": "1.0",
         "text": "<@U1> has joined <https://joined.example>"},
        {"type": "message", "subtype": "channel_purpose", "ts": "2.0",
         "text": "purpose <https://purpose.example>"},
    ])
    [pf] = parse_files([path])
    assert pf["links"] == []


def test_group_mentions_are_readable():
    text = "<!subteam^S019|@everybody> and <!here> see <https://a.org>"
    assert slack_parser.clean_text(text, {}) == "@everybody and @here see"


# ── Real export (local only) ──────────────────────────────────────────────────

EXPORT_DIR = os.environ.get("SLACK_EXPORT_DIR")


@pytest.mark.skipif(not EXPORT_DIR, reason="SLACK_EXPORT_DIR not set")
def test_real_export():
    files = sorted(glob.glob(os.path.join(EXPORT_DIR, "*.json")))
    assert files, f"no .json files in {EXPORT_DIR}"
    parsed = parse_files(files)
    links = [l for pf in parsed for l in pf["links"]]

    # URLs are clean.
    for l in links:
        assert "&amp;" not in l["url"], l["url"]
        assert "utm_" not in l["url"], l["url"]

    # Every link inside a workflow post is ingested.
    urls = {l["url"] for l in links}
    for path in files:
        for m in json.load(open(path, encoding="utf-8")):
            if m.get("subtype") == "bot_message" and parse_workflow_post(m.get("text", "")):
                for raw in slack_parser.URL_RE.findall(m["text"]):
                    assert clean_url(raw) in urls, raw

    # Every reply to a message with a link is attached to that link.
    attached = {c["ts"] for l in links for c in l["comments"]}
    parents = {}
    messages = [m for p in files for m in json.load(open(p, encoding="utf-8"))]
    for m in messages:
        if slack_parser._is_content(m) and m.get("thread_ts") in (None, m.get("ts")):
            parents[m.get("ts")] = m
    for m in messages:
        parent = parents.get(m.get("thread_ts"))
        if (slack_parser._is_content(m) and parent is not None
                and m.get("ts") != parent.get("ts")
                and slack_parser.URL_RE.search(parent.get("text") or "")):
            assert m["ts"] in attached, (m["ts"], m.get("text", "")[:60])


@pytest.mark.parametrize("raw, expected", [
    ("https://doi-org.ezproxy.princeton.edu/10.1080/07352166.2019.1662728",
     "https://doi.org/10.1080/07352166.2019.1662728"),
    ("https://www-jstor-org.ezproxy.princeton.edu/stable/123",
     "https://www.jstor.org/stable/123"),
    ("https://cody--cook-com.ezproxy.princeton.edu/p.pdf", "https://cody-cook.com/p.pdf"),
    ("https://login.ezproxy.princeton.edu/login?url=https://example.org/a",
     "https://example.org/a"),
])
def test_clean_url_undoes_library_proxies(raw, expected):
    assert clean_url(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.amazon.com%2Fdp%2F1&data=05",
     "https://www.amazon.com/dp/1"),
    ("https://scholar.google.com/scholar_url?url=https%3A%2F%2Fa.org%2Fp.pdf&hl=en", "https://a.org/p.pdf"),
    ("https://www.google.com/url?q=https://a.org/p&sa=D", "https://a.org/p"),
    ("https://l.facebook.com/l.php?u=https%3A%2F%2Fa.org%2Fp&h=x", "https://a.org/p"),
])
def test_clean_url_unwraps_redirect_links(raw, expected):
    assert clean_url(raw) == expected


def test_a_google_search_is_not_treated_as_a_redirect():
    assert clean_url("https://www.google.com/search?q=https://a.org").startswith(
        "https://www.google.com/search?")


def test_tweet_title_includes_the_date_when_the_id_has_one():
    assert slack_parser.tweet_title("https://x.com/nberpubs/status/1770873531364257837") == \
        "Post by @nberpubs, 21 Mar 2024"
    assert slack_parser.tweet_title("https://twitter.com/old/status/20") == "Post by @old"
