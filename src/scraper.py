from __future__ import annotations

from collections import OrderedDict
import os
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from src.const import BASE_URL
from src.images import ImageManager, choose_image_source


NOVELS_PATH = "/novels"
DEFAULT_QUERY = {
    "flag_complete": "",
    "sort_col": "new_epi_open_dt",
    "flag_cate": "",
    "flag_detail_trans": "",
    "content_type": "2",
    "is_indie_to_premium": "",
}


def build_novels_url(page: int, base_url: Optional[str] = None) -> str:
    root = (base_url or BASE_URL).rstrip("/")
    params = dict(DEFAULT_QUERY)
    params["page"] = str(page)
    return f"{root}{NOVELS_PATH}?{urlencode(params)}"


def scrape_novel_links(
    start_page: int,
    end_page: int,
    out_file: str,
    base_url: Optional[str] = None,
    timeout: int = 30,
    delay: float = 0.0,
    download_images: bool = False,
    image_dir: Optional[str] = None,
) -> List[str]:
    if start_page < 1 or end_page < start_page:
        raise ValueError("Invalid page range.")

    session = requests.Session()
    session.headers.update(
        {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": f"{(base_url or BASE_URL).rstrip('/')}/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
        }
    )

    found = OrderedDict()
    image_manager: Optional[ImageManager] = None
    if download_images:
        if not image_dir:
            out_parent = os.path.dirname(os.path.abspath(out_file))
            out_stem = os.path.splitext(os.path.basename(out_file))[0]
            image_dir = os.path.join(out_parent, f"{out_stem}_images")
        image_manager = ImageManager(session, image_dir, timeout=timeout)

    for page in range(start_page, end_page + 1):
        url = build_novels_url(page, base_url=base_url)
        response = session.get(url, timeout=timeout)
        response.raise_for_status()

        entries = extract_novel_entries(response.text, base_url=base_url)
        for entry in entries:
            link = entry["url"]
            found.setdefault(link, None)
            image_url = entry.get("image_url") or ""
            if image_manager and image_url:
                asset = image_manager.download(
                    image_url,
                    role="listing-cover",
                    context=link,
                    alt_text=entry.get("title"),
                    referer=url,
                    base_url=url,
                )
                if asset:
                    found[link] = asset.relative_path

        if delay and page < end_page:
            import time

            time.sleep(delay)

    out_dir = os.path.dirname(out_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_file, "w", encoding="utf-8") as f:
        for link in found.keys():
            f.write(link + "\n")

    if image_manager:
        image_manager.save_manifest()
        summary = image_manager.summary()
        print(
            "[info] Listing images: "
            f"{summary['image_count']} stored under {image_dir}, "
            f"{summary['image_failures']} failed."
        )
        for failure in image_manager.failure_messages():
            print(f"[warn] Listing image unavailable: {failure}")

    return list(found.keys())


def extract_novel_links(html_text: str, base_url: Optional[str] = None) -> Iterable[str]:
    return [entry["url"] for entry in extract_novel_entries(html_text, base_url=base_url)]


def extract_novel_entries(html_text: str, base_url: Optional[str] = None) -> List[Dict[str, str]]:
    root = (base_url or BASE_URL).rstrip("/")
    soup = BeautifulSoup(html_text, "html.parser")
    found: OrderedDict[str, Dict[str, str]] = OrderedDict()

    for a in soup.select("a[href^='/novel/'], a[href^='https://global.novelpia.com/novel/']"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/novel/"):
            href = root + href
        if "/novel/" not in href:
            continue
        path = href.split("?", 1)[0].rstrip("/")
        entry = found.setdefault(path, {"url": path, "title": "", "image_url": ""})
        title = str(a.get("title") or a.get("aria-label") or a.get_text(" ", strip=True) or "").strip()
        if title and not entry["title"]:
            entry["title"] = title

        image = a.find("img")
        if image is None:
            container = a.parent
            for _ in range(4):
                if container is None:
                    break
                novel_links = container.select(
                    "a[href^='/novel/'], a[href^='https://global.novelpia.com/novel/']"
                )
                normalized_links = set()
                for candidate_link in novel_links:
                    candidate_href = str(candidate_link.get("href") or "").strip()
                    if candidate_href.startswith("/novel/"):
                        candidate_href = root + candidate_href
                    if "/novel/" in candidate_href:
                        normalized_links.add(candidate_href.split("?", 1)[0].rstrip("/"))
                if normalized_links == {path}:
                    image = container.find("img")
                    if image is not None:
                        break
                container = container.parent

        if image is not None and not entry["image_url"]:
            entry["image_url"] = choose_image_source(image, root + "/")
            if not entry["title"]:
                entry["title"] = str(image.get("alt") or "").strip()

    return list(found.values())
