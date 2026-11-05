# Slack to Zotero

This app adds the papers and links shared in the Eviction Lab's literature Slack channel to the lab's Zotero library. You upload a Slack export, check the results, and upload them to Zotero.

## Using the app

1. Upload the channel's Slack export: the .zip file, or the channel's daily .json files.
2. Click **Search for sources**. The app reads every link and uploaded document and looks up its title, authors, year, and journal. A few months of messages take a minute or two.
3. Check the results, which are sorted into three groups:
   - **Ready**: metadata was found. These are uploaded. Fix details by double-clicking a cell, or tick rows to **Move to Review** or **Remove**.
   - **Review**: the app couldn't identify these, or they probably aren't sources. They are shown one at a time with the reason. Choose **Mark ready for upload**, **Edit** its details in place, or **Remove** for each. The Table view does the same for several items at once; edits there are kept when you press **Save edits**. Only items marked ready for upload are uploaded.
   - **Already in Zotero**: these are skipped. The table shows what matched (DOI, URL, or title) and links to the existing item.
4. Click **Upload to Zotero**. If Review items aren't marked ready for upload, the app asks first. They stay in Review afterwards, to upload or remove later.

Each uploaded item gets its metadata, the PDF when one could be downloaded, and a note with the Slack post and its replies. Tweets and other web pages are saved as web page items with their link. When both a working paper and its published version are shared, they are kept as separate items, and the working paper's title ends with "(working paper)". Nothing else is added to the library.

The search runs on the server, so refreshing the page or closing the tab doesn't stop it. **Pause** holds it and **Resume** continues; **Stop** ends it and clears its results. Results stay until someone starts a new search or the app restarts, which also happens when its code is updated. If the app restarts before you upload, search the export again; nothing is uploaded twice.

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

## Hosting

The app runs on Streamlit Community Cloud from this repository.

- Main file: `app.py`. Python version: 3.14 (under Advanced settings when deploying).
- Secrets: copy `.streamlit/secrets.toml.example` into the app's Secrets box and fill in the keys. The Zotero key needs write access to the library.
- Access: set the app to private and invite lab members by email.

## Maintenance

Package versions are pinned in `requirements.txt`. Dependabot checks for updates every Sunday morning and merges them on its own once the tests pass. An update that fails the tests stays open as a pull request.

A monthly GitHub check confirms the outside services the app relies on still respond as expected. It emails the repository owner only if something fails.

Python 3.14 stops receiving security updates in October 2030, and Streamlit then moves apps to a newer version. Before then, delete the app and deploy it again on the newest Python version offered, and update `.python-version` and the version in `requirements.in`. The monthly check starts failing six months ahead as a reminder.

If the default Claude model is retired, the app switches to the newest Haiku model on its own.
