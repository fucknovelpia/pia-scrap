from __future__ import annotations

from collections import OrderedDict
import os
from typing import Iterable, List, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from src.const import BASE_URL


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
    for page in range(start_page, end_page + 1):
        url = build_novels_url(page, base_url=base_url)
        response = session.get(url, timeout=timeout)
        response.raise_for_status()

        for link in extract_novel_links(response.text, base_url=base_url):
            found.setdefault(link, None)

        if delay and page < end_page:
            import time

            time.sleep(delay)

    out_dir = os.path.dirname(out_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_file, "w", encoding="utf-8") as f:
        for link in found.keys():
            f.write(link + "\n")

    return list(found.keys())


def extract_novel_links(html_text: str, base_url: Optional[str] = None) -> Iterable[str]:
    root = (base_url or BASE_URL).rstrip("/")
    soup = BeautifulSoup(html_text, "html.parser")
    found = OrderedDict()

    for a in soup.select("a[href^='/novel/'], a[href^='https://global.novelpia.com/novel/']"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("/novel/"):
            href = root + href
        if "/novel/" not in href:
            continue
        path = href.split("?", 1)[0].rstrip("/")
        found.setdefault(path, None)

    return list(found.keys())
