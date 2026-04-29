import json
import os
import random
import time
import threading
import uuid
import requests
import concurrent.futures
import re as _re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from src import const
from src.helper import j, mask_kv, attach_auth_cookies, merge_login_at
from src.helper import extract_t_token
from src.novel import html_from_episode_text

# ----------------------------
# API Client
# ----------------------------

# Module-level cancel event — set by the UI to stop downloads
cancel_event = threading.Event()

@dataclass
class Tokens:
    login_at: Optional[str] = None
    tkey: Optional[str] = None
    userkey: Optional[str] = None


class NovelpiaClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: int = 30, throttle: float = 0.5,
                 userkey: Optional[str] = None, tkey: Optional[str] = None,
                 threads: int = 1):
        self.s = requests.Session()
        self.s.headers.update(const.SESSION_HEADERS.copy())
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        # delay seconds between episode-related API calls to reduce 429/500 rate limits
        self.throttle = max(0.0, float(throttle or 0.5))
        self.chapter_counter = 0
        self.default_max_workers = max(1, int(threads or 1))
        self.recover_attempts = 2
        self.recover_cooldown_min = 3.0
        self.recover_cooldown_max = 8.0
        self.recover_throttle = 2.0
        self.rotate_session_on_failure = True
        try:
            if not userkey:
                userkey = uuid.uuid4().hex
            # Set cookies on both domains to ensure they reach the API
            for domain in [".novelpia.com"]:
                self.s.cookies.set("USERKEY", userkey, domain=domain, path="/")
                self.s.cookies.set("last_login", "google", domain=domain, path="/")
            self.tokens.userkey = userkey
            if tkey:
                self.s.cookies.set("TKEY", tkey, domain=".novelpia.com", path="/")
                self.tokens.tkey = tkey
        except Exception as e:
            print(f"Error setting cookies: {e}")



    def login(self):
        url = f"{const.API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s, "POST", url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        data = r.json()
        self.tokens.login_at = data["result"]["LOGINAT"]
        # Capture cookies after successful login
        try:
            for c in self.s.cookies:
                if c.name == "TKEY":
                    self.tokens.tkey = c.value
                elif c.name == "USERKEY":
                    self.tokens.userkey = c.value
        except Exception:
            pass

    def refresh(self) -> Optional[str]:
        url = f"{const.API_BASE}/v1/login/refresh"
        # /v1/login/refresh works with session cookies alone (USERKEY).
        # Do NOT send login-at header — if the JWT is expired, the API
        # will reject the request even though cookies would succeed.
        r = request_with_retries(
            self.s, "GET", url,
            timeout=self.timeout, max_retries=2,
        )
        r.raise_for_status()
        self.tokens.login_at = r.json()["result"]["LOGINAT"]
        # Persist refreshed token to config
        try:
            cfg: Dict[str, Any] = {}
            if os.path.exists(const.CONFIG_PATH):
                try:
                    with open(const.CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                except Exception as e:
                    print(f"Error loading config: {e}")
                    cfg = {}
            cfg["login_at"] = self.tokens.login_at
            with open(const.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                pass
        except Exception as e:
            print(f"Error saving config: {e}")
            pass
        return self.tokens.login_at

    def _on_rate_limit(self):
        """Increase throttle when 429 occurs."""
        old = self.throttle
        self.throttle = min(15.0, self.throttle + 1.5)
        if const.HTTP_LOG:
            print(f"[api] Increased throttle from {old}s to {self.throttle}s due to rate limit.")

    def me(self) -> Dict:
        url = f"{const.API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def novel(self, novel_id: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel"
        has_auth = bool(self.tokens.login_at or self.email)
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id},
            timeout=self.timeout, allow_refresh=has_auth, 
            refresh_fn=self.refresh if has_auth else None,
            login_fn=self.login if has_auth else None,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def episode_list(self, novel_id: int, rows: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/list"
        has_auth = bool(self.tokens.login_at or self.email)
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            timeout=self.timeout, allow_refresh=has_auth, 
            refresh_fn=self.refresh if has_auth else None,
            login_fn=self.login if has_auth else None,
            on_rate_limit=self._on_rate_limit
        )
        r.raise_for_status()
        return r.json()

    def episode_ticket(self, episode_no: int) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        # Throttle before hitting ticket endpoint to avoid rate limits
        if self.throttle:
            time.sleep(self.throttle)
        r = request_with_retries(
            self.s, "GET", url,
            headers=headers, params=params,
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit, max_retries=4,
        )
        if r.status_code >= 400:
            raise requests.HTTPError(describe_http_error(r), response=r)
        return r.json()

    def episode_content(self, token_t: str) -> Dict:
        url = f"{const.API_BASE}/v1/novel/episode/content"
        # No separate throttle here — ticket call already throttles
        r = request_with_retries(
            self.s, "GET", url,
            params={"_t": token_t},
            timeout=self.timeout, max_retries=3,
            allow_refresh=True, refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        if r.status_code >= 400:
            raise requests.HTTPError(describe_http_error(r), response=r)
        return r.json()

    def fetch_episode(self, ep: Dict, idx: int = 0) -> Dict:
        """Fetch ticket and content for a single episode."""
        episode_no = ep.get("episode_no")
        if episode_no is None:
            return {
                "error": "missing episode_no",
                "epi_no": None,
                "epi_title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                "idx": idx,
            }
        epi_no = int(episode_no)
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
        self.chapter_counter += 1

        if cancel_event.is_set():
            return {"error": "cancelled", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}
        
        # 1) Ticket
        try:
            tdata = self.episode_ticket(epi_no)
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        token_t, direct_url = extract_t_token(tdata)
        if not token_t and not direct_url:
            return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        if cancel_event.is_set():
            return {"error": "cancelled", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 2) Content
        try:
            if token_t:
                cdata = self.episode_content(token_t)
            else:
                assert direct_url is not None, "direct_url unavailable"
                r = self.s.get(direct_url, timeout=self.timeout)
                r.raise_for_status()
                cdata = r.json()
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 3) Extract HTML
        result_block = cdata.get("result", {})
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        try:
            def _key(k: str):
                m = _re.search(r"(\d+)$", k)
                return (0 if k == "epi_content" else 1, int(m.group(1)) if m else 0)
            for k in sorted([kk for kk in data_block.keys() if str(kk).startswith("epi_content")], key=_key):
                v = data_block.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
        except Exception:
            pass

        html_text = "".join(parts).strip()
        if not html_text:
            html_text = (
                result_block.get("content")
                or result_block.get("html")
                or result_block.get("text")
                or cdata.get("content")
                or ""
            )

        return {
            "html": html_from_episode_text(html_text),
            "epi_title": epi_title,
            "epi_no": epi_no,
            "idx": idx,
        }

    def fetch_episodes_parallel(self, ep_list: List[Dict[str, Any]], max_workers: int = 1, progress_cb=None) -> List[Dict[str, Any]]:
        """Fetch multiple episodes using the active fetch profile."""
        worker_count = max(1, int(max_workers or self.default_max_workers or 1))
        if worker_count <= 1:
            return self._fetch_episodes_sequential(ep_list, progress_cb=progress_cb)
        return self._fetch_episodes_concurrent(ep_list, max_workers=worker_count, progress_cb=progress_cb)

    def _fetch_episodes_sequential(self, ep_list: List[Dict[str, Any]], progress_cb=None) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = [{} for _ in range(len(ep_list))]
        for idx, ep in enumerate(ep_list, 1):
            res = self.fetch_episode(ep, idx)
            if (not res) or ("error" in res):
                err = res.get("error") if res else "Unknown error"
                print(f"[warn] Chapter {idx} failed on first attempt: {err}")
                res = self._recover_episode(ep, idx)
            results[idx - 1] = res
            if progress_cb:
                ok = bool(res) and "error" not in res
                progress_cb(idx, ok, res)
        return results

    def _fetch_episodes_concurrent(self, ep_list: List[Dict[str, Any]], max_workers: int, progress_cb=None) -> List[Dict[str, Any]]:
        """Fetch episodes in batches of max_workers, like NpiaDownloader67.

        Each batch submits max_workers chapters simultaneously with no
        per-request throttle inside the batch. The throttle delay is applied
        between batches instead, preventing rate limits while maximising
        throughput.
        """
        print(f"[info] Fetching with {max_workers} concurrent workers, {self.throttle}s delay between batches.")
        total = len(ep_list)
        results: List[Dict[str, Any]] = [{} for _ in range(total)]
        num_batches = (total + max_workers - 1) // max_workers

        # Temporarily disable per-request throttle since we throttle between batches
        saved_throttle = self.throttle

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for batch_start in range(0, total, max_workers):
                batch = ep_list[batch_start:batch_start + max_workers]
                batch_indices = list(range(batch_start, batch_start + len(batch)))
                batch_num = batch_start // max_workers + 1
                ch_ids = [i + 1 for i in batch_indices]
                batch_t0 = time.time()
                print(f"[batch {batch_num}/{num_batches}] Downloading Ch.{ch_ids[0]}-{ch_ids[-1]} ({len(batch)} chapters)...")

                # Submit batch — no throttle inside batch for true parallelism
                self.throttle = 0
                future_map = {}
                for i, ep in zip(batch_indices, batch):
                    idx = i + 1  # 1-based
                    future_map[executor.submit(self.fetch_episode, ep, idx)] = (idx, ep)

                # Wait for batch to complete
                for future in concurrent.futures.as_completed(future_map):
                    if cancel_event.is_set():
                        # Cancel remaining futures
                        for f in future_map:
                            f.cancel()
                        raise KeyboardInterrupt("Cancelled by user")
                    idx, ep = future_map[future]
                    try:
                        res = future.result()
                    except Exception as e:
                        res = {"error": str(e), "idx": idx}

                    if (not res) or ("error" in res):
                        if cancel_event.is_set():
                            raise KeyboardInterrupt("Cancelled by user")
                        err = res.get("error") if res else "Unknown error"
                        print(f"[warn] Chapter {idx} failed: {err}")
                        self.throttle = saved_throttle
                        res = self._recover_episode(ep, idx)
                        self.throttle = 0

                    results[idx - 1] = res
                    if progress_cb:
                        ok = bool(res) and "error" not in res
                        progress_cb(idx, ok, res)

                batch_elapsed = time.time() - batch_t0
                print(f"[batch {batch_num}/{num_batches}] Done in {batch_elapsed:.1f}s")

                # Check for cancellation
                if cancel_event.is_set():
                    print("[info] Download cancelled by user.")
                    raise KeyboardInterrupt("Cancelled by user")

                # Throttle between batches
                if batch_start + max_workers < total:
                    time.sleep(saved_throttle)

        self.throttle = saved_throttle
        return results

    def _recover_episode(self, ep: Dict[str, Any], idx: int) -> Dict[str, Any]:
        if cancel_event.is_set():
            return {"error": "cancelled", "idx": idx}
        old_throttle = self.throttle
        retry_res: Optional[Dict[str, Any]] = None
        self.throttle = min(10.0, max(self.throttle + 1.0, self.recover_throttle))
        try:
            for attempt in range(1, self.recover_attempts + 1):
                if cancel_event.is_set():
                    return {"error": "cancelled", "idx": idx}
                cooldown = random.uniform(self.recover_cooldown_min, self.recover_cooldown_max)
                print(
                    f"[warn] Cooling down {cooldown:.1f}s before recovery attempt {attempt}/{self.recover_attempts} "
                    f"for chapter {idx}..."
                )
                # Sleep in small increments so cancel is responsive
                for _ in range(int(cooldown * 10)):
                    if cancel_event.is_set():
                        return {"error": "cancelled", "idx": idx}
                    time.sleep(0.1)

                if self.rotate_session_on_failure:
                    try:
                        if self.tokens.login_at:
                            print("[info] Trying session refresh before retry...")
                            self.refresh()
                    except Exception as e:
                        print(f"[warn] Session refresh failed before retry: {e}")

                    try:
                        if self.email and self.password:
                            print("[info] Trying full re-login before retry...")
                            self.login()
                    except Exception as e:
                        print(f"[warn] Full re-login failed before retry: {e}")

                retry_res = self.fetch_episode(ep, idx)
                if retry_res and "error" not in retry_res:
                    print(f"[info] Recovered chapter {idx} on recovery attempt {attempt}/{self.recover_attempts}.")
                    return retry_res

                err = retry_res.get("error") if retry_res else "Unknown error"
                print(f"[warn] Recovery attempt {attempt}/{self.recover_attempts} failed for chapter {idx}: {err}")

            return retry_res if retry_res else {"error": "recovery failed", "idx": idx}
        finally:
            self.throttle = old_throttle


def describe_http_error(resp: requests.Response) -> str:
    base = f"{resp.status_code} {resp.reason} for url: {resp.url}"
    try:
        data = resp.json()
    except Exception:
        body = (resp.text or "").strip()
        if body:
            return f"{base} | body: {body[:300]}"
        return base

    errmsg = data.get("errmsg") or data.get("message")
    code = data.get("code")
    result = data.get("result") or {}
    result_msg = result.get("message") or result.get("name")
    details = " | ".join(str(x) for x in (errmsg, result_msg, code) if x)
    if details:
        return f"{base} | {details}"
    return base

def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None,
                          login_fn=None, on_rate_limit=None):
    """Generic request wrapper: retries on 5xx, 429, and network issues.
    If allow_refresh is True and the response indicates an expired token, invoke
    refresh_fn() followed by login_fn() if needed, then retry.
    """
    attempt = 0
    last_exc = None
    did_refresh = False
    did_login = False
    while attempt < max_retries:
        attempt += 1
        if cancel_event.is_set():
            raise requests.RequestException("Cancelled by user")
        try:
            # Inject Cookie header (except for login endpoint) using session cookies
            try:
                if "/v1/member/login" not in url:
                    attach_auth_cookies(session, headers)
            except Exception as e:
                print(f"Error occurred while attaching auth cookies: {e}")
                pass

            if const.HTTP_LOG:
                print(f"[api]   -> {method} {url} (attempt {attempt}/{max_retries})")
                try:
                    eff_headers = {}
                    try:
                        eff_headers.update(getattr(session, "headers", {}) or {})
                    except Exception as e:
                        print(f"Error occurred while fetching session headers: {e}")
                        pass
                    if headers:
                        eff_headers.update(headers)
                except Exception as e:
                    print(f"[api]   req-headers: <unavailable> ({e})")
                if params:
                    print(f"[api]   params:  {j(mask_kv(params))}")
                if json is not None:
                    print(f"[api]   json:    {j(mask_kv(json))}")

            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)

            if const.HTTP_LOG and r.status_code != 200:
                print(f"[api]   <- {r.status_code} {r.reason} from {r.url}")
                print(f"[api]   <- Response content: {r.text}")
            
            # Handle rate limiting (429)
            if r.status_code == 429:
                if on_rate_limit:
                    on_rate_limit()
                wait = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                if const.HTTP_LOG:
                    print(f"[api] !! Rate limit (429) hit. Waiting {wait:.1f}s...")
                time.sleep(wait)
                continue

            # Handle too many requests or server errors (5xx)
            if r.status_code >= 500:
                # Check if it's actually an auth error disguised as 500
                auth_err = False
                try:
                    body = r.json()
                    msg = (body.get("errmsg") or body.get("message") or "").lower()
                    if "logged in" in msg or "login" in msg:
                        auth_err = True
                except Exception:
                    pass

                if not auth_err:
                    if on_rate_limit:
                        on_rate_limit()
                    wait = min(3.0, backoff ** attempt) + random.uniform(0.2, 0.8)
                    time.sleep(wait)
                    continue

            # Handle auth refresh-and-retry for all endpoints except login/refresh
            if allow_refresh and (refresh_fn or login_fn) and not did_login:
                trigger_refresh = False
                if r.status_code in (401, 403):
                    trigger_refresh = True
                else:
                    msg = ""
                    try:
                        body = r.json()
                        msg = (body.get("errmsg") or body.get("message") or "").lower()
                    except Exception:
                        pass
                    if "token" in msg and "expire" in msg:
                        trigger_refresh = True
                    elif "logged in" in msg or "login" in msg:
                        trigger_refresh = True

                if trigger_refresh:
                    try:
                        success = False
                        # Try refresh first
                        if refresh_fn and not did_refresh:
                            if const.HTTP_LOG: print("[api] Session expired, trying refresh...")
                            try:
                                refresh_fn()
                                did_refresh = True
                                success = True
                            except Exception:
                                if const.HTTP_LOG: print("[api] Refresh failed.")
                        
                        # Try full login if refresh failed or not available
                        if not success and login_fn and not did_login:
                            if const.HTTP_LOG: print("[api] Refresh failed or unavailable, trying full re-login...")
                            try:
                                login_fn()
                                did_login = True
                                success = True
                            except Exception as e:
                                if const.HTTP_LOG: print(f"[api] Re-login failed: {e}")

                        if success:
                            # Retry original request once
                            r = session.request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)
                    except Exception as e:
                        if const.HTTP_LOG: print(f"[api] Auth recovery failed: {e}")

            if r.json and r.status_code >= 500 and attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            return r
        except requests.RequestException as e:
            if const.HTTP_LOG:
                print(f"[api] !! {method} {url} failed on attempt {attempt}: {e}")
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return r
