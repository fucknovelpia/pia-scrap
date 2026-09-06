# pia-scrap

Current version: **1.3.2**

Novelpia downloader with:
- EPUB export
- TXT export
- desktop UI
- public novel link scraping
- batch download from a `.txt` file
- reusable chapter cache for future updates

Use responsibly. Only access content your account can legitimately read. Respect Novelpia's terms and copyright.

## What This Version Adds

Compared to the original script, this version includes:
- desktop UI with live logs
- automatic Live Log focus and activity animation when a run starts
- Chrome session import
- public listing scraper for `/novels`
- batch mode from `novel_links.txt`
- richer EPUB metadata
- per-novel cache and update-friendly rebuilds
- persistent, deduplicated cover and chapter image downloads
- optional cover-thumbnail downloads in public-list scraping
- safer recovery logic and selectable download profiles

See [ADDED_FEATURES.md](./ADDED_FEATURES.md) for a detailed changelog of the added functionality.

## Requirements

- Python 3.9+
- macOS was the main target during development, but the CLI is standard Python

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick Start

Run a single EPUB download:

```bash
python3 main.py 49 --user you@example.com --pass "your-password"
```

Run the desktop UI:

```bash
python3 main.py --ui
```

Or on macOS:

```bash
./run-pia-ui.command
```

## Authentication Options

This project supports multiple auth flows.

### 1. Email / Password

```bash
python3 main.py 49 --user you@example.com --pass "your-password"
```

### 2. `.env`

Copy `.env.example` to `.env` and fill it in:

```env
NOVELPIA_EMAIL=your_email@example.com
NOVELPIA_PASSWORD=your_password
```

Then run:

```bash
python3 main.py 49
```

### 3. Existing Session

For Google accounts, use **Login with Google** in the desktop app and wait for
the login window to close automatically. The captured session is saved and used
for downloads ahead of saved email/password credentials. Clear the session
fields in the Login tab to switch back to email/password.

Python and the Windows executable use the same authentication flow. Settings
are read beside `main.py` in source mode or beside `PIA-Scrap.exe` in a build.
An explicitly supplied browser session takes priority over password flags;
explicit password flags otherwise override saved sessions. Expired sessions
are refreshed with their cookies and the new token is used for the retry.

You can reuse session values directly:

```bash
python3 main.py 49 \
  --login-at "PASTE_LOGIN_AT_HERE" \
  --userkey "PASTE_USERKEY_HERE" \
  --tkey "PASTE_TKEY_HERE" \
  --save-session
```

### 4. Chrome Session Import

Import Novelpia session data from a local Chrome profile:

```bash
python3 main.py --chrome-profile "Default" 49
```

Chrome import supports Windows, macOS, and Linux profiles. If Chrome's cookie
encryption prevents import, use the app's login window instead. Imported
cookies are refreshed to obtain an access token; `LOGINKEY` is not an access token.

Or do it from the UI with:
- `Import From Chrome`
- `Open Chrome Login`
- `Login In Chrome And Import`

When a chapter requires an advertisement (`0010`), the downloader opens that
chapter in the official viewer using the app's browser profile. Let the real ad
finish; the compact ad window starts minimized on the taskbar and clicks the
normal Continue button as soon as it becomes ready, without waiting for other
page resources to finish loading, then
receives the chapter directly from the viewer's successful server responses,
closes the window, and resumes. Chapter text and titles stay hidden in ad windows
to prevent spoilers. The live log reports the automatic Continue click and each
completed advertisement. Viewer page failures are automatically retried up to
10 times by default, with a 5-second cooldown and without restarting a healthy
ad countdown. Change **Ad retries** and **Retry cooldown (s)** in the Download
tab and click **Save Settings** to keep these values for future runs. Setting
Ad retries to 0 disables automatic viewer retries. The limit counts retries
after the first page attempt. Each retry gets a fresh timeout, allowing longer
cooldowns and all configured retries to run. The temporary preparation page is
removed from browser history, so a failed post-ad navigation cannot return to it
and stall.
Each download worker
can open its own ad window, so **Threads = 4** allows up to four simultaneous ad
windows sharing the same signed-in browser profile. When a worker finishes its
chapter, it starts the next queued chapter while other workers continue their
downloads, advertisements, or recovery attempts. Restore an ad window from
the taskbar if you need to interact with it. If the viewer asks you
to sign in, use the same account as the downloader. Closing an ad before it
finishes reports that chapter as unavailable without repeatedly refreshing
login. Cancelling the download also closes its ad windows.

## CLI Overview

Single novel:

```bash
python3 main.py NOVEL_ID [options]
```

UI:

```bash
python3 main.py --ui
```

Public link scraping:

```bash
python3 main.py --scrape-novel-links --page-start 1 --page-end 63 --links-out output/novel_links.txt
```

Batch download from a links file:

```bash
python3 main.py --novel-links-file output/novel_links.txt --user you@example.com --pass "your-password"
```

Important options:
- `--user`, `--pass`
- `--login-at`, `--userkey`, `--tkey`
- `--chrome-profile`
- `--save-session`
- `--ui`
- `--txt`
- `--no-images`
- `--out`
- `--start`, `--end`
- `--max-chapters`
- `--min-interval`, `--max-interval`
- `--ad-retries`, `--ad-retry-cooldown` (ad viewer retries and cooldown in seconds;
  override the saved Download settings, with defaults of 10 retries and 5 seconds)
