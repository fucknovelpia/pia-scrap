import json
import os
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_novel_and_episodes

# ----------------------------
# Build State & Cache Helpers
# ----------------------------

STATE_FILENAME = "build_state.json"
CACHE_DIRNAME = ".cache"
EPISODES_CACHE_DIRNAME = "episodes"


def get_book_dir(out_dir: str, title: str) -> str:
    return os.path.join(out_dir, kebab(title))


def get_cache_dir(book_dir: str) -> str:
    return os.path.join(book_dir, CACHE_DIRNAME)


def get_episode_cache_dir(book_dir: str) -> str:
    return os.path.join(get_cache_dir(book_dir), EPISODES_CACHE_DIRNAME)


def episode_cache_path(book_dir: str, episode_no: int) -> str:
    return os.path.join(get_episode_cache_dir(book_dir), f"{int(episode_no)}.json")


def load_build_state(book_dir: str) -> Dict:
    path = os.path.join(book_dir, STATE_FILENAME)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                if isinstance(data, dict):
                    return data
    except Exception as e:
        print(f"[warn] Could not load build state: {e}")
    return {}


def save_build_state(book_dir: str, state: Dict) -> None:
    path = os.path.join(book_dir, STATE_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_cached_episode(book_dir: str, episode_no: int) -> Optional[Dict]:
    path = episode_cache_path(book_dir, episode_no)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                if isinstance(data, dict) and data.get("html"):
                    return data
    except Exception as e:
        print(f"[warn] Could not load cached episode {episode_no}: {e}")
    return None


def save_cached_episode(book_dir: str, ep: Dict, res: Dict) -> None:
    episode_no = int(ep.get("episode_no"))
    ensure_dir(get_episode_cache_dir(book_dir))
    payload = {
        "episode_no": episode_no,
        "epi_num": ep.get("epi_num"),
        "epi_title": res.get("epi_title") or ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
        "url": f"https://global.novelpia.com/viewer/{episode_no}",
        "html": res.get("html") or "",
    }
    with open(episode_cache_path(book_dir, episode_no), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def write_build_state(book_dir: str, data_novel: Dict, novel_id: int, ep_list: List[Dict], successful_episode_nos: List[int]) -> None:
    nv = data_novel["result"]["novel"]
    writers = data_novel["result"].get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or len(ep_list)
    successful_set = {int(x) for x in successful_episode_nos}
    downloaded = []
    for ep in ep_list:
        episode_no = int(ep.get("episode_no"))
        if episode_no not in successful_set:
            continue
        downloaded.append(
            {
                "episode_no": episode_no,
                "epi_num": ep.get("epi_num"),
                "title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                "url": f"https://global.novelpia.com/viewer/{episode_no}",
            }
        )

    save_build_state(
        book_dir,
        {
            "novel_id": novel_id,
            "title": nv.get("novel_name"),
            "author": author,
            "source_url": f"https://global.novelpia.com/novel/{novel_id}",
            "known_total_chapters": int(epi_cnt) if epi_cnt else len(ep_list),
            "downloaded_chapter_count": len(downloaded),
            "downloaded_episodes": downloaded,
            "cache_dir": get_cache_dir(book_dir),
        },
    )


# ----------------------------
# Main Build Functions
# ----------------------------

def build_epub(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None, language="en", debug_dump=False):
    print(f"[info] Loading novel metadata for {novel_id}...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)
    print(f"[info] Loaded '{title}' with {len(ep_list)} chapters queued.")

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)
    book_dir = get_book_dir(out_dir, title)
    ensure_dir(book_dir)

    previous_state = load_build_state(book_dir)
    previous_total = previous_state.get("known_total_chapters")
    if previous_total:
        print(
            f"[info] Previous build state found: {previous_state.get('downloaded_chapter_count', 0)} chapters cached "
            f"out of known total {previous_total}."
        )

    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)
    print(f"[info] Metadata written under: {book_dir}")
    print("[info] Building EPUB and embedding assets...")

    return builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        book_dir=book_dir,
    )


