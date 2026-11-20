# AutoUsenetDownloader

A self-hosted web app that regularly searches your Usenet indexers for new posts matching your own keywords and phrases, then sends the matches to NZBGet. It never grabs the same release twice. It tracks the downloads it started, and it can move finished downloads into your library folder.

It runs as two small containers from one image:

| Service  | What it does |
|----------|--------------|
| `web`    | The web UI on port 8080: dashboard, indexers, search profiles, NZBGet settings, downloads, settings and the activity log. |
| `worker` | The background service. It runs scheduled scans, checks NZBGet for status, and moves completed folders. |

All settings and history are stored in one SQLite database in `./config` on the host, so rebuilding or updating the containers keeps your data.

---

## Quick start

Requirements: Docker with the Compose plugin, NZBGet (anywhere on your network), and at least one Newznab indexer API key (NZBGeek, NZBFinder, DrunkenSlug, NZBPlanet and so on).

```bash
cd AutoUsenetDownloader
cp .env.example .env        # then edit it, see below
docker compose up -d --build
```

Open `http://<your-server>:8080`, then:

1. **Indexers → Add indexer.** Enter a name, the API URL (for example `https://api.nzbgeek.info` or `https://nzbfinder.ws`), your API key and a priority. Click **Test connection**, then **Save**.
2. **NZBGet.** Enter the host, port, username, password and whether to use SSL. Click **Test connection**, then **Save**. If NZBGet runs in Docker on the same host, use the host's LAN IP. If it's in the same Compose project, use its service name.
3. **Search Profiles → New profile.** Name it, tick categories, and enter keywords and phrases (one per line). Use **Try a title** to check what will match.
4. **Settings.** Set the polling interval (default 30 minutes) and the search window (default 24 hours). Turn on **Move completed downloads** if you want that (see below).
5. Back on the **Dashboard**, click **Scan now**, or wait for the next scheduled scan.

### Prebuilt image (no build needed)

Every push to `main` on GitHub builds and publishes an image (amd64 and arm64) to:

```
ghcr.io/cerlendson/autousenetdownloader:latest
```

To use it instead of building locally, run `docker compose pull && docker compose up -d`. The Compose file already points at this image. Set `IMAGE=` in `.env` to use a different one.

If the repository is private, the image is private too. Either run `docker login ghcr.io` with a GitHub token that has `read:packages`, or make the package public under GitHub → Packages → autousenetdownloader → Package settings.

In a Docker UI such as Portainer, Unraid or Synology, use the repository/image `ghcr.io/cerlendson/autousenetdownloader:latest`. Create two containers from it:
- **web**: map port 8080 and mount `/config`.
- **worker**: use the command `python -m app.worker`, and mount `/config`, `/nzbget-completed` and `/target`.

Both containers must share the same `/config` folder.

### `.env` settings

| Variable | Meaning |
|----------|---------|
| `TZ` | Time zone for times shown in the UI, for example `America/Chicago`. |
| `PUID` / `PGID` | User and group the app runs as. Use the owner of your media and NZBGet download folders (run `id` on the host to find them). |
| `WEB_PORT` | Host port for the UI (default 8080). |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | Optional login for the UI. Leave both blank to turn it off. |
| `CONFIG_DIR` | Host folder for the database (default `./config`). |
| `NZBGET_COMPLETED_DIR` | Host folder where NZBGet puts completed downloads. It's mounted at `/nzbget-completed` in the worker. |
| `TARGET_DIR` | Host folder that completed downloads are moved into. It's mounted at `/target` in the worker. |

---

## How it works

### Search profiles and matching

Each profile has a name, one or more categories, and a list of keywords and phrases, one per line (commas also work):

| Entry | Type | Matches |
|-------|------|---------|
| `Beatles` | keyword | Any title containing the word: *The Beatles - Abbey Road FLAC*, *Beatles Discography*, *Rare Beatles Collection*. It doesn't match *Beatlesque*. |
| `Pink Floyd` | keyword with several words | Titles containing all of the words, in any order. |
| `"Michael Jackson"` | exact phrase | *Michael Jackson - Thriller* and *Michael Jackson Collection FLAC*, but not *Jackson Family Collection*. |
| `-karaoke`, `-"Greatest Hits"` | exclusion | Rejects any title that contains it. |

A title matches when it matches **any** keyword or phrase and none of the exclusions. Matching ignores case and treats dots, dashes and underscores as spaces, so `The.Beatles-Abbey_Road` matches `Beatles`.

