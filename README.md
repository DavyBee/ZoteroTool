# Slack to Zotero

This app adds the papers and links shared in the Eviction Lab's literature Slack channel to the lab's Zotero library. You upload a Slack export, check the results, and upload them to Zotero.

## Using the app

1. Upload the channel's Slack export: the .zip file, or the channel's daily .json files.
2. Click **Scan**. The app reads every link and uploaded document and looks up its title, authors, year, and journal. A few months of messages take a minute or two.
3. Check the results, which are sorted into three groups:
   - **Ready**: metadata was found. These are selected for upload. Clear the box on any you want to leave out.
   - **Review**: the app isn't confident, or the link probably isn't a source. Each row gives the reason. Nothing here is uploaded unless you select it.
   - **Already in Zotero**: these are skipped. The table shows what matched (DOI, URL, or title) and links to the existing item.
4. Click **Upload**.

Each uploaded item gets its metadata, the PDF when one could be downloaded, and a note with the Slack post and its replies. Nothing else is added to the library.

The scan runs on the server, so refreshing the page or closing the tab doesn't stop it. Results stay until someone starts a new scan or the app restarts. If the app restarts before you upload, scan the export again; nothing is uploaded twice.

## Review reasons

| Reason | What it means |
| --- | --- |
| Little text to read | The page gave the AI very little to work with, so the details may be wrong. |
| Title differs from Slack post | The title found doesn't match the one typed into "Post an article". The link may be wrong. |
| Title from Slack post only | Nothing was found online, so the item would use the title from the Slack post. |
| Page could not be accessed | The site blocked the app or the page is gone. Adding it with the Zotero browser extension usually works. |
| Web page, not a paper | A page without authors, such as a journal homepage or a project page. |
| Likely not a source | Probably nothing to keep, such as an image or a dead link. |
| Tweet | Uploaded as a plain link if selected. |
| Slack file could not be downloaded | The file's download link in the export has expired. |
| PDF has no readable text | A PDF was found but its text couldn't be read. |
| No metadata found | No lookup recognized the link. |
| Not scanned, Error during scan | The scan was stopped or hit an error before reaching this item. Scanning again usually fixes it. |

## Hosting

The app runs on Streamlit Community Cloud from this repository.

- Main file: `app.py`. Python version: 3.14 (under Advanced settings when deploying).
- Secrets: copy `.streamlit/secrets.toml.example` into the app's Secrets box and fill in the keys. The Zotero key needs write access to the library.
- Access: set the app to private and invite lab members by email.

## Maintenance

Package versions are pinned in `requirements.txt`. Dependabot checks for updates every Sunday morning. Minor updates merge on their own once the tests pass; major updates wait for review.

A monthly GitHub check confirms the outside services the app relies on still respond as expected. It emails the repository owner only if something fails.

Python 3.14 stops receiving security updates in October 2030, and Streamlit then moves apps to a newer version. Before then, delete the app and deploy it again on the newest Python version offered, and update `.python-version` and the version in `requirements.in`. The monthly check starts failing six months ahead as a reminder.

If the default Claude model is retired, the app switches to the newest Haiku model on its own.
