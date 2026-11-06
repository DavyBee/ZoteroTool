"""
Search a Slack export once and save the results, so the app can show them
again without searching (upload the saved file in step 1). For testing.

    .venv/bin/python tools/save_search.py EXPORT.zip OUT.json

Uses the Anthropic key in .streamlit/secrets.toml. Zotero isn't checked
while saving, so every item is looked up. The saved file holds Slack
messages and names: keep it out of the repository.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_to_zotero import config  # noqa: E402
from slack_to_zotero import scan  # noqa: E402


def main(export_path: str, out_path: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        export = scan.read_export([(Path(export_path).name, Path(export_path).read_bytes())], tmp)
        day_files = next(iter(export.channels.values()))
        papers = scan.collect_papers(day_files, export.user_map)
    print(f"{len(papers)} links and documents")
    # No Zotero check: every item is looked up, so the file has full details
    # whatever is in the library now. Loading the file checks Zotero then.
    job = scan.Job(papers, config.anthropic_api_key(), str(config.LLM_AUDIT_PATH))
    job.start()
    while job.snapshot()["phase"] == scan.SCANNING:
        snap = job.snapshot()
        print(f"\r{snap['done']} of {snap['total']}", end="", flush=True)
        time.sleep(2)
    print()
    Path(out_path).write_bytes(scan.saved_results(job.papers, Path(out_path).name))
    print(f"Saved {len(job.papers)} items to {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
