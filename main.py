import argparse
import os
import re
import sys
import warnings
warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

from dotenv import dotenv_values
from src.api import AuthenticationError, NovelpiaClient
from src.ad_navigation import (
    DEFAULT_AD_RETRIES, DEFAULT_AD_RETRY_COOLDOWN, validate_ad_retry_settings,
)
from src.builder import build_epub, build_txt
from src.chrome_session import load_chrome_novelpia_session
from src.helper import load_config, save_config
from src.scraper import scrape_novel_links
from src.ui import launch_ui
from src import __version__, const

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
            download_images=not args.no_images,
        )
    return build_epub(
        client, novel_id, args.out,
        start_chapter=args.start_chapter,
        end_chapter=args.end_chapter,
        max_chapters=(args.max_chapters if args.max_chapters and args.max_chapters > 0 else None),
        language=args.lang, debug_dump=args.debug,
        download_images=not args.no_images,
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

def resolve_ad_retry_settings(args, cfg):
    retries = getattr(args, "ad_retries", None)
    cooldown = getattr(args, "ad_retry_cooldown", None)
    return validate_ad_retry_settings(
        cfg.get("ad_retries", DEFAULT_AD_RETRIES) if retries is None else retries,
        cfg.get("ad_retry_cooldown", DEFAULT_AD_RETRY_COOLDOWN) if cooldown is None else cooldown,
    )


def create_authenticated_client(args, cfg):
    """Select one auth source consistently in Python and the frozen desktop app."""
    ad_retries, ad_retry_cooldown = resolve_ad_retry_settings(args, cfg)
    # Read the file next to the app on every run. load_dotenv() searches from
    # bundled source paths and caches old credentials in the frozen process.
    environment = dict(dotenv_values(const.APP_DIR / ".env"))
    environment.update(os.environ)
    explicit_session = any((args.login_at, args.userkey, args.tkey, args.chrome_profile))
    explicit_credentials = bool(args.email or args.password)
    if args.chrome_profile:
        try:
            imported = load_chrome_novelpia_session(args.chrome_profile)
        except Exception as exc:
            raise AuthenticationError(f"Failed to import Chrome session: {exc}") from exc
        session = {"login_at": imported.login_at, "userkey": imported.userkey, "tkey": imported.tkey}
        if not any(session.values()):
            raise AuthenticationError("No Novelpia session was found in this Chrome profile. Sign in first.")
    elif explicit_session:
        session = {}
    elif explicit_credentials:
        session = {}
    elif any(environment.get(key) for key in ("NOVELPIA_LOGIN_AT", "NOVELPIA_USERKEY", "NOVELPIA_TKEY")):
        session = {key: environment.get("NOVELPIA_" + key.upper()) for key in ("login_at", "userkey", "tkey")}
    else:
        session = {key: cfg.get(key) for key in ("login_at", "userkey", "tkey")}
    if explicit_session:
        for key in ("login_at", "userkey", "tkey"):
            if getattr(args, key):
                session[key] = getattr(args, key)
    session = {key: str(value).strip() if value else None for key, value in session.items()}
    use_session = any(session.values())
    # Credentials from a different provider must never replace a browser login.
    email = None if use_session else (args.email or environment.get("NOVELPIA_EMAIL"))
    password = None if use_session else (args.password or environment.get("NOVELPIA_PASSWORD"))
    if not use_session and bool(email) != bool(password):
        raise AuthenticationError("Email/password login requires both an email and a password.")
    client = NovelpiaClient(
        email=email, password=password, proxy=args.proxy, throttle=args.throttle,
        min_interval=args.min_interval, max_interval=args.max_interval,
        userkey=session.get("userkey"), tkey=session.get("tkey"), threads=args.threads,
        ad_retries=ad_retries, ad_retry_cooldown=ad_retry_cooldown,
    )
    if use_session:
        print("[auth] Checking browser session...")
        client.tokens.login_at = session.get("login_at")
        if not client.tokens.login_at:
            client.refresh()
        client.me()
        print("[auth] Browser session recognized.")
    elif email and password:
        print("[auth] Signing in with email/password...")
        client.login()
        print("[auth] Email/password login successful.")
    else:
        print("[info] No credentials found. Running without login (free chapters only).")
    if (email and password) or (use_session and args.save_session):
        save_config({
            "login_at": client.tokens.login_at or "",
            "userkey": client.tokens.userkey or "",
            "tkey": client.tokens.tkey or "",
        })
    return client


def main():
    ap = argparse.ArgumentParser(description="Novelpia to EPUB packer (API)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("novel_id", type=parse_novel_id, nargs="?", help="Novel ID or URL (e.g., 1072 or https://global.novelpia.com/novel/1072)")
    ap.add_argument("--ui", action="store_true", help="Launch the desktop UI")
    ap.add_argument("--user", "--email", "-u", "-e", dest="email", help="Novelpia email (used when no explicit browser session is supplied)")
    ap.add_argument("--pass", "--password", "-p", dest="password", help="Novelpia password (used when no explicit browser session is supplied)")
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
    ap.add_argument("--min-interval", type=float, default=0.5, help="Minimum random delay between episode requests (default: 0.5s)")
    ap.add_argument("--max-interval", type=float, default=2.0, help="Maximum random delay between episode requests (default: 2.0s)")
    ap.add_argument("--throttle", type=float, default=None, help="Legacy fixed delay; overrides --min-interval and --max-interval")
    ap.add_argument("--threads", type=int, default=1, help="Number of concurrent download threads (default: 1)")
    ap.add_argument("--ad-retries", type=int, default=None,
                    help="Automatic ad page retries (0 disables; saved setting or default: 10)")
    ap.add_argument("--ad-retry-cooldown", type=float, default=None,
                    help="Seconds before retrying a failed ad page (saved setting or default: 5)")
    ap.add_argument("--txt", "-txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    ap.add_argument("--no-images", action="store_true", help="Skip cover and chapter image downloads")
    ap.add_argument("--novel-links-file", help="Read novel links/IDs from a text file and download them one by one")
    ap.add_argument("--batch-limit", type=int, default=0, help="Process at most N novels from --novel-links-file (0 = all)")
    ap.add_argument("--scrape-novel-links", action="store_true", help="Scrape novel links from the public novel list pages")
    ap.add_argument("--page-start", type=int, default=1, help="Start page for --scrape-novel-links (default: 1)")
    ap.add_argument("--page-end", type=int, default=63, help="End page for --scrape-novel-links (default: 63)")
    ap.add_argument("--links-out", default="output/novel_links.txt", help="Output file for --scrape-novel-links")
    ap.add_argument("--scrape-images", action="store_true", help="Download cover thumbnails found while scraping public novel lists")
    ap.add_argument("--scrape-images-dir", help="Directory for listing images (default: <links-out name>_images)")
    args = ap.parse_args()

    if args.start_chapter is not None and args.start_chapter < 1:
        ap.error("--start must be at least 1")
    if args.end_chapter is not None and args.end_chapter < 1:
        ap.error("--end must be at least 1")
    if (
        args.start_chapter is not None
        and args.end_chapter is not None
        and args.start_chapter > args.end_chapter
    ):
        ap.error("--start cannot be greater than --end")
    if args.throttle is not None and args.throttle < 0:
        ap.error("--throttle cannot be negative")
    if args.min_interval < 0 or args.max_interval < 0:
        ap.error("request intervals cannot be negative")
    if args.min_interval > args.max_interval:
        ap.error("--min-interval cannot be greater than --max-interval")
    try:
        # Reject invalid explicit options even when launching the UI or scraper.
        resolve_ad_retry_settings(args, {})
    except (TypeError, ValueError) as exc:
        ap.error(str(exc))

    const.HTTP_LOG = bool(args.debug)

    # Default to UI when no arguments given (e.g. double-clicking the .exe)
    if args.ui or (not args.novel_id and not args.scrape_novel_links and not args.novel_links_file):
        launch_ui()
        return

    cfg = load_config()
    try:
        args.ad_retries, args.ad_retry_cooldown = resolve_ad_retry_settings(args, cfg)
    except (TypeError, ValueError) as exc:
        ap.error(str(exc))

    if args.scrape_novel_links:
        try:
            links = scrape_novel_links(
                start_page=args.page_start,
                end_page=args.page_end,
                out_file=args.links_out,
                download_images=args.scrape_images,
                image_dir=args.scrape_images_dir,
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

    try:
        client = create_authenticated_client(args, cfg)
    except AuthenticationError as exc:
        print(f"[error] {exc}")
        sys.exit(1)

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