def build_txt(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None, language="en", debug_dump=False):
    print(f"[info] Loading novel metadata for {novel_id}...")
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)
    print(f"[info] Loaded '{title}' with {len(ep_list)} chapters queued.")

    book_dir = get_book_dir(out_dir, title)
    ensure_dir(book_dir)

    total = 0
    failed = 0
    fetched_count = 0
    successful_episode_nos: List[int] = []

    def update_pbar(idx, ok):
        nonlocal fetched_count
        fetched_count += 1
        state = "ok" if ok else "failed"
        print(f"[progress] Chapter attempt {fetched_count}/{len(ep_list)} ({state})")

    cached_results: Dict[int, Dict] = {}
    episodes_to_fetch: List[Dict] = []
    for ep in ep_list:
        episode_no = int(ep.get("episode_no"))
        cached = load_cached_episode(book_dir, episode_no)
        if cached:
            cached_results[episode_no] = {
                "html": cached.get("html") or "",
                "epi_title": cached.get("epi_title") or ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
            }
        else:
            episodes_to_fetch.append(ep)

    if cached_results:
        print(f"[info] Reusing {len(cached_results)} cached chapters for TXT export.")
    if episodes_to_fetch:
        print(f"[info] Fetching {len(episodes_to_fetch)} uncached/new chapters for TXT export.")

    fetched_results = []
    if episodes_to_fetch:
        fetched_results = client.fetch_episodes_parallel(episodes_to_fetch, progress_cb=update_pbar)
    fetched_map = {int(ep.get("episode_no")): res for ep, res in zip(episodes_to_fetch, fetched_results)}

    for i, ep in enumerate(ep_list, 1):
        episode_no = int(ep.get("episode_no"))
        res = fetched_map.get(episode_no) or cached_results.get(episode_no)
        if not res or "error" in res:
            err = res.get("error") if res else "Unknown error"
            print(f"[warn] Failed to fetch chapter {i}: {err}")
            failed += 1
            continue

        html_text = res["html"]
        epi_title = res["epi_title"]

        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)

        save_cached_episode(book_dir, ep, res)
        successful_episode_nos.append(episode_no)
        total += 1
        print(f"[info] Wrote TXT chapter {i}: {fname}")

    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)
    write_build_state(book_dir, data_novel, novel_id, ep_list, successful_episode_nos)
    print(f"[info] Metadata written under: {book_dir}")
    if failed:
        print(f"[warn] TXT export partial: {total} chapters written, {failed} failed.")

    return book_dir, title, total


def build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters=None):
    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0
    writers = data_novel["result"].get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    description = (nv.get("novel_story") or "").strip()

    tag_items = (data_novel.get("result", {}).get("tag_list")
                 or nv.get("tag_list")
                 or [])
    tags: List[str] = []
    for t in tag_items:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            val = t.get("tag_name") or t.get("name") or t.get("title")
            if isinstance(val, str):
                tags.append(val)

    seen = set()
    uniq_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq_tags.append(t)

    meta = {
        "url": f"https://global.novelpia.com/novel/{novel_id}",
        "title": nv.get("novel_name") or title,
        "author": author,
        "tags": uniq_tags,
        "chapter": len(ep_list) if (max_chapters and max_chapters > 0) else (int(epi_cnt) if epi_cnt else len(ep_list)),
        "status": status,
        "description": description,
        "cache_dir": get_cache_dir(book_dir),
        "state_file": os.path.join(book_dir, STATE_FILENAME),
    }

    meta_path = os.path.join(book_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    chapters_path = os.path.join(book_dir, "chapters.jsonl")
    with open(chapters_path, "w", encoding="utf-8") as f:
        for idx, ep in enumerate(ep_list, 1):
            epi_no = int(ep.get("episode_no"))
            epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
            rec = {"idx": idx, "episode_no": epi_no, "title": epi_title, "url": f"https://global.novelpia.com/viewer/{epi_no}"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
