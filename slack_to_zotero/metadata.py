"""
metadata.py — Find bibliographic metadata for one source.

enrich_paper tries these in order and stops at the first that works:
    1. CrossRef, when the link or Slack post already gives a DOI
    2. SSRN links: the DOI follows from the abstract id
    3. citation_* tags on the page (the Google Scholar convention)
    4. Citoid, Wikimedia's hosted copy of Zotero's translators
    5. Claude, reading the page or PDF

Steps 1-4 are free and give the same answer every time. Every Claude request
and response is appended to state/llm_audit.jsonl.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote as url_quote, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import config
from . import http_fetch
from .records import Comment
from .pdf_storage import save_pdf, save_slack_file


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> Optional[dict]:
    """GET a URL expecting JSON. Returns parsed object or None on any error."""
    req = Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except HTTPError:
        return None
    except Exception:
        return None


def _fetch_page_text(url: str) -> tuple[str, str]:
    """The page's text for the AI step, and whether the page could be read.
    Returns (text, state):
      text   HTML with tags removed, or text extracted from a PDF, cut to
             config.LLM_PAGE_TEXT_CHARS; "" if there is none.
      state  "ok"      the page loaded (even if it had no text)
             "dead"    404 or 410: the page is gone
             "blocked" anything else (403, 429, 5xx, network error). We can't
                       tell whether the page still exists.
    """
    raw, content_type, status = http_fetch.fetch(url, max_bytes=500_000)
    if raw is None:
        return "", ("dead" if status in (404, 410) else "blocked")

    if "text/html" in content_type:
        html = raw.decode("utf-8", errors="replace")
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:config.LLM_PAGE_TEXT_CHARS], "ok"

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(raw)
        return (text[:config.LLM_PAGE_TEXT_CHARS] if text.strip()
                else "[PDF; text not extracted]"), "ok"

    return "", "ok"             # reachable (2xx) but no extractable text


# ── Citation tags on the page ─────────────────────────────────────────────────
# Publishers mark up article pages with Highwire Press "citation_*" meta tags
# (citation_title, citation_author, citation_doi, ...). Google Scholar's
# inclusion guidelines are built on them, so publishers keep them current; they
# are a long-standing convention, not something specific to one site.
# https://scholar.google.com/intl/en/scholar/inclusion.html#indexing

class _MetaTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        name = (a.get("name") or a.get("property") or "").strip().lower()
        content = " ".join(a.get("content", "").split())
        if name.startswith("citation_") and content:
            self.tags.setdefault(name, []).append(content)


class _TitleParser(HTMLParser):
    """The page's og:title, else its <title>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title = ""
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            if (a.get("property") or a.get("name") or "").lower() == "og:title":
                self.og_title = self.og_title or " ".join(a.get("content", "").split())

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())


def page_title(url: str) -> Optional[str]:
    """The title a web page gives itself, for saving it as a link. Uses the
    copy already fetched during the scan."""
    raw, content_type, _status = http_fetch.fetch(url, max_bytes=2_000_000)
    if raw is None or "html" not in (content_type or "").lower():
        return None
    parser = _TitleParser()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception:
        pass
    return (parser.og_title or parser.title or None)


def _is_site_name(title: str, url: str) -> bool:
    """True when a page title is just the site's name ("OSF" on osf.io,
    "Spotify" on open.spotify.com), which happens on pages built by JavaScript."""
    host = urlsplit(url).netloc.lower().replace("-", "").replace(".", "")
    words = re.findall(r"[a-z0-9]+", title.lower())
    return not words or all(w in host for w in words)


def _link_title(url: str, summary: Optional[str]) -> str:
    """Title for a web page saved as a link: its own title, unless that is just
    the site's name; then the AI's one-line description; then the URL."""
    title = page_title(url)
    if title and not _is_site_name(title, url):
        return title
    if summary:
        return summary.strip().rstrip(".")
    return title or url


