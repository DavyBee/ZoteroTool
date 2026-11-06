"""
records.py — The shape of one source as it moves through a scan.

There is no database: a scan builds these in memory (scan.collect_papers),
enrichment fills them in (metadata.enrich_paper), and upload sends them to
Zotero (zotero_client). Zotero is the only permanent record.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class Comment(TypedDict):
    author: str          # display name
    ts: str              # Slack message ts (unique within a workspace)
    is_reply: bool       # True for a thread reply
    text: str            # cleaned message text


class Paper(TypedDict, total=False):
    # Identity
    url: str
    doi: Optional[str]

    # Bibliographic metadata
    title: Optional[str]
    authors: list[str]               # "Last, First" or a single name
    abstract: Optional[str]
    year: Optional[str]
    journal: Optional[str]           # venue: journal, institution, repository, ...
    item_type: str                   # Zotero item type
    volume: Optional[str]
    issue: Optional[str]
    pages: Optional[str]
    publisher: Optional[str]
    isbn: Optional[str]
    language: Optional[str]

    # Which lookup produced the metadata: crossref_doi, citation_tags,
    # translator (Citoid), llm, or a no-metadata outcome (link, non_paper,
    # needs_manual, pdf_saved, none). See scan.classify.
    metadata_source: str
    summary: Optional[str]           # the AI's one-line description, shown in Review
    low_context: Optional[bool]      # the AI had little page text to read

    # Slack
    comments: list[Comment]
    slack_hints: Optional[dict]      # fields typed into "Post an article"
    slack_file_url: Optional[str]    # url_private for a Slack upload (expiring token)
    slack_file_name: Optional[str]

    # Local file to attach on upload
    local_pdf_path: Optional[str]
    pdf_size_bytes: Optional[int]

    # Upload outcome
    zotero_key: Optional[str]
    upload_error: Optional[str]


def normalize_url(url: str) -> str:
    """Lowercase scheme and host, strip a trailing slash; keep the path's case
    and the query string (some sites identify the paper there)."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host, *path = rest.split("/", 1)
    return f"{scheme.lower()}://{host.lower()}" + ("/" + path[0] if path else "")


def new_paper(url: str, doi: Optional[str] = None) -> Paper:
    return {"url": url, "doi": doi, "authors": [], "comments": [],
            "metadata_source": "none"}