- `--throttle` (legacy fixed-delay override)
- `--fetch-profile`
- `--novel-links-file`
- `--batch-limit`
- `--scrape-novel-links`
- `--page-start`, `--page-end`, `--links-out`
- `--scrape-images`, `--scrape-images-dir`

## Download Profiles

Two download strategies are available:

- `safe`
  - conservative
  - sequential
  - stronger cooldown behavior
  - best for stability

- `fast-rotate`
  - closer to original speed
  - more aggressive
  - rotates session on failure
  - better when you want more throughput

Example:

```bash
python3 main.py 49 --user you@example.com --pass "your-password" --fetch-profile fast-rotate
```

## Chapter Range And Request Intervals

Use an inclusive chapter range from the CLI:

```bash
python3 main.py 49 --start 20 --end 40
```

Set **Threads** in the UI or `--threads N` in the CLI to allow up to N chapter
downloads at once. Each free worker starts the next queued chapter independently.
An advertisement or recovery attempt keeps its worker occupied while the other
workers continue.

Each worker waits a fresh random delay before every chapter request, including
its first request. The default range is 0.5 to 2.0 seconds. Set different bounds with:

```bash
python3 main.py 49 --min-interval 1.0 --max-interval 3.0
```

The legacy `--throttle 1.0` option remains available when a fixed delay is
needed. It overrides both interval bounds.

## Public Novel Link Scraper

This version can crawl the public Novelpia listing and export one novel URL per line.

Example:

```bash
python3 main.py --scrape-novel-links --page-start 1 --page-end 63 --links-out output/novel_links.txt
```

Output lines look like:

```text
https://global.novelpia.com/novel/3183
```

Download the cover thumbnails found on those listing pages at the same time:

```bash
python3 main.py --scrape-novel-links --page-start 1 --page-end 63 \
  --links-out output/novel_links.txt --scrape-images
```

The default thumbnail directory is `output/novel_links_images/`. Override it
with `--scrape-images-dir PATH`. Its `images.json` file maps each saved,
content-deduplicated image back to the associated novel URL.

## Batch Download

You can feed that `.txt` file back into the downloader.

Supported input lines:
- `https://global.novelpia.com/novel/3183`
- `3183`

Example:

```bash
python3 main.py \
  --novel-links-file output/novel_links.txt \
  --user you@example.com \
  --pass "your-password" \
  --fetch-profile safe
```

Limit a run:

```bash
python3 main.py --novel-links-file output/novel_links.txt --batch-limit 10
```

## UI Features

The desktop UI includes:
- login tab
- download tab
- scrape tab
- live log tab
- single download
- batch download from a links file
- batch download from URLs or IDs pasted into a dedicated dialog
- inclusive start/end chapter spinboxes
- minimum/maximum random interval spinboxes (defaults: 0.5s and 2.0s)
- mouse-wheel-locked spinboxes to prevent accidental changes while scrolling
- grouped Novel Output, Batch Source, Download Options, and Actions sections
- cancel button
- log files under `output/logs`

## Output Structure

Each novel is written under:

```text
output/<title>/
```

Typical files:
- `<title>.epub`
- `images/<content-hash>.<extension>`
- `images.json`
- `metadata.json`
- `chapters.jsonl`
- `build_state.json`
- `.cache/episodes/<episode_no>.json`

## Cache And Update Behavior

This version keeps reusable local state per novel.

That means:
- already downloaded chapter HTML can be reused
- future builds can avoid re-fetching everything
- if a novel has new chapters, only the missing/new ones need to be fetched when possible

## EPUB Metadata

Generated EPUBs include:
- title
- author
- cover
- description
- tags
- categories / subjects
- publisher when available
- source URL
- `About` page

Inline chapter images are also downloaded and embedded when accessible.

## Image Support

Image downloads are enabled by default for EPUB and TXT builds. The downloader:

- saves the novel cover and inline chapter images under `images/`
- recognizes regular, lazy-loaded, responsive `srcset`, `<picture>`, inline
  background, stylesheet, protocol-relative, relative, and `data:` image sources
- validates the actual payload instead of trusting its URL extension
- supports JPEG, PNG, GIF, WebP, SVG, and AVIF
- uses the authenticated Novelpia session and viewer referrer when fetching assets
- applies Novelpia's per-episode CloudFront signing cookies to protected chapter images
- retries temporary failures, enforces a 25 MiB per-image limit, and reports failures
- deduplicates identical content and reuses it on later builds
- records sources, roles, chapters, sizes, hashes, and failures in `images.json`

If a remote image is genuinely unavailable, EPUB output now inserts an
`[Image unavailable]` placeholder instead of retaining a broken external URL.

EPUB builds embed the downloaded assets for offline reading. TXT files cannot
embed binary data, so they contain an `[Image: ... (images/...)]` marker at the
original location and keep the corresponding file beside the chapters.

Use `--no-images` to skip all cover and chapter image downloads. The desktop UI
exposes the same novel-image and listing-thumbnail controls.

## Notes

- `.env`, `.api.json`, `.venv`, and `output/` should stay out of git
- this clean copy was prepared for sharing and publishing
- see [SHARING.md](./SHARING.md) for packaging notes

## License

Provided as-is. Do not use it to redistribute copyrighted content. Follow Novelpia's rules and the law in your jurisdiction.
