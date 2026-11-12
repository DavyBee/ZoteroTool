# Slack to Zotero

This app adds the papers and links shared in the Eviction Lab's literature Slack channel to the lab's Zotero library. You upload a Slack export, check the results, and upload them to Zotero.

## Using the app

1. Upload the channel's Slack export: the .zip file, or the channel's daily .json files.
2. Click **Search for sources**. The app reads every link and uploaded document and looks up its title, authors, year, and journal. 
3. Review the results, which are sorted into three groups:
   - **Ready**: metadata was found. These are uploaded. Fix details by double-clicking a cell, **Move to Review**,or **Remove**.
   - **Review**: the app couldn't identify these, or they probably aren't sources or block automated access. **Mark ready for upload**, **Edit** its details in place, or **Remove** each. These are not uploaded until marked **Ready for upload**.
   - **Already in Zotero**: these are skipped. The table shows what matched (DOI, URL, or title) and links to the existing item.
4. Click **Upload to Zotero**. Upload all sources in **Ready** to the Zotero library folder, skipping those currently in **Review**.

Every upload to Zotero includes the source metadata, a PDF (where possible), and a note of the Slack messages that accompanied the source. Some items like tweets and other web pages are saved as webpage items with their link. Working papers are usually treated differently to published versions.

## Review reasons

| Reason | What it means |
| --- | --- |
| Page could not be accessed | The site blocked the app, the page is gone, or it had too little text to identify. Adding it with the Zotero browser extension usually works. |
| Title from Slack post only | Nothing was found online, so the item would use the title from the Slack post. |
| Likely not a source | Probably nothing to keep, such as an image, a dead link, or a link to another Slack message. |
| Slack file could not be downloaded | The file's download link in the export has expired. |
| PDF has no readable text | A PDF was found but its text couldn't be read. |
| No metadata found | No lookup recognized the link. |
| Moved from Ready | It was moved from Ready to Review by hand. |
| Not searched, Error during search | The search hit an error before reaching this item. Searching again usually fixes it. |

## Maintenance

Package versions are pinned in `requirements.txt` and are not updated on a schedule. When a package has a known security problem, Dependabot emails the repository owner and opens a pull request with the fix, which merges on its own once the tests pass. One that fails the tests stays open. To update every package together, rebuild the pins with the command at the top of `requirements.txt` (add `--upgrade`), run the tests, and do one test upload before merging.

A monthly GitHub check confirms the outside services the app relies on still respond as expected. It emails the repository owner only if something fails.

Python 3.14 stops receiving security updates in October 2030, and Streamlit then moves apps to a newer version. Before then, delete the app and deploy it again on the newest Python version offered, and update the two Python versions in `pyproject.toml`.

If the default Claude model is retired, the app switches to the newest Haiku model on its own.

## Files

- `app.py`: the page.
- `slack_to_zotero/`: reading the Slack export, looking up each source, checking Zotero for duplicates, and uploading.
- `tests/`: run on every pull request and every push to main.
- `tools/save_search.py`: saves a finished search to a file, which can be uploaded in step 1 to show those results again without searching. For testing changes to the page.
