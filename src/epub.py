import html
import os
import time

from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from src.api import NovelpiaClient
from src.const import BASE_URL
from src.helper import ensure_dir, kebab, media_type_from_ext, normalize_url

# ----------------------------
# EPUB Builder
# ----------------------------

class EpubBuilder:
    def __init__(self, out_dir: str, debug_dump: bool = False):
        self.out_dir = out_dir
        self.debug_dump = debug_dump
        ensure_dir(out_dir)

    def _fetch_bytes(self, client: NovelpiaClient, url: str) -> Optional[bytes]:
        for attempt in range(1, 4):
            try:
                resp = client.s.get(url, timeout=client.timeout)
                if resp.status_code == 429:
                    wait = 2.0 * attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.content
            except Exception:
                if attempt < 3:
                    time.sleep(1.0)
                continue
        return None

    def build(self, client: NovelpiaClient, novel: Dict, episodes: List[Dict],
              filename_hint: Optional[str] = None, language: str = "en",
              author_fallback: str = "Unknown", css_text: Optional[str] = None,
              novel_id: Optional[int] = None, book_dir: Optional[str] = None) -> Tuple[str, str, int]:
        nv = novel["result"]["novel"]
        result = novel.get("result") or {}
        title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
        writers = result.get("writer_list") or []
        author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else author_fallback)
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()
        chapter_total = result.get("info", {}).get("epi_cnt") or nv.get("count_epi") or len(episodes)

        def pick_strings(items: object, *keys: str) -> List[str]:
            out: List[str] = []
            if not isinstance(items, list):
                return out
            for item in items:
                if isinstance(item, str):
                    value = item.strip()
                    if value:
                        out.append(value)
                    continue
                if not isinstance(item, dict):
                    continue
                for key in keys:
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        out.append(raw.strip())
                        break
            return out

        def dedupe(values: List[str]) -> List[str]:
            seen = set()
            unique: List[str] = []
            for value in values:
                if value in seen:
                    continue
                seen.add(value)
                unique.append(value)
            return unique

        tags = dedupe(
            pick_strings(result.get("tag_list"), "tag_name", "name", "title")
            + pick_strings(nv.get("tag_list"), "tag_name", "name", "title")
        )
        categories = dedupe(
            pick_strings(result.get("cate_list"), "cate_name", "name", "title")
            + pick_strings(nv.get("cate_list"), "cate_name", "name", "title")
            + pick_strings(result.get("genre_list"), "genre_name", "name", "title")
            + pick_strings(nv.get("genre_list"), "genre_name", "name", "title")
        )
        all_subjects = dedupe(categories + tags)
        intro = (nv.get("intro") or nv.get("novel_intro") or "").strip()
        publisher = (
            nv.get("cp_name")
            or nv.get("publisher_name")
            or result.get("cp_info", {}).get("cp_name")
            or ""
        ).strip()

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)
        if description:
            book.add_metadata("DC", "description", description)
        if publisher:
            book.add_metadata("DC", "publisher", publisher)
        for subject in all_subjects:
            book.add_metadata("DC", "subject", subject)

        # Cover
        cover_url = normalize_url(nv.get("novel_full_img") or nv.get("novel_img") or "")
        if cover_url:
            print("[info] Downloading cover image...")
        cover_bytes = self._fetch_bytes(client, cover_url) if cover_url else None
        has_cover = False
        if cover_bytes:
            book.set_cover("cover.jpg", cover_bytes)
            has_cover = True
            print("[info] Cover embedded.")
        elif cover_url:
            print("[warn] Cover could not be downloaded.")

        # CSS
        default_css = css_text or (
            """
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial; line-height: 1.6; }
            h1, h2, h3 { page-break-after: avoid; }
            img { max-width: 100%; height: auto; }
            .epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
            """
        )
        style = epub.EpubItem(uid="style", file_name="style/main.css",
                              media_type="text/css", content=default_css.encode("utf-8"))
        book.add_item(style)

        spine: List = ["nav"]
        toc: List = []
        image_cache: Dict[str, str] = {}
        img_index = 1

        def add_images_and_rewrite(html_str: str) -> Tuple[str, List[epub.EpubItem]]:
            nonlocal img_index
            soup = BeautifulSoup(html_str, "html.parser")
            added_items: List[epub.EpubItem] = []

            for img in soup.find_all("img"):
                src = img.get("src")
                if not src:
                    continue
                src = normalize_url(src)
                if src in image_cache:
                    img["src"] = image_cache[src]
                    continue

                path = urlparse(src).path
                ext = os.path.splitext(path)[1].lower() or ".jpg"
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    ext = ".jpg"

                img_bytes = self._fetch_bytes(client, src)
                if not img_bytes:
                    # leave external
                    continue

                fname = f"images/img_{img_index:05d}{ext}"
                image_cache[src] = fname
                img_index += 1

                item = epub.EpubItem(uid=f"img{img_index}", file_name=fname,
                                     media_type=media_type_from_ext(ext), content=img_bytes)
                added_items.append(item)
                img["src"] = fname

            return str(soup), added_items

        cached_results: Dict[int, Dict] = {}
        episodes_to_fetch: List[Dict] = []
        if book_dir:
            from src.builder import load_cached_episode
            for ep in episodes:
                episode_no = int(ep.get("episode_no"))
                cached = load_cached_episode(book_dir, episode_no)
                if cached:
                    cached_results[episode_no] = {
                        "html": cached.get("html") or "",
                        "epi_title": cached.get("epi_title") or ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                        "epi_no": episode_no,
                    }
                else:
                    episodes_to_fetch.append(ep)
        else:
            episodes_to_fetch = list(episodes)

        if cached_results:
            print(f"[info] Reusing {len(cached_results)} cached chapters from previous runs.")
        if episodes_to_fetch:
            print(f"[info] Fetching {len(episodes_to_fetch)} uncached/new chapters from Novelpia.")

        fetched_count = 0

        def update_pbar(idx, ok):
            nonlocal fetched_count
            fetched_count += 1
            state = "ok" if ok else "failed"
            print(f"[progress] Chapter attempt {fetched_count}/{len(episodes_to_fetch)} ({state})")

        fetched_results = []
        if episodes_to_fetch:
            fetched_results = client.fetch_episodes_parallel(episodes_to_fetch, progress_cb=update_pbar)

        fetched_map: Dict[int, Dict] = {}
        if book_dir:
            from src.builder import save_cached_episode
        for ep, res in zip(episodes_to_fetch, fetched_results):
            episode_no = int(ep.get("episode_no"))
            fetched_map[episode_no] = res
            if book_dir and res and "error" not in res:
                save_cached_episode(book_dir, ep, res)

        success_count = 0
        failed_count = 0
        successful_episode_nos: List[int] = []

        for i, ep in enumerate(episodes, 1):
            episode_no = int(ep.get("episode_no"))
            res = fetched_map.get(episode_no) or cached_results.get(episode_no)
            if not res or "error" in res:
                err = res.get("error") if res else "Unknown error"
                print(f"[warn] Failed to fetch chapter {i}: {err}")
                failed_count += 1
                continue

            html_text = res["html"]
            epi_title = res["epi_title"]
            
            html_text, new_imgs = add_images_and_rewrite(html_text)
            print(f"[info] Processed chapter {i}/{len(episodes)}: {epi_title} | embedded images: {len(new_imgs)}")

            chapter = epub.EpubHtml(
                title=epi_title,
                file_name=f"chap_{i:04d}.xhtml",
                lang=language,
                content=(
                    f"<html xmlns=\"http://www.w3.org/1999/xhtml\">"
                    f"<head><title>{html.escape(epi_title)}</title>"
                    f"<link rel=\"stylesheet\" href=\"style/main.css\"/></head>"
                    f"<body><h2 class=\"epi-title\">{html.escape(epi_title)}</h2>{html_text}</body></html>"
                ),
            )

            book.add_item(chapter)
            spine.append(chapter)
            toc.append(chapter)

            for item in new_imgs:
                book.add_item(item)
            success_count += 1
            successful_episode_nos.append(episode_no)

        if success_count == 0:
            raise RuntimeError("No chapters could be fetched. The session may be invalid or Novelpia is rejecting requests.")

        if failed_count:
            print(f"[warn] EPUB will be partial: {success_count} chapters fetched, {failed_count} failed.")

        # About / metadata page
        src_url = f"{BASE_URL}/novel/{novel_id}" if novel_id else ""
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append("<p><img src='cover.jpg' alt='Cover' style='width:230px;max-width:90%;height:auto;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>")
        meta_parts.append(f"<p><strong>Author:</strong> {html.escape(author)}</p>")
        if publisher:
            meta_parts.append(f"<p><strong>Publisher:</strong> {html.escape(publisher)}</p>")
        meta_parts.append(f"<p><strong>Total Chapters:</strong> {html.escape(str(chapter_total))}</p>")
        meta_parts.append(f"<p><strong>Chapters Downloaded:</strong> {success_count}</p>")
        if failed_count:
            meta_parts.append(f"<p><strong>Chapters Failed:</strong> {failed_count}</p>")
        meta_parts.append(f"<p><strong>Status:</strong> {html.escape(status)}</p>")
        if categories:
            meta_parts.append(
                f"<p><strong>Categories:</strong> {html.escape(', '.join(categories))}</p>"
            )
        if tags:
            meta_parts.append(
                f"<p><strong>Tags:</strong> {html.escape(', '.join(tags))}</p>"
            )
        if src_url:
            meta_parts.append(f"<p><strong>Source:</strong> <a href='{src_url}'>{src_url}</a></p>")
        if description:
            meta_parts.append("<h2>Description</h2>")
            meta_parts.append(f"<p>{html.escape(description)}</p>")
        if intro and intro != description:
            meta_parts.append("<h2>Intro</h2>")
            meta_parts.append(f"<p>{html.escape(intro)}</p>")
        meta_html = (
            "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>"
             + "".join(meta_parts) + "</body></html>"
        )
        about = epub.EpubHtml(title="About", file_name="about.xhtml", lang=language, content=meta_html)
        book.add_item(about)
        spine.insert(1, about)
        toc.insert(0, about)

        # TOC, NCX, Nav
        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Spine & CSS
        book.spine = spine

        base = kebab(filename_hint or title)
        target_book_dir = book_dir or os.path.join(self.out_dir, base)
        ensure_dir(target_book_dir)
        out_path = os.path.join(target_book_dir, f"{base}.epub")
        print("[info] Writing EPUB file...")
        epub.write_epub(out_path, book, {})
        print(f"[info] EPUB ready: {out_path}")
        if target_book_dir:
            from src.builder import write_build_state
            write_build_state(target_book_dir, novel, novel_id or int(nv.get("novel_no") or 0), episodes, successful_episode_nos)
        return out_path, title, success_count
