# Added Features Over The Original Version

This document summarizes the main additions made on top of the original `pia-scrap` behavior.

## 1. Public Novel Link Scraper

Added a mode to crawl the public Novelpia novel listing pages and export one novel URL per line.

What it adds:
- scrapes pages like `/novels?page=1 ...`
- collects links in the format `https://global.novelpia.com/novel/<id>`
- deduplicates results
- saves them to a text file

CLI:
- `--scrape-novel-links`
- `--page-start`
- `--page-end`
- `--links-out`

## 2. Batch Download From A Links File

Added a batch mode that reads a `.txt` file containing novel links or raw IDs and downloads them one by one.

What it adds:
- accepts lines such as `https://global.novelpia.com/novel/3183`
- also accepts plain numeric IDs
- deduplicates IDs before processing
- supports optional limit with `--batch-limit`
- works with both EPUB and TXT output

CLI:
- `--novel-links-file`
- `--batch-limit`

## 3. Desktop UI

Added a Tkinter desktop interface to avoid relying only on CLI usage.

What it adds:
- tabbed UI for Login / Download / Scrape / Live Log
- run single download from the UI
- run batch download from the UI
- run public link scraping from the UI
- visible status bar
- live log panel
- cancel button for running jobs
- launcher script with `run-pia-ui.command`

CLI:
- `--ui`

## 4. Email/Password And Session-Based Auth

Extended auth handling beyond the original simple login flow.

What it adds:
- standard email/password login
- reuse of existing session values:
  - `login-at`
  - `USERKEY`
  - `TKEY`
- optional persistence of session values in `.api.json`
- optional `.env` credential loading

CLI:
- `--user` / `--pass`
- `--login-at`
- `--userkey`
- `--tkey`
- `--save-session`

## 5. Chrome Session Import

Added the ability to import Novelpia session data from a local Chrome profile.

What it adds:
- reads Novelpia cookies from Chrome
- imports available session-related values into the UI/CLI flow
- can open Chrome directly to the Novelpia login page
- supports a guided “login in Chrome and import” workflow

CLI:
- `--chrome-profile`

UI:
- `Import From Chrome`
- `Open Chrome Login`
- `Login In Chrome And Import`

## 6. Improved Download Profiles

Added configurable download strategies instead of a single fixed behavior.

What it adds:
- `safe` profile:
  - conservative
  - sequential
  - cooldown-oriented
- `fast-rotate` profile:
  - closer to original speed
  - more aggressive
  - rotates session on failures

CLI:
- `--fetch-profile`

## 7. Recovery And Session Rotation

Added more resilience when Novelpia starts returning failures.

What it adds:
- session refresh attempts
- full re-login attempts when credentials exist
- chapter-level recovery for failed downloads
- novel-level retry in batch mode
- better handling of long-running batch sessions

## 8. Clearer Progress Logging

Reworked progress output to make long downloads easier to understand.

What it adds:
- live progress per chapter attempt
- explicit `ok` / `failed` states
- detailed build messages for:
  - metadata
  - cover embedding
  - chapter processing
  - embedded images
  - partial EPUB warnings
- UI logs saved to files under `output/logs`

## 9. Better EPUB Metadata

Expanded the metadata included in generated EPUBs.

What it adds:
- description
- tags
- categories / subjects
- publisher when available
- richer `About` page inside the EPUB

Result:
- prettier EPUB presentation
- better metadata in compatible ebook readers

## 10. Partial Download Safety

Improved behavior when some chapters fail.

What it adds:
- real successful chapter count instead of only queued chapter count
- partial build warnings
- avoids silently pretending a full download succeeded when it did not
- fails cleanly when zero chapters were actually fetched

## 11. Persistent Per-Novel Cache And Update-Friendly State

Added reusable local state for future updates.

What it adds:
- per-novel `build_state.json`
- cached episode HTML files under `.cache/episodes/`
- remembers downloaded episode IDs and links
- reuses cached chapters on future builds
- only fetches uncached/new chapters when possible
- helps rebuild updated EPUBs without redownloading everything

Files involved:
- `build_state.json`
- `metadata.json`
- `chapters.jsonl`
- `.cache/episodes/<episode_no>.json`

## 12. TXT Export Improvements

The TXT export path was also upgraded instead of remaining separate and basic.

What it adds:
- shared recovery logic
- shared cache reuse
- shared metadata generation
- per-chapter TXT output with clearer progress messages

## 13. Shareable Project Cleanup Support

This clean copy also includes packaging improvements for sharing.

What it adds:
- relative-path launcher script
- no bundled user credentials or session files
- no output artifacts
- helper note in `SHARING.md`

## Summary

Compared to the original version, this cleaned project now supports:
- scraping public novel lists
- batch downloading from saved links
- desktop UI usage
- Chrome-assisted session import
- richer auth/session workflows
- safer and smarter recovery behavior
- cleaner logs
- richer EPUB metadata
- reusable local cache for updates
- easier sharing as a public repository
