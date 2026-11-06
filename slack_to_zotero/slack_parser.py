"""
slack_parser.py — Extract URLs, document uploads, and threaded comments from
Slack JSON exports.

A Slack export is one JSON array of messages per channel per day. Top-level
messages and thread replies live in the same arrays; replies have `thread_ts`
set to their parent's `ts`. A reply is filed under the day it was *posted*, so a
thread started on Monday with replies on Tuesday is split across two files.
parse_files therefore groups threads across every file it is given, not per
file, so the whole discussion stays attached to the link.

Messages are kept when they carry content: normal posts, thread broadcasts,
file shares, and bot/workflow posts. The lab's "Post an article" Slack workflow
posts as a bot, and its structured fields (title, authors, keywords) are parsed
into `hints`. Channel bookkeeping (joins, purpose/name changes, etc.) is skipped.
"""

from __future__ import annotations

import datetime
import html
import json
import re
from typing import Optional, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .records import Comment


# ── Regexes ───────────────────────────────────────────────────────────────────

# <https://example.com>  or  <https://example.com|label>
URL_RE = re.compile(r'<(https?://[^>|]+)(?:\|[^>]*)?>?')

# DOI in free text or URL path
DOI_RE = re.compile(r'(10\.\d{4,}/[^\s&?#"\'<>]+)', re.I)

# <@U03M8H8N82Z>: a Slack user mention; group(1) is the user id
USER_MENTION_RE = re.compile(r'<@([A-Z0-9]+)>')

# <!subteam^S019|@everybody>, <!here>, <!channel>: group mentions;
# group(1) is the label if Slack included one, else the keyword.
SPECIAL_MENTION_RE = re.compile(r'<!(?:[^>|]*\|)?@?([^>|]+)>')

# A bold label in a workflow post, e.g. "*title*" or "*may interest:*".
_LABEL_RE = re.compile(r'\*([^*\n]{1,40})\*')

# "submission from <@U123>": the person who filled in the workflow form.
_SUBMITTER_RE = re.compile(r'submission from\s*<@([A-Z0-9]+)>', re.I)


# ── Message filtering ─────────────────────────────────────────────────────────

# Subtypes that carry real content. Everything else (channel_join,
# channel_purpose, channel_name, pinned_item, …) is channel bookkeeping.
_CONTENT_SUBTYPES = {None, "bot_message", "thread_broadcast", "file_share",
                     "me_message"}


def _is_content(msg: dict) -> bool:
    return msg.get("subtype") in _CONTENT_SUBTYPES


# ── URL cleaning ──────────────────────────────────────────────────────────────

# Query parameters that only track where a click came from. Dropping them makes
# the same paper shared from a newsletter and from a browser look identical.
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
                    "_hsenc", "_hsmi", "mkt_tok", "ref_src", "ref_url",
                    "casa_token", "dgcid"}     # the last two are Elsevier's
# Parameters that only track on one site; elsewhere the name may mean something.
_SITE_PARAMS = {"sciencedirect.com": {"via"}}
# Twitter/X share links append ?s=20&t=<token>, which identify the sharer.
_TWITTER_PARAMS = {"s", "t"}
_TWITTER_HOSTS = {"twitter.com", "www.twitter.com", "x.com", "www.x.com",
                  "mobile.twitter.com"}


# University library proxies: "doi-org.ezproxy.princeton.edu/10.1/x" is
# "doi.org/10.1/x" (EZproxy writes the real host's dots as "-" and its
# hyphens as "--"), and ".../login?url=<real url>" wraps the real link.
_EZPROXY_HOST_RE = re.compile(r"^([a-z0-9-]+)\.ezproxy\.[a-z0-9.-]+$", re.I)


# Redirect links that carry the real address in a query parameter: Outlook
# safelinks, Google and Google Scholar redirects, Facebook's link shim.
_REDIRECTS = (
    (re.compile(r"(^|\.)safelinks\.protection\.outlook\.com$"), "/", "url"),
    (re.compile(r"^(www\.)?google\.[a-z.]+$"), "/url", "q"),
    (re.compile(r"^(www\.)?google\.[a-z.]+$"), "/url", "url"),
    (re.compile(r"^scholar\.google\.[a-z.]+$"), "/scholar_url", "url"),
    (re.compile(r"^l\.facebook\.com$"), "/l.php", "u"),
)


