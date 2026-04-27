import argparse
import os
import re
import sys
import warnings
warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

from dotenv import load_dotenv
from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.chrome_session import load_chrome_novelpia_session
from src.helper import load_config, save_config
from src.scraper import scrape_novel_links
from src.ui import launch_ui
from src import const

# ----------------------------
# Main Function
# ----------------------------

NOVEL_LINK_RE = re.compile(r"/novel/(\d+)")


def extract_novel_ids_from_file(path: str) -> list[int]:
    novel_ids: list[int] = []
    seen: set[int] = set()
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            match = NOVEL_LINK_RE.search(line)
            if match:
                novel_id = int(match.group(1))
            elif line.isdigit():
                novel_id = int(line)
            else:
                continue
            if novel_id in seen:
                continue
            seen.add(novel_id)
            novel_ids.append(novel_id)
    return novel_ids


def run_single_build(client, args, novel_id: int):
    if args.txt:
        return build_txt(
            client, novel_id, args.out,
            start_chapter=args.start_chapter,
            end_chapter=args.end_chapter,
            max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
            language=args.lang, debug_dump=args.debug,
        )
    return build_epub(
        client, novel_id, args.out,
        start_chapter=args.start_chapter,
        end_chapter=args.end_chapter,
        max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
        language=args.lang, debug_dump=args.debug
    )


def rotate_session_for_retry(client) -> None:
    refreshed = False
    try:
        if client.tokens.login_at:
            print("[batch] Trying session refresh before retry...")
            client.refresh()
            refreshed = True
    except Exception as e:
        print(f"[batch] Session refresh failed: {e}")

    if client.email and client.password:
        try:
            print("[batch] Trying full re-login before retry...")
            client.login()
            refreshed = True
        except Exception as e:
            print(f"[batch] Full re-login failed: {e}")

    if not refreshed:
        print("[batch] No session rotation step succeeded.")


def run_single_build_with_recovery(client, args, novel_id: int, attempts: int = 2):
    last_error = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(f"[batch] Retrying novel {novel_id} after session rotation ({attempt}/{attempts})...")
        try:
            return run_single_build(client, args, novel_id)
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                break
            print(f"[batch] Novel {novel_id} failed on attempt {attempt}/{attempts}: {e}")
            rotate_session_for_retry(client)
    assert last_error is not None
    raise last_error

