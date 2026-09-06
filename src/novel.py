from bs4 import BeautifulSoup
from src.images import normalize_image_tags

# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------

def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")

    # Normalize lazy and responsive sources early, while preserving inline styles
    # so background images can be localized by the output builder.
    normalize_image_tags(soup)

    # Ensure document wrapper
    if not soup.find("html"):
        html_tag = soup.new_tag("html")
        head = soup.new_tag("head")
        meta = soup.new_tag("meta", charset="utf-8")
        head.append(meta)
        body = soup.new_tag("body")
        for el in list(soup.children):
            body.append(el.extract())
        html_tag.append(head)
        html_tag.append(body)
        soup.append(html_tag)

    return str(soup)

def fetch_novel_and_episodes(client, novel_id, start_chapter=None, end_chapter=None, max_chapters=None):
    # The API client validates authentication and refreshes expired sessions.
    # Decoding a JWT locally does not prove login, and must not erase the
    # session when a refresh temporarily fails.
    print("[info] extracting metadata...")
    data_novel = client.novel(novel_id)

    nv = data_novel["result"]["novel"]
    title = nv.get("novel_name", f"novel_{novel_id}")
    epi_cnt = data_novel["result"].get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0
    writers = data_novel["result"].get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    
    print(f"[info] title='{title}' author='{author}' chapter={epi_cnt} status={status}")

    rows = int(epi_cnt) if epi_cnt else 1000
    data_list = client.episode_list(novel_id, rows=rows)
    ep_list = data_list["result"].get("list", [])

    # Handle range
    if start_chapter:
        ep_list = [ep for ep in ep_list if int(ep.get("epi_num", 0)) >= int(start_chapter)]
    if end_chapter:
        ep_list = [ep for ep in ep_list if int(ep.get("epi_num", 0)) <= int(end_chapter)]

    if max_chapters:
        ep_list = ep_list[:int(max_chapters)]

    return data_novel, ep_list, title
