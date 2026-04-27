from bs4 import BeautifulSoup
from src.helper import normalize_url

# ----------------------------
# Novelpia Novel & Episodes Fetcher
# ----------------------------

def html_from_episode_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")

    # normalize images
    for img in soup.find_all("img"):
        if img.get("data-src") and not img.get("src"):
            img["src"] = img["data-src"]
        if "style" in img.attrs:
            del img["style"]
        if img.get("src"):
            img["src"] = normalize_url(img["src"])

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
    # Auth check — only if we have a session token
    # Auth: verify the token is valid by decoding the JWT
    if client.tokens.login_at:
        try:
            import base64, json as _json
            parts = client.tokens.login_at.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                payload += "=" * (-len(payload) % 4)
                data = _json.loads(base64.urlsafe_b64decode(payload))
                import time as _time
                exp = data.get("exp", 0)
                now = int(_time.time())
                mem_no = data.get("mem_no", "?")
                if exp and now > exp:
                    # Token expired — try refresh
                    print(f"[auth] Token expired, attempting refresh...")
                    try:
                        client.refresh()
                        print(f"[auth] Token refreshed successfully (member #{mem_no})")
                    except Exception:
                        print("[warn] Token expired and refresh failed -- falling back to anonymous mode.")
                        client.tokens.login_at = None
                else:
                    remaining = exp - now if exp else 0
                    print(f"[auth] Token valid for member #{mem_no} ({remaining // 60}m {remaining % 60}s remaining)")
        except Exception as e:
            print(f"[warn] Could not verify token: {e}")

    print("[info] extracting metadata…")
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