def parse_novel_id(value: str) -> int:
    """Accept a novel ID or a Novelpia URL and return the numeric ID."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    m = NOVEL_LINK_RE.search(value)
    if m:
        return int(m.group(1))
    # Try to find any trailing number in the URL (e.g. /viewer/586921)
    m = re.search(r"/(\d+)(?:[/?#]|$)", value)
    if m:
        return int(m.group(1))
    raise argparse.ArgumentTypeError(f"Cannot extract novel ID from: {value}")

def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Novelpia → EPUB packer (API)")
    ap.add_argument("novel_id", type=parse_novel_id, nargs="?", help="Novel ID or URL (e.g., 1072 or https://global.novelpia.com/novel/1072)")
    ap.add_argument("--ui", action="store_true", help="Launch the desktop UI")
    ap.add_argument("--user", "--email", "-u", "-e", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("--pass", "--password", "-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("--login-at", dest="login_at", help="Existing Novelpia session token from your browser/app session")
    ap.add_argument("--userkey", dest="userkey", help="Existing USERKEY cookie from your browser/app session")
    ap.add_argument("--tkey", dest="tkey", help="Existing TKEY cookie from your browser/app session")
    ap.add_argument("--chrome-profile", dest="chrome_profile", help="Import Novelpia cookies from a Google Chrome profile, e.g. 'Default' or 'Profile 2'")
    ap.add_argument("--save-session", action="store_true", help="Persist provided session tokens/cookies to .api.json")
    ap.add_argument("--out", default="output", help="Output directory")
    ap.add_argument("--max-chapters", "-max", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("--start", "--start-chapter", dest="start_chapter", type=int, default=None, help="Start fetching from this chapter number")
    ap.add_argument("--end", "--end-chapter", dest="end_chapter", type=int, default=None, help="Stop fetching at this chapter number")
    ap.add_argument("--lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("--proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("--debug", "-v", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics")
    ap.add_argument("--throttle", type=float, default=0.5, help="Seconds delay between episode requests (default: 0.5)")
    ap.add_argument("--threads", type=int, default=1, help="Number of concurrent download threads (default: 1)")
    ap.add_argument("--txt", "-txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    ap.add_argument("--novel-links-file", help="Read novel links/IDs from a text file and download them one by one")
    ap.add_argument("--batch-limit", type=int, default=0, help="Process at most N novels from --novel-links-file (0 = all)")
    ap.add_argument("--scrape-novel-links", action="store_true", help="Scrape novel links from the public novel list pages")
    ap.add_argument("--page-start", type=int, default=1, help="Start page for --scrape-novel-links (default: 1)")
    ap.add_argument("--page-end", type=int, default=63, help="End page for --scrape-novel-links (default: 63)")
    ap.add_argument("--links-out", default="output/novel_links.txt", help="Output file for --scrape-novel-links")
    args = ap.parse_args()

    const.HTTP_LOG = bool(args.debug)

    # Default to UI when no arguments given (e.g. double-clicking the .exe)
    if args.ui or (not args.novel_id and not args.scrape_novel_links and not args.novel_links_file):
        launch_ui()
        return

    if args.scrape_novel_links:
        try:
            links = scrape_novel_links(
                start_page=args.page_start,
                end_page=args.page_end,
                out_file=args.links_out,
            )
            print(
                f"[success] Wrote {len(links)} novel links to: {args.links_out}"
            )
            return
        except Exception as e:
            print(f"[error] Failed to scrape novel links: {e}")
            sys.exit(1)

    if args.novel_id is None and not args.novel_links_file:
        ap.error("novel_id is required unless you use --scrape-novel-links or --novel-links-file")

    cfg = load_config()
    cfg_login_at = (cfg.get("login_at") or "").strip() or None
    cfg_userkey = (cfg.get("userkey") or "").strip() or None
    cfg_tkey = (cfg.get("tkey") or "").strip() or None

    # Priority: CLI > .env > config tokens > error
    email = args.email or os.getenv("NOVELPIA_EMAIL")
    password = args.password or os.getenv("NOVELPIA_PASSWORD")
    chrome_session = None
    if args.chrome_profile:
        try:
            chrome_session = load_chrome_novelpia_session(args.chrome_profile)
        except Exception as e:
            print(f"[error] Failed to import Chrome session: {e}")
            sys.exit(1)

    session_login_at = (
        args.login_at
        or os.getenv("NOVELPIA_LOGIN_AT")
        or (chrome_session.login_at if chrome_session else None)
        or cfg_login_at
    )
    session_userkey = (
        args.userkey
        or os.getenv("NOVELPIA_USERKEY")
        or (chrome_session.userkey if chrome_session else None)
        or cfg_userkey
    )
    session_tkey = (
        args.tkey
        or os.getenv("NOVELPIA_TKEY")
        or (chrome_session.tkey if chrome_session else None)
        or cfg_tkey
    )

    if email and password:
        client = NovelpiaClient(
            email=email,
            password=password,
            proxy=args.proxy,
            throttle=args.throttle,
            userkey=session_userkey,
            tkey=session_tkey,
            threads=args.threads,
        )
        client.login()
        # Persist/refresh tokens after successful login
        userkey_val = None
        tkey_val = None
        try:
            for c in client.s.cookies:
                if c.name == "USERKEY":
                    userkey_val = c.value
                elif c.name == "TKEY":
                    tkey_val = c.value
        except Exception as e:
            print(f"Error occurred while fetching cookies: {e}")
            pass
        save_config({
            "login_at": client.tokens.login_at,
            "userkey": userkey_val or session_userkey or "",
            "tkey": tkey_val or client.tokens.tkey or session_tkey or "",
        })
    elif session_login_at and session_userkey:
        client = NovelpiaClient(
            email=None,
            password=None,
            proxy=args.proxy,
            throttle=args.throttle,
            userkey=session_userkey,
            tkey=session_tkey,
            threads=args.threads,
        )
        client.tokens.login_at = session_login_at
        if args.save_session and (args.login_at or args.userkey or args.tkey or args.chrome_profile):
            save_config({
                "login_at": session_login_at or "",
                "userkey": session_userkey or "",
                "tkey": session_tkey or "",
            })
    else:
        print("[info] No credentials found. Running without login (free chapters only).")
        client = NovelpiaClient(
            email=None,
            password=None,
            proxy=args.proxy,
            throttle=args.throttle,
            threads=args.threads,
        )

    if args.novel_links_file:
        try:
            novel_ids = extract_novel_ids_from_file(args.novel_links_file)
        except Exception as e:
            print(f"[error] Failed to read novel links file: {e}")
            sys.exit(1)

        if not novel_ids:
            print("[error] No valid novel links or IDs were found in the provided file.")
            sys.exit(1)

        if args.batch_limit and args.batch_limit > 0:
            novel_ids = novel_ids[:args.batch_limit]

        success = 0
        failed = 0
        print(f"[info] Loaded {len(novel_ids)} novel IDs from {args.novel_links_file}")
        for idx, novel_id in enumerate(novel_ids, 1):
            print(f"\n[batch] Starting {idx}/{len(novel_ids)}: novel {novel_id}")
            try:
                out_path, title, count = run_single_build_with_recovery(client, args, novel_id)
                label = "TXT files under" if args.txt else "EPUB"
                print(f"[batch] Finished {idx}/{len(novel_ids)}: {title} | chapters={count} | {label}: {out_path}")
                success += 1
            except Exception as e:
                print(f"[batch] Failed {idx}/{len(novel_ids)}: novel {novel_id} | {e}")
                failed += 1

        print(f"\n[success] Batch finished. Successful novels: {success} | Failed novels: {failed}")
        if failed:
            sys.exit(1)
        return

    try:
        out_path, title, count = run_single_build(client, args, args.novel_id)
        if args.txt:
            print(f"\n[success] Wrote TXT files under: {out_path}")
        else:
            print(f"\n[success] Wrote EPUB: {out_path}")
    except Exception as e:
        print(f"[error] Failed to build novel: {e}")
        sys.exit(1)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