def _unwrap_redirect(url: str) -> str:
    parts = urlsplit(url)
    host, path = parts.netloc.lower(), parts.path or "/"
    params = dict(parse_qsl(parts.query))
    for host_re, path_prefix, param in _REDIRECTS:
        target = params.get(param, "")
        if host_re.search(host) and path.startswith(path_prefix) and target.startswith("http"):
            return target
    return url


def _unproxy(url: str) -> str:
    url = _unwrap_redirect(url)
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if ".ezproxy." in host or host.startswith("ezproxy."):
        target = dict(parse_qsl(parts.query)).get("url") or dict(parse_qsl(parts.query)).get("qurl")
        if target and target.startswith("http"):
            return target
    m = _EZPROXY_HOST_RE.match(host)
    if m:
        real = m.group(1).replace("--", "\0").replace("-", ".").replace("\0", "-")
        return urlunsplit(parts._replace(scheme="https", netloc=real))
    return url


def clean_url(url: str) -> str:
    """Decode HTML entities Slack leaves in URLs (`&amp;`), undo university
    proxy links, and drop tracking parameters. Every other parameter is kept,
    in order: some sites identify the paper in the query string (e.g. SSRN's
    abstract_id)."""
    url = _unproxy(html.unescape(url.strip()))
    parts = urlsplit(url)
    if not parts.query:
        return url
    host = parts.netloc.lower()
    site_params = set().union(*(names for site, names in _SITE_PARAMS.items()
                                if host == site or host.endswith("." + site)))
    if host in _TWITTER_HOSTS:
        site_params |= _TWITTER_PARAMS

    def tracking(k: str) -> bool:
        # Mangled forms: "%3Butm_x" (from "&amp;utm_x") and "via%3Dihub="
        # (an encoded "="), so compare the part before any "=".
        k = k.lower().lstrip(";").split("=")[0]
        return k.startswith("utm_") or k in _TRACKING_PARAMS or k in site_params

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not tracking(k)]
    return urlunsplit(parts._replace(query=urlencode(kept, doseq=True)))


