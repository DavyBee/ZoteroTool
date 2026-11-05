"""
zotero_index.py — What is already in the Zotero library, for duplicate checks.

Zotero is the only record of what has been uploaded, so each scan reads the
target (the configured collection, or the whole library if none is set) and
builds an in-memory index of its top-level items by DOI, cleaned URL, and
title. Nothing is written to Zotero here.

A full read happens at the start of each scan (Zotero returns 100 items per
request). Just before upload, refresh() fetches only items added or changed
since then, to catch anything someone added in the meantime.
"""

from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from typing import Optional

from slack_parser import clean_url


# ── Normalization ─────────────────────────────────────────────────────────────

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI_IN_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\S+)$", re.I)
_EXTRA_DOI_RE = re.compile(r"^\s*DOI:\s*(10\.\S+)", re.I | re.M)
_VALID_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


def doi_key(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    d = _DOI_PREFIX_RE.sub("", doi.strip()).rstrip(".,;)").lower()
    # A bare prefix ("10.2139/") names a publisher, not a work.
    return d if _VALID_DOI_RE.match(d) else None


def url_key(url: Optional[str]) -> Optional[str]:
    """Scheme, "www.", and trailing slash don't distinguish pages; the path's
    case and the (tracking-free) query string do."""
    if not url:
        return None
    u = clean_url(url)
    m = re.match(r"^https?://([^/?#]+)([^#]*)", u, re.I)
    if not m:
        return None
    host = m.group(1).lower()
    if host.startswith("www."):
        host = host[4:]
    rest = m.group(2)
    path, _, query = rest.partition("?")
    return host + path.rstrip("/") + ("?" + query if query else "")


def title_key(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = html.unescape(title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def year_of(value: Optional[str]) -> Optional[str]:
    m = _YEAR_RE.search(value or "")
    return m.group(1) if m else None


def first_author(names: list[str]) -> Optional[str]:
    """Lowercased last name of the first author ("Last, First" or "Name")."""
    for n in names:
        n = (n or "").strip()
        if n:
            return n.split(",")[0].strip().split()[-1].lower()
    return None


def _typo(x: str, y: str) -> bool:
    """Same word, allowing a typo (one or two letters off)."""
    return x == y or (len(x) > 3 and len(y) > 3 and SequenceMatcher(None, x, y).ratio() >= 0.75)


def _words_match(a: list[str], b: list[str]) -> bool:
    """Word lists equal up to typos, plus a few missing words in long titles
    (one per eight words). A different word in the same place is a different
    title: "Where the Landlords Live" is not "Where the Landlords Are"."""
    if not a or not b:
        return False
    missing = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1 and all(
                _typo(x, y) for x, y in zip(a[i1:i2], b[j1:j2])):
            continue
        if tag in ("insert", "delete"):
            missing += max(i2 - i1, j2 - j1)
            continue
        return False
    return missing <= max(len(a), len(b)) // 8


# A working paper and its published version share a title but are kept as
# separate items.
WORKING_PAPER_TYPES = {"report", "preprint", "workingPaper"}
PUBLISHED_TYPES = {"journalArticle", "book", "bookSection"}


def different_versions(kind_a: Optional[str], kind_b: Optional[str]) -> bool:
    """True for a working paper and a published work (in either order)."""
    return ((kind_a in WORKING_PAPER_TYPES and kind_b in PUBLISHED_TYPES)
            or (kind_a in PUBLISHED_TYPES and kind_b in WORKING_PAPER_TYPES))


def titles_match(a: Optional[str], b: Optional[str]) -> bool:
    """Loose comparison for a human-typed title against fetched metadata.
    Tolerates typos, a left-off subtitle, and the odd missing word. Never
    matches titles whose numbers differ ("Part 1" vs "Part 2")."""
    ka, kb = title_key(a), title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if set(re.findall(r"\d+", ka)) != set(re.findall(r"\d+", kb)) \
            and re.search(r"\d", ka) and re.search(r"\d", kb):
        return False
    wa, wb = ka.split(), kb.split()
    if _words_match(wa, wb):
        return True
    # One side left off the subtitle: compare it with the start of the other.
    short, long_ = sorted((wa, wb), key=len)
    return len(short) >= 3 and _words_match(short, long_[:len(short)])


# ── The index ─────────────────────────────────────────────────────────────────

class LibraryIndex:
    def __init__(self, web_base: str = "") -> None:
        self.web_base = web_base          # e.g. https://www.zotero.org/groups/1
        self.version = 0
        self._by_doi: dict[str, str] = {}
        self._by_url: dict[str, str] = {}
        # title → [(year, first author, item type, key)]
        self._by_title: dict[str, list[tuple]] = {}
        self.titles: dict[str, str] = {}      # item key → title as stored in Zotero
        self.size = 0

    def add(self, item: dict) -> None:
        data = item.get("data", item)
        key = data.get("key") or item.get("key")
        if not key or data.get("deleted") or data.get("itemType") in ("note", "attachment"):
            return
        self.size += 1
        if data.get("title"):
            self.titles[key] = data["title"]
        d = doi_key(data.get("DOI"))
        if not d:
            m = _EXTRA_DOI_RE.search(data.get("extra") or "")
            d = doi_key(m.group(1)) if m else None
        if d:
            self._by_doi.setdefault(d, key)
        u = data.get("url")
        if u:
            m = _DOI_IN_URL_RE.match(u.strip())
            if m and doi_key(m.group(1)):
                self._by_doi.setdefault(doi_key(m.group(1)), key)
            uk = url_key(u)
            if uk:
                self._by_url.setdefault(uk, key)
        t = title_key(data.get("title"))
        if t:
            creators = data.get("creators") or []
            names = [c.get("lastName") or c.get("name") or "" for c in creators]
            self._by_title.setdefault(t, []).append(
                (year_of(data.get("date")), first_author(names), data.get("itemType"), key))

    def has_published(self, title: Optional[str]) -> bool:
        """Whether the library has a journal article, book, or chapter with
        this exact title."""
        t = title_key(title)
        return any(kind in PUBLISHED_TYPES for _y, _a, kind, _k in self._by_title.get(t, []))

    def item_url(self, key: str) -> str:
        return f"{self.web_base}/items/{key}" if self.web_base else ""

    def match_identifiers(self, paper: dict) -> Optional[tuple[str, str]]:
        """(item key, "DOI" | "URL") by identifiers known before enrichment."""
        d = doi_key(paper.get("doi"))
        if not d:
            m = _DOI_IN_URL_RE.match((paper.get("url") or "").strip())
            d = doi_key(m.group(1)) if m else None
        if d and d in self._by_doi:
            return self._by_doi[d], "DOI"
        u = url_key(paper.get("url"))
        if u and u in self._by_url:
            return self._by_url[u], "URL"
        return None

    def match(self, paper: dict) -> Optional[tuple[str, str]]:
        """(item key, "DOI" | "URL" | "Title") using everything enrichment found.
        An identical title counts when the years match, or for two journal
        articles a year apart (online and print dates). Otherwise different
        years are different versions (a working paper and the article).
        When a year is missing, it counts if the first authors match, the title
        is six words or more, or this item has no authors (a landing page or a
        thin record); short generic titles alone don't."""
        hit = self.match_identifiers(paper)
        if hit:
            return hit
        t = title_key(paper.get("title"))
        if not t or t not in self._by_title:
            return None
        year = year_of(paper.get("year"))
        author = first_author(paper.get("authors") or [])
        words = len(t.split())
        kind = paper.get("item_type")
        for item_year, item_author, item_kind, key in self._by_title[t]:
            if different_versions(kind, item_kind):
                continue
            if year and item_year:
                if year == item_year:
                    return key, "Title"
                # A journal article's online and print dates often differ by a
                # year. A working paper and its article are different types.
                if kind == item_kind == "journalArticle" and abs(int(year) - int(item_year)) == 1:
                    return key, "Title"
            elif author and item_author and author == item_author:
                return key, "Title"
            elif words >= 6 or (not author and words >= 3):
                return key, "Title"
        return None


# ── Reading Zotero ────────────────────────────────────────────────────────────

def _fetch(zot, collection: Optional[str], **params) -> list[dict]:
    if collection:
        return zot.everything(zot.collection_items_top(collection, limit=100, **params))
    return zot.everything(zot.top(limit=100, **params))


def _web_base(zot, library_type: str, library_id: str) -> str:
    if library_type == "group":
        return f"https://www.zotero.org/groups/{library_id}"
    try:
        return f"https://www.zotero.org/{zot.key_info()['username']}"
    except Exception:
        return ""


def load(zot, library_type: str, library_id: str,
         collection: Optional[str] = None) -> LibraryIndex:
    """Read the whole target. The version is taken first, so an item changed
    while reading is picked up again by the next refresh()."""
    index = LibraryIndex(_web_base(zot, library_type, library_id))
    index.version = zot.last_modified_version()
    for item in _fetch(zot, collection):
        index.add(item)
    return index


def refresh(index: LibraryIndex, zot, collection: Optional[str] = None) -> None:
    """Add items created or changed since the last read."""
    version = zot.last_modified_version()
    if version == index.version:
        return
    for item in _fetch(zot, collection, since=index.version):
        index.add(item)
    index.version = version