Categories are chosen by name (Audio, Audio > Lossless, Movies > UHD and so on). Choosing a top-level category includes all of its subcategories. Each name maps to the standard Newznab category ID by default. If an indexer uses different IDs, override them on that indexer's edit page. **Fetch categories** shows the IDs the indexer reports.

By default a profile searches every enabled indexer. You can limit it to specific indexers.

### Each scan

For every enabled profile, every allowed indexer (highest priority first), and every keyword or phrase, the worker:

1. Searches the indexer's API with the profile's category IDs and a maximum age.
2. For each result, checks that:
   - the category is one of the profile's categories,
   - the post was published within the search window,
   - the title matches the keywords and phrases.
3. Checks whether the release has already been processed (see below). If it has, the release is skipped.
4. Downloads the NZB, checks again on the NZB's contents, and sends it to NZBGet. It records the NZBGet ID, the status and the queue date.

### Duplicate prevention

A release counts as already processed if any of these match a release in the history:

- **GUID** from the same indexer
- **NZB ID** from the same indexer
- **Release name**, normalized for case and punctuation, from any indexer
- **Indexer-supplied hash** (`nzbhash` or `hash` attributes), when the indexer provides one
- **NZB content hash**, a hash of the NZB's article list. This catches the same post listed on two indexers under different names.

Because indexers are searched in priority order, a release listed on several indexers is taken from the highest-priority one. Failed downloads stay in the history, so they aren't grabbed again automatically. To allow a release again, go to **Downloads** and click **Remove**. If NZBGet refuses an NZB, nothing is recorded, so the next scan retries it.

### NZBGet monitoring

Every 60 seconds by default, the worker asks NZBGet for its queue and history. It only looks up the NZBGet IDs this app queued, so downloads you add to NZBGet yourself are never touched. Each tracked download moves through these statuses:

`queued` → `downloading` → `processing` (verify, repair, unpack) → `completed` or `failed`

### Moving completed downloads

When **Settings → Move completed downloads** is on and NZBGet reports a download as successful, the worker moves the download's folder to the target folder. With **one subfolder per profile** turned on, it goes to `/target/<profile name>/<release folder>`.

The worker must be able to see NZBGet's completed folder:

1. In `.env`, set `NZBGET_COMPLETED_DIR` to the host folder that NZBGet writes completed downloads to.
2. NZBGet reports paths from its own point of view, for example `/downloads/completed/...` inside its container. In Settings, set:
   - **NZBGet path** = the folder as NZBGet sees it, for example `/downloads/completed`
   - **Path in this container** = `/nzbget-completed`

   If NZBGet runs directly on the host, set **NZBGet path** to the host path, for example `/srv/usenet/completed`.
3. Make sure `PUID`/`PGID` can write to both folders.

If a move fails (for example the folder isn't found, or permission is denied), the error appears on the Downloads page and in the activity log. The worker retries the move on every check, and there's also a **Retry move** button.

---

## Updating

Your data lives in `CONFIG_DIR` (`./config`), outside the containers. To update:

```bash
docker compose pull && docker compose up -d     # prebuilt image
# or, building from source:
git pull && docker compose up -d --build
```

Database changes are applied automatically when the app starts. To back up, copy `./config/autousenet.db`, ideally with the containers stopped.

## Useful commands

```bash
docker compose logs -f worker     # watch scans and NZBGet checks
docker compose logs -f web
docker compose restart worker
docker compose down               # stop (your data stays in ./config)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest                                           # unit and end-to-end tests with fake indexer and NZBGet
DATA_DIR=./dev-config uvicorn app.web:app --reload --port 8080
DATA_DIR=./dev-config python -m app.worker       # in a second terminal
```

Code layout:

```
app/
  web.py         web UI routes
  worker.py      background scheduler loop
  searcher.py    scan logic, matching pipeline, duplicate checks, queueing
  monitor.py     NZBGet status tracking and completed-folder moves
  newznab.py     indexer API client
  nzbget.py      NZBGet JSON-RPC client
  matcher.py     keyword, phrase and exclusion parsing and matching
  categories.py  category tree and Newznab IDs
  db.py          SQLite schema, migrations and settings
  templates/     HTML pages
tests/           pytest suite with fake Newznab and NZBGet servers
```

## Notes

- API keys and the NZBGet password are stored in the SQLite database in `./config`. Keep that folder private. The UI never shows saved secrets in full.
- If the UI is reachable from outside your network, set `AUTH_USERNAME`/`AUTH_PASSWORD` and put it behind a reverse proxy with HTTPS.
- Each scan runs one search per keyword, per profile, per indexer. Keep an eye on your indexers' daily API limits when you add many keywords or shorten the interval.