_TWEET_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/([^/?#]+)/status/\d+", re.I)


def is_tweet_url(url: str) -> bool:
    """True for a twitter.com / x.com link to an individual post."""
    return bool(_TWEET_RE.match((url or "").strip()))


# Post ids since 2010 encode their creation time: milliseconds since this epoch,
# shifted left 22 bits (X's "Snowflake" ids).
_SNOWFLAKE_EPOCH_MS = 1288834974657


def tweet_date(url: str) -> Optional[datetime.date]:
    m = re.search(r"/status/(\d+)", url or "")
    if not m or int(m.group(1)) < 2 ** 40:          # older ids carry no date
        return None
    ms = (int(m.group(1)) >> 22) + _SNOWFLAKE_EPOCH_MS
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date()


def tweet_title(url: str) -> str:
    """'Post by @handle, 12 Mar 2024' for a tweet link."""
    m = _TWEET_RE.match((url or "").strip())
    title = f"Post by @{m.group(1)}" if m else "Post on X"
    day = tweet_date(url)
    return f"{title}, {day.day} {day:%b %Y}" if day else title


# ── Types ─────────────────────────────────────────────────────────────────────

class WorkflowHints(TypedDict, total=False):
    title: str
    authors: str              # free text as typed, e.g. "Preis" or "A & B"
    keywords: list[str]


class ThreadedLink(TypedDict, total=False):
    url: str
    doi: Optional[str]        # extracted from the URL path, None if not present
    comments: list[Comment]   # the entire Slack thread the URL appeared in,
                              # in chronological order (parent first, then replies)
    hints: WorkflowHints      # present only for "Post an article" submissions


class ThreadedFile(TypedDict):
    url: str                  # the file's Slack permalink (stable)
    download_url: str         # url_private: its token expires
    name: str                 # original filename, e.g. "hepburn_2018.pdf"
    comments: list[Comment]   # the whole thread, chronological


class ParsedFile(TypedDict):
    path: str
    messages: int             # raw message count (including subtypes)
    links: list[ThreadedLink]
    files: list[ThreadedFile]  # document *uploads* (the files[] array)


# ── Public API ────────────────────────────────────────────────────────────────

def parse_files(filepaths: list[str],
                user_map: Optional[dict[str, str]] = None) -> list[ParsedFile]:
    """
    Load and parse a list of Slack export files.

    Threads are grouped across all the files, so replies posted on a later day
    are attached to the link from the earlier day. Each link or upload is
    reported under the file where it first appears. The user-id → name map is
    built from every file before any text is cleaned, because a user's name may
    only appear in a file other than the one they posted in. `user_map` seeds
    it, e.g. from a full export's users.json (see read_users_json).
    """
    loaded: list[tuple[str, list[dict]]] = []
    user_map = dict(user_map or {})

    for path in filepaths:
        try:
            with open(path, encoding="utf-8") as f:
                messages = json.load(f)
        except Exception:
            continue
        if not isinstance(messages, list):
            continue
        messages = [_sane(m) for m in messages if isinstance(m, dict)]
        loaded.append((path, messages))
        _absorb_user_profiles(messages, user_map)

    tagged = [(path, m) for path, messages in loaded for m in messages]
    threads = _group_threads(tagged)

    by_path: dict[str, ParsedFile] = {
        path: {"path": path, "messages": len(messages), "links": [], "files": []}
        for path, messages in loaded
    }
    for path, link in _links_from_threads(threads, user_map):
        by_path[path]["links"].append(link)
    for path, fa in _files_from_threads(threads, user_map):
        by_path[path]["files"].append(fa)
    return [by_path[path] for path, _ in loaded]


def read_users_json(data: bytes | str) -> dict[str, str]:
    """user id → display name from a full Slack export's users.json. Returns
    {} if the content isn't a users list."""
    try:
        users = json.loads(data)
    except (ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    if isinstance(users, list):
        for u in users:
            if not isinstance(u, dict) or not u.get("id"):
                continue
            prof = u.get("profile") or {}
            name = (u.get("real_name") or prof.get("real_name")
                    or prof.get("display_name") or u.get("name"))
            if name:
                out[u["id"]] = name
    return out


def extract_doi(text: str) -> Optional[str]:
    m = DOI_RE.search(text)
    return m.group(1).rstrip("./") if m else None


def clean_text(text: str, user_map: dict[str, str]) -> str:
    """
    Resolve user mentions to @real-names, strip URL/label syntax, decode
    HTML entities, collapse whitespace.
    """
    text = USER_MENTION_RE.sub(
        lambda m: "@" + user_map.get(m.group(1), m.group(1)),
        text,
    )
    text = SPECIAL_MENTION_RE.sub(lambda m: "@" + m.group(1), text)
    text = URL_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_workflow_post(text: str) -> Optional[dict[str, str]]:
    """Fields of a "Post an article" workflow submission, keyed by lowercase
    label ("title", "author or institution", "keywords", "article link",
    "may interest"). Returns None if `text` isn't a workflow submission.

    Handles both layouts Slack has used: one field per line, and (older posts)
    all fields on one line. A label is only recognised if it is one of the known
    field names, so a bold word inside a title isn't mistaken for a field."""
    if not _SUBMITTER_RE.search(text or ""):
        return None
    known = {"title", "author or institution", "keywords", "article link",
             "may interest"}
    marks = [(m, m.group(1).strip().rstrip(":").strip().lower())
             for m in _LABEL_RE.finditer(text)]
    marks = [(m, label) for m, label in marks if label in known]
    fields: dict[str, str] = {}
    for i, (m, label) in enumerate(marks):
        end = marks[i + 1][0].start() if i + 1 < len(marks) else len(text)
        # Newer posts bold the values too ("*title*\n*Some Title*").
        value = text[m.end():end].strip().strip("*").strip()
        if value and value.strip("_").strip().lower() != "left blank":
            fields[label] = value
    return fields


# ── Internals ─────────────────────────────────────────────────────────────────

def _group_threads(tagged: list[tuple[str, dict]]) -> list[list[tuple[str, dict]]]:
    """Group (path, message) pairs into threads (thread_ts, else ts; a
    standalone message is a thread of one), each sorted chronologically."""
    threads: dict[str, list[tuple[str, dict]]] = {}
    for path, msg in tagged:
        if not _is_content(msg):
            continue
        thread_id = msg.get("thread_ts") or msg.get("ts")
        if not thread_id:
            continue
        threads.setdefault(thread_id, []).append((path, msg))
    out = []
    for msgs in threads.values():
        msgs.sort(key=lambda pm: float(pm[1].get("ts") or 0))
        out.append(msgs)
    return out


def _sane(msg: dict) -> dict:
    """Make a message safe to read when an export has odd values (a
    hand-edited or damaged file): text is a string, files a list of dicts."""
    msg = dict(msg)
    if not isinstance(msg.get("text"), str):
        msg["text"] = ""
    files = msg.get("files")
    msg["files"] = [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
    for f in msg["files"]:
        for k in ("permalink", "url_private", "name"):
            if not isinstance(f.get(k), (str, type(None))):
                f[k] = None
    return msg


def _links_from_threads(threads, user_map) -> list[tuple[str, ThreadedLink]]:
    out: list[tuple[str, ThreadedLink]] = []
    for msgs in threads:
        found: dict[str, tuple[str, Optional[WorkflowHints]]] = {}
        for path, m in msgs:
            text = m.get("text") or ""
            hints = _workflow_hints(text)
            for match in URL_RE.finditer(text):
                try:
                    url = clean_url(match.group(1))
                except ValueError:            # malformed, e.g. "http://[x"
                    continue
                if url not in found:
                    found[url] = (path, hints)
        if not found:
            continue
        comments = [_make_comment(m, user_map) for _, m in msgs]
        for url, (path, hints) in found.items():
            link: ThreadedLink = {"url": url, "doi": extract_doi(url),
                                  "comments": comments}
            if hints:
                link["hints"] = hints
            out.append((path, link))
    return out


def _files_from_threads(threads, user_map) -> list[tuple[str, ThreadedFile]]:
    """Identity is the file's Slack permalink (stable, token-free). download_url
    is url_private, whose embedded token expires, so the bytes must be fetched
    at ingest time, not deferred."""
    out: list[tuple[str, ThreadedFile]] = []
    for msgs in threads:
        comments = None
        seen: set[str] = set()
        for path, m in msgs:
            for f in (m.get("files") or []):
                if not _is_document_file(f):
                    continue
                permalink = (f.get("permalink") or "").strip()
                download = (f.get("url_private") or "").strip()
                if not permalink or not download or permalink in seen:
                    continue
                seen.add(permalink)
                if comments is None:
                    comments = [_make_comment(x, user_map) for _, x in msgs]
                out.append((path, {
                    "url": permalink,
                    "download_url": download,
                    "name": f.get("name") or "",
                    "comments": comments,
                }))
    return out


# Uploaded files treated as papers. Images, slides, and spreadsheets are not.
_DOC_EXTS = {"pdf", "docx", "doc", "rtf", "odt"}


def _is_document_file(f: dict) -> bool:
    name = (f.get("name") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in _DOC_EXTS:
        return True
    mt = (f.get("mimetype") or "").lower()
    return any(s in mt for s in ("pdf", "wordprocessing", "msword",
                                 "opendocument.text", "rtf"))


def _workflow_hints(text: str) -> Optional[WorkflowHints]:
    fields = parse_workflow_post(text)
    if not fields:
        return None
    hints: WorkflowHints = {}
    if fields.get("title"):
        hints["title"] = html.unescape(fields["title"])
    if fields.get("author or institution"):
        hints["authors"] = html.unescape(fields["author or institution"])
    if fields.get("keywords"):
        kws = [k.strip() for k in html.unescape(fields["keywords"]).split(",")]
        hints["keywords"] = [k for k in kws if k]
    return hints or None


def _absorb_user_profiles(messages: list[dict], user_map: dict[str, str]) -> None:
    for msg in messages:
        uid = msg.get("user")
        profile = msg.get("user_profile") or {}
        name = profile.get("real_name") or profile.get("display_name")
        if uid and name and uid not in user_map:
            user_map[uid] = name


def _make_comment(msg: dict, user_map: dict[str, str]) -> Comment:
    text = msg.get("text", "") or ""
    fields = parse_workflow_post(text) if msg.get("subtype") == "bot_message" else None
    if fields is not None:
        # A workflow post: credit the person who submitted the form, and render
        # the fields as readable lines instead of the raw "*label* value" markup.
        uid = _SUBMITTER_RE.search(text).group(1)
        author = user_map.get(uid, "Unknown")
        lines = []
        for label, name in (("title", "Title"),
                            ("author or institution", "Authors"),
                            ("keywords", "Keywords"),
                            ("may interest", "May interest")):
            if fields.get(label):
                lines.append(f"{name}: {clean_text(fields[label], user_map)}")
        body = "\n".join(lines)
    else:
        profile = msg.get("user_profile") or {}
        author = (profile.get("real_name")
                  or profile.get("display_name")
                  or user_map.get(msg.get("user", ""))
                  or msg.get("username")
                  or "Unknown")
        body = clean_text(text, user_map)
    return {
        "author": author,
        "ts": msg.get("ts", ""),
        "is_reply": bool(msg.get("thread_ts")) and msg.get("thread_ts") != msg.get("ts"),
        "text": body,
    }
