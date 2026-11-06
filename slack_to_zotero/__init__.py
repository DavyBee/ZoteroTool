"""Slack to Zotero: find the sources shared in a Slack channel and add them
to a Zotero library. The Streamlit page is app.py at the top of the repo."""

from pathlib import Path

_HERE = Path(__file__).resolve().parent


def code_stamp() -> tuple:
    """When each module in this folder last changed. app.py compares it across
    reruns to notice new code after a push."""
    return tuple((p.name, p.stat().st_mtime) for p in sorted(_HERE.glob("*.py")))