def _is_slack_message(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    return (host == "slack.com" or host.endswith(".slack.com")) \
        and parts.path.startswith("/archives/")


def parse_citation_tags(html_bytes: bytes) -> dict[str, list[str]]:
    parser = _MetaTagParser()
    try:
        parser.feed(html_bytes.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:
        pass
    return parser.tags


_NAME_PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "di", "da",
                   "du", "la", "le", "ter", "dos", "das", "st", "st."}
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def _tag_author(name: str) -> str:
    """'Rebecca Diamond' → 'Diamond, Rebecca'; 'Clément de Chaisemartin' →
    'de Chaisemartin, Clément'. Names already in 'Last, First' form are kept."""
    name = " ".join(name.split())
    if "," in name or " " not in name:
        return name
    parts = name.split()
    suffix = parts.pop() if parts[-1].lower() in _NAME_SUFFIXES and len(parts) > 2 else ""
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in _NAME_PARTICLES:
        i -= 1
    first = " ".join(parts[:i] + ([suffix] if suffix else []))
    return f"{' '.join(parts[i:])}, {first}"


def tags_to_fields(tags: dict[str, list[str]]) -> Optional[dict]:
    """Bibliographic fields from citation tags, or None unless the page names
    both a title and at least one author."""
    def one(*names):
        for n in names:
            if tags.get(n):
                return tags[n][0]
        return None

    title = one("citation_title")
    authors = [_tag_author(a) for a in tags.get("citation_author", []) if a.strip()]
    if not title or not authors:
        return None
    if tags.get("citation_arxiv_id"):
        item_type, venue = "preprint", "arXiv"
    elif one("citation_technical_report_institution", "citation_technical_report_number"):
        item_type, venue = "report", one("citation_technical_report_institution")
    elif one("citation_journal_title"):
        item_type, venue = "journalArticle", one("citation_journal_title")
    elif one("citation_conference_title"):
        item_type, venue = "conferencePaper", one("citation_conference_title")
    elif one("citation_dissertation_institution"):
        item_type, venue = "thesis", one("citation_dissertation_institution")
    elif one("citation_inbook_title", "citation_book_title"):
        item_type, venue = "bookSection", one("citation_inbook_title", "citation_book_title")
    else:
        item_type, venue = "webpage", None
    first, last = one("citation_firstpage"), one("citation_lastpage")
    date = one("citation_publication_date", "citation_date", "citation_online_date",
               "citation_cover_date", "citation_year")
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", date or "")
    return {
        "title": clean_title(title), "authors": authors, "item_type": item_type, "journal": venue,
        "year": year.group(1) if year else None,
        "volume": one("citation_volume"), "issue": one("citation_issue"),
        "pages": f"{first}-{last}" if first and last else first,
        "publisher": one("citation_publisher"), "isbn": one("citation_isbn"),
        "language": one("citation_language"), "abstract": one("citation_abstract"),
        "doi": _clean_doi(one("citation_doi")),
    }


def fetch_citation_metadata(url: str) -> Optional[tuple[str, dict]]:
    """Metadata from the page's citation tags. A DOI found there goes to
    CrossRef, and is trusted only if CrossRef's title matches the page's.
    Returns (metadata_source, fields) or None."""
    from .zotero_index import titles_match
    raw, content_type, _status = http_fetch.fetch(url, max_bytes=2_000_000)
    if raw is None or "html" not in (content_type or "").lower():
        return None
    tags = parse_citation_tags(raw)
    doi = _clean_doi((tags.get("citation_doi") or [None])[0])
    if doi:
        found = lookup_doi(doi)
        page_title = (tags.get("citation_title") or [None])[0]
        if found and (not page_title or titles_match(found[1].get("title"), page_title)):
            return found
    fields = tags_to_fields(tags)
    return ("citation_tags", fields) if fields else None


# ── SSRN ──────────────────────────────────────────────────────────────────────
# SSRN blocks automated readers (Citoid included), but every SSRN paper has a
# DOI built from its abstract id: 10.2139/ssrn.<id>. That is SSRN's own scheme.
# The DOI is only used if CrossRef has a record for it.

_SSRN_PATH_RE = re.compile(r"/abstract=(\d+)", re.I)


def ssrn_doi(url: str) -> Optional[str]:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if not (host == "ssrn.com" or host.endswith(".ssrn.com")):
        return None
    params = {k.lower(): v for k, v in parse_qsl(parts.query)}
    abstract_id = params.get("abstract_id") or params.get("abstractid")
    if not abstract_id:
        m = _SSRN_PATH_RE.search(parts.path)
        abstract_id = m.group(1) if m else None
    if abstract_id and abstract_id.isdigit():
        return f"10.2139/ssrn.{abstract_id}"
    return None


# ── Citoid (Wikimedia's hosted Zotero translator) ─────────────────────────────

# Results are cached per URL, and requests are spaced at least
# config.CITOID_MIN_INTERVAL apart, as Wikimedia's API policy asks.
_CITOID_CACHE: dict[str, Optional[dict]] = {}
_last_citoid_call = 0.0
# Scans enrich several papers at once; the lock makes the throttle hold across
# threads by reserving each request's start time in turn.
_citoid_lock = threading.Lock()
CITOID_ATTEMPTS = 3
# Room for models that think before answering (the reply itself is ~300 tokens).
# Only tokens actually generated are billed.
LLM_MAX_TOKENS = 4000
LLM_TIMEOUT = 90           # seconds per request
CITOID_RETRY_WAIT = 3.0      # seconds; multiplied by the attempt number


def _citoid_get(url: str) -> Optional[dict]:
    """
    GET Citoid's Zotero endpoint for `url` and return the first Zotero-native
    item, or None. Citoid returns a JSON array of items; we take the first.

    Cached per-URL for the current run and throttled to respect Wikimedia's REST
    usage policy. A descriptive, contact-carrying User-Agent (config.
    CITOID_USER_AGENT) is required by that policy.
    """
    global _last_citoid_call
    if url in _CITOID_CACHE:
        return _CITOID_CACHE[url]

    api = config.CITOID_BASE + url_quote(url, safe="")
    item: Optional[dict] = None
    # Citoid is free and has no SLA: under load it answers 429/5xx or times
    # out. Those are retried with backoff; a 404 ("no metadata") is final.
    for attempt in range(CITOID_ATTEMPTS):
        with _citoid_lock:
            wait = config.CITOID_MIN_INTERVAL - (time.monotonic() - _last_citoid_call)
            if wait > 0:
                time.sleep(wait)
            _last_citoid_call = time.monotonic()
        req = Request(api, headers={
            "User-Agent": config.CITOID_USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                item = data[0]
            break
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                break
        except (URLError, TimeoutError, OSError):
            pass
        except Exception:
            break
        time.sleep(CITOID_RETRY_WAIT * (attempt + 1))

    _CITOID_CACHE[url] = item
    return item


def fetch_translator(url: str) -> Optional[dict]:
    """The Zotero item Citoid returns for `url`, or None if it returns nothing
    usable. Results are labelled metadata_source "translator"."""
    raw = _citoid_get(url)
    if raw and not _is_translator_junk(raw, url):
        return raw
    return None


def lookup_doi(doi: str) -> Optional[tuple[str, dict]]:
    """
    Resolve a DOI to a flat fields dict via a direct CrossRef call. CrossRef is
    authoritative for DOIs (Citoid often misses them), so the DOI path goes
    straight there rather than through the hosted translator.

    Returns (metadata_source_label, fields_dict) on success, or None.
    """
    data = fetch_crossref_doi(doi)
    if data and (data.get("title") or [None])[0]:
        return ("crossref_doi", crossref_to_fields(data))
    return None


def _extract_pdf_text(pdf_bytes: bytes,
                      max_chars: Optional[int] = None) -> str:
    """The PDF's text, via pypdf, up to max_chars (config.LLM_PAGE_TEXT_CHARS
    by default). Stops reading pages once it has that much: extracting a long
    PDF is slow, and only the start is sent to the AI. "" for a scanned PDF or
    one pypdf can't read; the AI step then reads the first pages as images."""
    import logging
    from io import BytesIO
    from pypdf import PdfReader

    # pypdf logs warnings to stderr on malformed/non-PDF input (e.g. a truncated
    # download or HTML mis-served as a PDF). We already handle those as "no text",
    # so silence the noise.
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    limit = max_chars or config.LLM_PAGE_TEXT_CHARS
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        parts, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
                total += len(text)
            if total >= limit:
                break
        return "\n".join(parts).strip()
    except Exception:
        # Encrypted or unreadable: no text.
        return ""


def _first_pages_pdf(pdf_bytes: bytes, n: int) -> Optional[bytes]:
    """Return a new PDF containing only the first `n` pages of `pdf_bytes`, for the
    vision fallback (so Claude reads just the title/abstract pages, not the whole
    file). Returns None if the PDF can't be parsed/sliced."""
    import logging
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages[:n]:
            writer.add_page(page)
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return None


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _clean_doi(doi: Optional[str]) -> Optional[str]:
    """Strip trailing punctuation and validate structure; return a well-formed
    DOI or None.

    A DOI is `10.<registrant>/<suffix>`. A bare prefix such as `10.2139/`
    names a publisher, not a paper, and would make unrelated papers look like
    duplicates."""
    if not doi:
        return None
    doi = doi.strip().rstrip(".,;:)>]\"'")
    return doi if _DOI_RE.match(doi) else None


def _is_translator_junk(raw: dict, url: str = "") -> bool:
    """
    True if Citoid's result is too thin to be a citation. Every web page has a
    title, so also require an author, a date, an abstract, or a DOI. A title
    that is only the site's name ("- YouTube") means Citoid didn't see the page.
    """
    title = (raw.get("title") or "").strip()
    if not title or (url and _is_site_name(title, url)):
        return True
    has_author   = bool(raw.get("creators"))
    has_year     = bool(raw.get("date"))
    has_abstract = bool(raw.get("abstractNote"))
    has_doi      = bool(_clean_doi(raw.get("DOI")))
    if not (has_author or has_year or has_abstract or has_doi):
        return True
    return False


def translator_to_fields(raw: dict) -> dict:
    """Convert a translation-server (Zotero-native) item into our flat field dict."""
    year_raw = raw.get("date") or ""
    year = year_raw[:4] if year_raw[:4].isdigit() else None
    journal = (raw.get("publicationTitle") or raw.get("websiteTitle")
               or raw.get("bookTitle") or raw.get("publisher")
               or raw.get("institution") or raw.get("repository"))
    isbn_raw = raw.get("ISBN")
    isbn = (isbn_raw[0] if isinstance(isbn_raw, list) and isbn_raw
            else isbn_raw if isinstance(isbn_raw, str) else None)
    return {
        "title":      clean_title(raw.get("title")),
        "authors":    _translator_creators(raw),
        "abstract":   raw.get("abstractNote"),
        "year":       year,
        "journal":    journal,
        "item_type":  raw.get("itemType", "journalArticle"),
        "keywords":   [t["tag"] for t in (raw.get("tags") or []) if t.get("tag")],
        "volume":     raw.get("volume"),
        "issue":      raw.get("issue"),
        "pages":      raw.get("pages"),
        "publisher":  raw.get("publisher"),
        "isbn":       isbn,
        "language":   raw.get("language"),
        "doi":        _clean_doi(raw.get("DOI")),
    }


def _translator_creators(raw: dict) -> list[str]:
    """Translator output uses Zotero's native creators array. Filter to authors."""
    out: list[str] = []
    for c in raw.get("creators") or []:
        if c.get("creatorType") and c.get("creatorType") != "author":
            continue
        last  = (c.get("lastName") or "").strip()
        first = (c.get("firstName") or "").strip()
        name  = (c.get("name") or "").strip()  # one-name creators
        if last:
            out.append(f"{last}, {first}".rstrip(", "))
        elif name:
            out.append(name)
    return out


# ── CrossRef DOI ──────────────────────────────────────────────────────────────

_CROSSREF_TYPE_MAP = {
    "journal-article":     "journalArticle",
    "book-chapter":        "bookSection",
    "proceedings-article": "conferencePaper",
    "report":              "report",
    "book":                "book",
    "posted-content":      "preprint",
    "dataset":             "dataset",
    "dissertation":        "thesis",
    "monograph":           "book",
    "reference-entry":     "encyclopediaArticle",
}


def search_crossref_title(title: str, author: Optional[str] = None) -> Optional[tuple[str, dict]]:
    """Find a work on CrossRef by its title (e.g. one typed into the lab's
    "Post an article" Slack form), tolerating typos. A hit is only accepted
    when its title matches, and either the typed author is among its authors
    or, with no author typed, the title is specific (four or more words).
    Returns lookup_doi's (source, fields) or None."""
    from .zotero_index import title_key, titles_match, _typo
    words = (title_key(title) or "").split()
    if not author and len(words) < 4:
        return None
    query = f"query.bibliographic={url_quote(title)}"
    if author:
        query += f"&query.author={url_quote(author)}"
    raw = _http_get_json(f"{config.CROSSREF_BASE}?{query}&rows=5&select=DOI,title")
    items = ((raw or {}).get("message") or {}).get("items") or [] if isinstance(raw, dict) else []
    want = (title_key(author) or "").split()
    for it in items:
        found = (it.get("title") or [None])[0]
        if not (found and it.get("DOI") and titles_match(found, title)):
            continue
        result = lookup_doi(it["DOI"])
        if result is None:
            continue
        if want:
            names = {w for a in result[1].get("authors") or [] for w in (title_key(a) or "").split()}
            if not any(_typo(w, n) for w in want for n in names):
                continue
        return result
    return None


def fetch_crossref_doi(doi: str) -> Optional[dict]:
    """Direct DOI lookup. Returns the inner 'message' dict or None."""
    raw = _http_get_json(f"{config.CROSSREF_BASE}/{url_quote(doi, safe='/')}")
    return (raw or {}).get("message") if isinstance(raw, dict) else None


def crossref_to_fields(data: dict) -> dict:
    title_list = data.get("title") or []
    container  = data.get("container-title") or []
    abstract   = re.sub(r"<[^>]+>", " ", data.get("abstract") or "").strip() or None
    item_type  = _CROSSREF_TYPE_MAP.get(data.get("type") or "", "journalArticle")
    doi        = _clean_doi(data.get("DOI"))
    # Where the work appeared. Without a journal name, a report or preprint is
    # credited to its publisher (e.g. NBER); an article is left blank rather
    # than credited to a publishing company. 10.2139 is SSRN's DOI prefix.
    if container:
        venue = container[0]
    elif doi and doi.startswith("10.2139/"):
        venue = "SSRN"
    elif item_type in ("report", "preprint"):
        venue = data.get("publisher")
    else:
        venue = None
    return {
        "title":      clean_title(title_list[0]) if title_list else None,
        "authors":    _crossref_authors(data),
        "abstract":   abstract,
        "year":       _crossref_year(data),
        "journal":    venue,
        "item_type":  item_type,
        "volume":     data.get("volume"),
        "issue":      data.get("issue"),
        "pages":      data.get("page"),
        "publisher":  data.get("publisher"),
        "doi":        doi,
    }


def clean_title(title: Optional[str]) -> Optional[str]:
    """Remove HTML formatting tags (CrossRef titles can contain <i>, <sub>,
    ...) and decode entities."""
    if not title:
        return title
    if isinstance(title, list):             # a lookup returned ["Title"]
        title = title[0] if title else None
    if isinstance(title, (int, float)) and not isinstance(title, bool):
        title = str(title)
    if not isinstance(title, str):
        return None
    import html as _html
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", "", title)).split()) or None


def _crossref_authors(data: dict) -> list[str]:
    out: list[str] = []
    for a in data.get("author") or []:
        last  = (a.get("family") or "").strip()
        first = (a.get("given") or "").strip()
        if last:
            out.append(f"{last}, {first}".rstrip(", "))
        elif a.get("name"):
            out.append(a["name"].strip())
    return out


def _crossref_year(data: dict) -> Optional[str]:
    for f in ("published-print", "published-online", "created"):
        parts = (data.get(f) or {}).get("date-parts") or [[]]
        if parts and parts[0]:
            return str(parts[0][0])
    return None


# ── LLM fallback ──────────────────────────────────────────────────────────────

_LLM_REQUIRED_KEYS = {"title", "authors", "year", "item_type"}

# With less page text than this, the AI is mostly guessing from the link and the
# Slack messages, so the item goes to Review ("Page could not be accessed").
_LOW_CONTEXT_CHARS = 500


# Set when the Anthropic key is rejected or out of credit. Later items then
# skip the AI step (every call would fail the same way), and the page says why
# so many items are in Review. scan.Job clears it when a search starts.
llm_problem: Optional[str] = None


def _account_problem(e: Exception) -> Optional[str]:
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if status == 401:
        return "The Anthropic key was rejected. Check it in the app's secrets."
    if "credit balance" in text or "billing" in text:
        return "The Anthropic account is out of credit."
    if status == 403:
        return "The Anthropic key isn't allowed to use the model."
    return None


def fetch_llm(url: str, comments: list[Comment],
              anthropic_key: str, audit_log_path: str,
              page_text: Optional[str] = None,
              pdf_doc: Optional[bytes] = None) -> Optional[dict]:
    """
    Ask Claude for the source's metadata as JSON. Fetches the page itself unless
    given `page_text` (text already extracted, e.g. from a .docx) or `pdf_doc`
    (the first pages of a scanned PDF, read as images). Every request and
    response is appended to audit_log_path.
    """
    global llm_problem
    if llm_problem:
        return None
    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return None

    if pdf_doc is not None:
        fetch_state = "ok"             # Claude sees the file directly via vision
    elif page_text is None:
        page_text, fetch_state = _fetch_page_text(url)
    else:
        fetch_state = "ok"             # caller supplied the content (a local file)
    slack_context = "\n".join(
        f"[{c.get('author', '?')}] {c.get('text', '')}"
        for c in comments[:5]
    )

    prompt = (
        "Extract publication metadata from the information below.\n"
        "Return ONLY valid JSON with these exact keys (use null or [] for unknowns):\n"
        '{"title":null,"authors":[],"abstract":null,"year":null,"journal":null,'
        '"item_type":"journalArticle","doi":null,"volume":null,"issue":null,'
        '"pages":null,"keywords":[],"summary":null,"category":"paper"}\n\n'
        "item_type must be one of: journalArticle, preprint, report, book, "
        "bookSection, conferencePaper, thesis, blogPost, webpage\n"
        "authors should be ['Last, First', ...].\n\n"
        "The page often lists other works too — reference lists, 'related "
        "articles', 'cited by' entries, or recommendations. Extract the "
        "metadata for the ONE central article the page is primarily about: its "
        "main title/heading and author byline, normally at the top of the page "
        "and matching the page title. That central article is the work to "
        "cite. Do NOT return null just because other citations appear lower on "
        "the page — those are context, not the answer.\n\n"
        "summary is an ULTRA-SHORT label — 10 words MAX, a fragment not a "
        "sentence — naming just WHAT the source is or is about, enough to judge "
        "at a glance. Examples: 'NBER working paper on rent control', "
        "'Reporter's X/Twitter profile', 'County eviction dashboard', "
        "'Brookings report on housing vouchers', 'Google Drive folder'. "
        "Describe the source ITSELF only — do NOT mention access or availability "
        "status (no 'paywalled', 'blocked', 'login required', 'content "
        "unavailable', etc.); that's handled separately and is just noise here. "
        "Base it on the page content when available; otherwise infer from the "
        "URL/path and Slack discussion. Always provide it.\n\n"
        "category is a routing judgment, separate from the metadata fields. "
        "Everything here was deliberately shared in a research lab's "
        "paper-sharing channel, so assume good intent. Choose EXACTLY one of:\n"
        "  \"paper\" — the URL is, or points to, a SINGLE directly-citable "
        "work: a research paper, working paper, technical report, journal "
        "article, preprint, conference paper, thesis, or dataset publication "
        "(e.g. sciencedirect.com/article/, jamanetwork.com, springer.com, "
        "arxiv.org), OR a single piece of long-form written content a "
        "researcher would cite: a news/magazine article, op-ed, policy brief, "
        "think-tank or government report, or a substantive analytical blog "
        "post. Use \"paper\" even if you cannot extract the fields above "
        "because the page is paywalled or blocked, as long as the URL/domain/"
        "path indicate one specific such work. A file-hosting link (Google "
        "Drive, Dropbox, OneDrive, etc.) is \"paper\" when it points to a "
        "SINGLE such work — e.g. a hosted PDF of a working paper, article, or "
        "report — even though the file can't be read here; the filename, page "
        "title, or Slack discussion is usually enough to tell.\n"
        "  \"link\" — NOT a single citable work, but a genuine resource worth "
        "keeping in a reference library as a saved webpage: a journal homepage "
        "or issue table-of-contents, an author or organization profile page, a "
        "project/report landing or index page, a tweet or thread, a Google "
        "Drive folder (or a hosted file that is NOT a single citable work — a "
        "dataset, spreadsheet, or slide deck), a dataset portal, or a useful "
        "tool. Anything scholarly or that might point to a useful file is at "
        "least a \"link\" — never \"junk\". When unsure between \"paper\" and "
        "\"link\", choose \"link\".\n"
        "  \"junk\" — clutter with no place in a reference library: a reaction "
        "GIF or image, a dead or parked page, a login screen with nothing "
        "behind it, or a bare site homepage with no specific content. Only "
        "choose \"junk\" when you are confident; if the page content below is "
        "empty you usually CANNOT be confident, so prefer \"paper\" or "
        "\"link\".\n\n"
        f"URL: {url}\n\n"
        f"Slack discussion:\n{slack_context}\n\n"
        + ("The publication is the attached PDF (its first pages)."
           if pdf_doc is not None else f"Page content:\n{page_text}")
    )

    # Text-only by default; for the vision fallback, prepend the PDF as a document
    # block so Claude reads the scanned pages directly.
    if pdf_doc is not None:
        import base64
        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_doc).decode("ascii")}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    model = config.llm_model()
    audit = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url":       url,
        "model":     model,
        "prompt":    prompt,
        "vision":    pdf_doc is not None,
        "response":  None,
        "parsed":    None,
        "error":     None,
    }

    try:
        # A reply takes seconds; the SDK's default wait is ten minutes, which
        # would hold up a search worker on a stalled connection.
        client = anthropic_sdk.Anthropic(api_key=anthropic_key, timeout=LLM_TIMEOUT)
        messages = [{"role": "user", "content": content}]
        try:
            resp = client.messages.create(model=model, max_tokens=LLM_MAX_TOKENS,
                                           messages=messages)
        except Exception as e:
            # Model retired/deprecated (404) → self-heal: switch to a current model
            # and retry once, so an unmaintained deploy keeps running with no human.
            new_model = (_auto_heal_model(anthropic_key, model)
                         if _is_model_unavailable(e) else None)
            if not new_model:
                raise
            audit["model"] = model = new_model
            audit["model_auto_switched"] = True
            resp = client.messages.create(model=model, max_tokens=LLM_MAX_TOKENS,
                                           messages=messages)
        # Read the answer by block type: newer models put a thinking block
        # first, so content[0] is not necessarily the text.
        raw_text = "".join(getattr(b, "text", "") for b in resp.content
                           if getattr(b, "type", "") == "text")
        if not raw_text.strip():
            audit["error"] = f"no text in response (stop_reason={resp.stop_reason})"
            return None
        audit["response"] = raw_text
        # Some models (notably Haiku) emit valid JSON followed by explanatory
        # prose ("Note: ..."). Pull just the first balanced JSON object.
        cleaned = _extract_json_object(raw_text)
        parsed = json.loads(cleaned)
        if not _is_valid_llm_response(parsed):
            audit["error"] = "schema validation failed"
            return None
        audit["parsed"] = parsed
        # A valid reply with no title is not an error: enrich_paper uses the
        # category and whether the page loaded to pick the Review reason.
        fields = _llm_to_fields(parsed)
        fields["_fetch_state"] = fetch_state
        # Little page text means the AI was mostly guessing (unless it read a
        # scanned PDF's pages as images).
        fields["_low_context"] = (pdf_doc is None
            and len((page_text or "").strip()) < _LOW_CONTEXT_CHARS)
        return fields
    except Exception as e:
        # A 404 reaching here means the configured model was retired AND self-heal
        # couldn't find a replacement (no models API / offline). Flag it distinctly
        # so a wave of failures is diagnosable; otherwise it's a generic API error.
        if _is_model_unavailable(e):
            audit["error"] = (f"model '{model}' unavailable (deprecated/retired) "
                              f"and auto-fallback found no replacement: {e}")
        else:
            audit["error"] = f"{type(e).__name__}: {e}"
            llm_problem = llm_problem or _account_problem(e)
        return None
    finally:
        try:
            with open(audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _is_model_unavailable(e: Exception) -> bool:
    """A retired/deprecated/typo'd model surfaces as a 404 NotFoundError."""
    return getattr(e, "status_code", None) == 404 or type(e).__name__ == "NotFoundError"


def _auto_heal_model(anthropic_key: str, current_model: str) -> Optional[str]:
    """The configured model was retired (404). Switch to the newest Haiku, else
    the newest Sonnet, else Opus, from the live list of models. Returns the new
    model id, or None if none could be found."""
    try:
        import anthropic as anthropic_sdk
        models = anthropic_sdk.Anthropic(api_key=anthropic_key, timeout=LLM_TIMEOUT).models.list(
            limit=1000).data
    except Exception:
        return None
    claude = [m for m in models if str(getattr(m, "id", "")).startswith("claude")]
    if not claude:
        return None
    claude.sort(key=lambda m: str(getattr(m, "created_at", "") or ""), reverse=True)
    chosen = None
    for tier in ("haiku", "sonnet", "opus"):
        chosen = next((m.id for m in claude if tier in m.id and m.id != current_model),
                      None)
        if chosen:
            break
    chosen = chosen or claude[0].id
    if chosen == current_model:
        return None
    config.set_llm_model(chosen)
    return chosen


def _extract_json_object(text: str) -> str:
    """
    Pull the first balanced JSON object out of a string that may contain
    surrounding prose, code-fence markers, or trailing commentary. Walks
    the text tracking brace depth and string state so commentary like
    'Note: ...' after the closing `}` is dropped cleanly.
    """
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    if start == -1:
        return text  # let json.loads fail naturally

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return text  # unbalanced; json.loads will fail


def _is_valid_llm_response(parsed) -> bool:
    """Checks the reply's shape only. The title may be null: that means the
    AI found nothing citable, which enrich_paper handles."""
    if not isinstance(parsed, dict):
        return False
    return _LLM_REQUIRED_KEYS.issubset(parsed.keys())


def _is_useful_partial(out: dict) -> bool:
    """
    Whether an AI result with no title still identifies the work. A year or a
    journal alone doesn't: thousands of articles share them.

    A useful partial requires:
      - a DOI (Zotero will resolve it to full metadata on upload), OR
      - authors AND year AND journal all present (manually findable later).
    """
    if out.get("doi"):
        return True
    authors = out.get("authors") or []
    return bool(authors and out.get("year") and out.get("journal"))


def _llm_to_fields(d: dict) -> dict:
    # `category` is a routing hint for the cascade ("paper" | "link" | "junk"),
    # not a Paper field we persist. enrich_paper pops it (and _fetch_state) out
    # before merging.
    return {
        "title":        clean_title(d.get("title")),
        "authors":      d.get("authors") or [],
        "abstract":     d.get("abstract"),
        "year":         str(d["year"]) if d.get("year") else None,
        "journal":      d.get("journal"),
        "item_type":    d.get("item_type") or "journalArticle",
        "keywords":     d.get("keywords") or [],
        "volume":       d.get("volume"),
        "issue":        d.get("issue"),
        "pages":        d.get("pages"),
        "doi":          _clean_doi(d.get("doi")),
        "summary":      d.get("summary"),
        "category":     (d.get("category") or "paper"),
    }


# ── Cascade ───────────────────────────────────────────────────────────────────

def enrich_paper(paper: dict, anthropic_key: Optional[str] = None,
                 audit_log_path: Optional[str] = None) -> dict:
    """
    Run the full cascade for one paper. Returns a dict of fields the caller
    should merge into the Paper. Bookkeeping fields are always set; metadata
    fields are only set when a stage actually resolves them.
    """
    url = paper["url"]
    doi = paper.get("doi")
    audit_log_path = audit_log_path or str(config.LLM_AUDIT_PATH)

    fields: dict = {
        "attempts":            (paper.get("attempts") or 0) + 1,
        "metadata_fetched_at": datetime.now(timezone.utc).isoformat(),
        "last_error":          None,
    }

    # A tweet is saved as a plain link; it has no bibliographic metadata.
    from .slack_parser import is_tweet_url, tweet_date, tweet_title
    if is_tweet_url(url):
        fields["metadata_source"] = "link"
        fields["item_type"] = "webpage"
        fields["title"] = tweet_title(url)
        day = tweet_date(url)
        if day:
            fields["year"] = str(day.year)
        return fields

    # A link to another Slack message is a reference within Slack, not a source.
    if _is_slack_message(url):
        fields["metadata_source"] = "non_paper"
        fields["summary"] = "Link to a Slack message"
        return fields

    # A file uploaded to Slack: download it and have the AI read it. The
    #    download link in an export expires, so older exports often fail here.
    slack_url = paper.get("slack_file_url")
    if slack_url:
        path = paper.get("local_pdf_path")
        if not path:
            got = save_slack_file(slack_url, url, paper.get("slack_file_name") or "")
            if got is None:
                fields["metadata_source"] = "needs_manual"
                fields["summary"] = f"Slack upload: {paper.get('slack_file_name') or ''}"
                fields["last_error"] = "slack file download failed (token expired?)"
                return fields
            path, size = got
            fields["local_pdf_path"] = path
            fields["pdf_size_bytes"] = size
        if anthropic_key:
            fields.update(enrich_local_file(path, url, paper.get("comments") or [],
                                            anthropic_key, audit_log_path))
        else:
            fields["metadata_source"] = "pdf_saved"
        return fields

    # Look for a PDF first, whichever step ends up supplying the metadata, so
    # the file can be attached on upload.
    pdf_result = save_pdf(url)
    if pdf_result is not None:
        path, size = pdf_result
        fields["local_pdf_path"] = path
        fields["pdf_size_bytes"] = size

    # 1. CrossRef, if the link or Slack post already gives a DOI.
    if doi:
        result = lookup_doi(doi)
        if result:
            source, data = result
            _merge_truthy(fields, data)
            fields["metadata_source"] = source
            return fields

    # 2. SSRN: the DOI follows from the abstract id (SSRN blocks page reads).
    if not doi:
        guessed = ssrn_doi(url)
        result = lookup_doi(guessed) if guessed else None
        if result:
            source, data = result
            _merge_truthy(fields, data)
            fields["metadata_source"] = source
            return fields

    # 3. Citation tags on the page.
    result = fetch_citation_metadata(url)
    if result:
        source, data = result
        _merge_truthy(fields, data)
        fields["metadata_source"] = source
        return fields

    # 4. Citoid.
    raw = fetch_translator(url)
    if raw:
        _merge_truthy(fields, translator_to_fields(raw))
        fields["metadata_source"] = "translator"
        return fields

    # A PDF but no metadata yet: have the AI read the PDF.
    if pdf_result is not None:
        if anthropic_key:
            fields.update(enrich_local_file(path, url, paper.get("comments") or [],
                                            anthropic_key, audit_log_path))
        else:
            fields["metadata_source"] = "pdf_saved"
        return fields

    # 5. The AI reads the page.
    if anthropic_key:
        out = fetch_llm(url, paper.get("comments") or [],
                        anthropic_key, audit_log_path)
        if out is None:
            # API or parse failure; see state/llm_audit.jsonl
            fields["last_error"] = "llm: API call failed (see state/llm_audit.jsonl)"
        else:
            # Used below to sort the result; not kept on the record.
            category = out.pop("category", "paper")
            fetch_state = out.pop("_fetch_state", "blocked")

            # The AI's one-line description, shown in Review.
            if out.get("summary"):
                fields["summary"] = str(out["summary"]).strip()
            fields["llm_category"] = category
            fields["low_context"] = bool(out.pop("_low_context", False))

            # 5a. A DOI from the AI is checked against CrossRef; if CrossRef has
            #     it, CrossRef's metadata is used instead of the AI's.
            llm_doi = out.get("doi")
            if llm_doi and llm_doi != doi:
                result = lookup_doi(llm_doi)
                if result:
                    source, data = result
                    _merge_truthy(fields, data)
                    fields["metadata_source"] = source
                    return fields

            # 5b. A title, or enough else to identify the work (a DOI, or
            #     authors + year + journal).
            if out.get("title") or _is_useful_partial(out):
                _merge_truthy(fields, out)
                fields["metadata_source"] = "llm"
                return fields

            # 5c. Nothing citable. What it was decides the Review reason:
            #     page loaded and not worth keeping   → non_paper ("Likely not a source")
            #     page loaded, a link worth keeping   → link (uploaded as a web page)
            #     page gone (404/410) and not a paper → non_paper
            #     anything else                       → needs_manual ("Page could not
            #                                            be accessed")
            #     A dead page that looks like a paper stays needs_manual: it may
            #     have moved.
            if fetch_state == "ok" and category == "junk":
                fields["metadata_source"] = "non_paper"
            elif fetch_state == "ok" and category == "link":
                fields["metadata_source"] = "link"
                fields["item_type"] = "webpage"
                fields["title"] = _link_title(url, fields.get("summary"))
            elif fetch_state == "dead" and category != "paper":
                fields["metadata_source"] = "non_paper"
            else:
                fields["metadata_source"] = "needs_manual"
            return fields
    else:
        reasons = ["Citoid returned nothing usable"]
        if not doi:
            reasons.append("no DOI to look up via CrossRef")
        reasons.append("no anthropic key for LLM")
        fields["last_error"] = "; ".join(reasons)

    fields["metadata_source"] = "none"
    return fields


def _merge_truthy(target: dict, source: dict) -> None:
    """Copy from source into target, but only fields with a truthy value."""
    for k, v in source.items():
        if v not in (None, "", [], {}):
            target[k] = v


# ── Local file (PDF / .docx) metadata via the LLM ─────────────────────────────
# For a PDF with no other metadata (and for Slack uploads), the AI reads the
# file itself. If that fails too, the item is "pdf_saved" and goes to Review.

def _extract_docx_text(path: str) -> str:
    """The body text of a .docx, with paragraph breaks. "" if unreadable."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)             # paragraphs → newlines
    text = re.sub(r"<[^>]+>", " ", xml)            # strip all XML tags
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _extract_file_text(file_path: str) -> str:
    """Extract text from a local PDF or .docx for the LLM (stdlib only). Returns
    "" when nothing readable comes out (e.g. a scanned/image-only PDF). Capped at
    config.LLM_PAGE_TEXT_CHARS, same as the page-text fetch."""
    if file_path.lower().endswith(".docx"):
        text = _extract_docx_text(file_path)
    else:                                          # PDF (the default)
        try:
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError:
            return ""
        text = _extract_pdf_text(raw)
    return (text or "")[:config.LLM_PAGE_TEXT_CHARS]


def enrich_local_file(file_path: str, url: str, comments: list[Comment],
                      anthropic_key: Optional[str], audit_log_path: str) -> dict:
    """Extract a local file's text (PDF or .docx) and run the LLM extractor on it.
    Returns the fields to merge into the paper:
      - title (or useful partial) found → metadata_source "llm" + the metadata
      - otherwise (no key, unreadable/scanned, or nothing extracted) → "pdf_saved",
        which goes to Review (the file is still attached if it is uploaded).
    Routing-only hints (category / _fetch_state) are dropped; summary is kept.

    A scanned PDF (under _LOW_CONTEXT_CHARS of text) is sent as images of its
    first config.LLM_VISION_PAGES pages instead."""
    text = _extract_file_text(file_path)
    out = None
    if anthropic_key:
        is_pdf = not file_path.lower().endswith(".docx")
        if is_pdf and len(text.strip()) < _LOW_CONTEXT_CHARS:
            try:
                with open(file_path, "rb") as f:
                    doc = _first_pages_pdf(f.read(), config.LLM_VISION_PAGES)
            except OSError:
                doc = None
            if doc is not None:
                out = fetch_llm(url, comments, anthropic_key, audit_log_path,
                                pdf_doc=doc)
        # Text path: the normal case, and the fallback if vision was skipped or
        # the API call failed but we still have some extracted text to try.
        if out is None and text.strip():
            out = fetch_llm(url, comments, anthropic_key, audit_log_path,
                            page_text=text)
    fields: dict = {}
    if out is not None:
        out.pop("category", None)
        out.pop("_fetch_state", None)
        fields["low_context"] = bool(out.pop("_low_context", False))
        if out.get("title") or _is_useful_partial(out):
            _merge_truthy(fields, out)
            fields["metadata_source"] = "llm"
            return fields
        if out.get("summary"):
            fields["summary"] = out["summary"]
    fields["metadata_source"] = "pdf_saved"
    return fields
