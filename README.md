# pia-scrap

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
- Chrome session import
- public listing scraper for `/novels`
- batch mode from `novel_links.txt`
- richer EPUB metadata
- per-novel cache and update-friendly rebuilds
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

Or do it from the UI with:
- `Import From Chrome`
- `Open Chrome Login`
- `Login In Chrome And Import`

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
- `--out`
- `--start`, `--end`
- `--max-chapters`
- `--fetch-profile`
- `--novel-links-file`
- `--batch-limit`
- `--scrape-novel-links`
- `--page-start`, `--page-end`, `--links-out`

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
- cancel button
- log files under `output/logs`

## Output Structure

Each novel is written under:

```text
output/<title>/
```

Typical files:
- `<title>.epub`
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

## Notes

- `.env`, `.api.json`, `.venv`, and `output/` should stay out of git
- this clean copy was prepared for sharing and publishing
- see [SHARING.md](./SHARING.md) for packaging notes

## License

Provided as-is. Do not use it to redistribute copyrighted content. Follow Novelpia's rules and the law in your jurisdiction.
