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
from src.helper import j, mask_kv, merge_login_at, save_config
from src.helper import extract_t_token
from src.novel import html_from_episode_text
from src.advertisements import AdvertisementError, AdvertisementResult, watch_episode_ad
from src.ad_navigation import (
    DEFAULT_AD_RETRIES, DEFAULT_AD_RETRY_COOLDOWN, validate_ad_retry_settings,
)

# ----------------------------
# API Client
# ----------------------------

# Module-level cancel event — set by the UI to stop downloads
cancel_event = threading.Event()


class AuthenticationError(RuntimeError):
    """An authentication failure with a message suitable for the desktop UI."""


def _session_token(response: requests.Response, action: str) -> str:
    try:
        data = response.json()
    except ValueError:
        data = {}
    result = data.get("result") if isinstance(data, dict) else None
    token = result.get("LOGINAT") if isinstance(result, dict) else None
    status = data.get("statusCode", 200) if isinstance(data, dict) else None
    if response.ok and str(status) == "200" and isinstance(token, str) and token.strip():
        return token.strip()
    if action == "login":
        raise AuthenticationError(
            f"Novelpia email/password login failed (HTTP {response.status_code}). "
            "Check your Novelpia credentials, or use Login with Google in the Login tab "
            "for a Google account, then retry the download."
        )
    raise AuthenticationError(
        f"Novelpia could not refresh the saved session (HTTP {response.status_code}). "
        "Sign in again using Login with Google, or import fresh browser session cookies."
    )

@dataclass
class Tokens:
    login_at: Optional[str] = None
    tkey: Optional[str] = None
    userkey: Optional[str] = None


class NovelpiaClient:
    IMAGE_COOKIE_NAMES = (
        "CloudFront-Policy",
        "CloudFront-Key-Pair-Id",
        "CloudFront-Signature",
    )

    def __init__(self, email: Optional[str] = None, password: Optional[str] = None,
                 proxy: Optional[str] = None, timeout: int = 30, throttle: Optional[float] = None,
                 userkey: Optional[str] = None, tkey: Optional[str] = None,
                 threads: int = 1, min_interval: Optional[float] = None,
                 max_interval: Optional[float] = None,
                 ad_retries: int = DEFAULT_AD_RETRIES,
                 ad_retry_cooldown: float = DEFAULT_AD_RETRY_COOLDOWN):
        self.ad_retries, self.ad_retry_cooldown = validate_ad_retry_settings(
            ad_retries, ad_retry_cooldown,
        )
        self.s = requests.Session()
        self.s.headers.update(const.SESSION_HEADERS.copy())
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        # Random delay range between episode-related API calls. A supplied
        # legacy `throttle` value intentionally creates a fixed interval.
        if throttle is not None:
            interval_min = interval_max = float(throttle)
        else:
            interval_min = 0.5 if min_interval is None else float(min_interval)
            interval_max = 2.0 if max_interval is None else float(max_interval)
        if interval_min < 0 or interval_max < 0:
            raise ValueError("Request intervals cannot be negative.")
        if interval_min > interval_max:
            raise ValueError("Minimum request interval cannot exceed maximum request interval.")
        self.interval_min = interval_min
        self.interval_max = interval_max
        self._interval_lock = threading.Lock()
        self._request_context = threading.local()
        self._auth_lock = threading.RLock()
        self._auth_generation = 0
        self.chapter_counter = 0
        self.default_max_workers = max(1, int(threads or 1))
        # The Download settings govern both chapter recovery and viewer reloads.
        self.recover_attempts = self.ad_retries
        self.recover_cooldown_min = self.ad_retry_cooldown
        self.recover_cooldown_max = self.ad_retry_cooldown
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

    @property
    def throttle(self) -> float:
        """Backward-compatible fixed-throttle view (returns the upper bound)."""
        return self.interval_max

    @throttle.setter
    def throttle(self, value: float) -> None:
        fixed = max(0.0, float(value))
        with self._interval_lock:
            self.interval_min = fixed
            self.interval_max = fixed

    def _next_request_interval(self) -> float:
        with self._interval_lock:
            lower, upper = self.interval_min, self.interval_max
        recovery_bounds = getattr(self._request_context, "recovery_bounds", None)
        if recovery_bounds is not None:
            lower = max(lower, recovery_bounds[0])
            upper = max(upper, recovery_bounds[1], lower)
        if lower == upper:
            return lower
        return random.uniform(lower, upper)

    def _sleep_request_interval(self) -> float:
        delay = self._next_request_interval()
        if delay > 0:
            if const.HTTP_LOG:
                print(f"[api] Waiting {delay:.2f}s before the next episode request.")
            cancel_event.wait(delay)
        return delay



    def login(self):
        with self._auth_lock:
            token = self._login()
            self._auth_generation += 1
            return token

    def _login(self):
        if not self.email or not self.password:
            raise AuthenticationError("No email/password credentials are available. Sign in again in the Login tab.")
        self.s.cookies.set("last_login", "basic", domain=".novelpia.com", path="/")
        url = f"{const.API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s, "POST", url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout, max_retries=2,
        )
        self.tokens.login_at = _session_token(r, "login")
        # Capture cookies after successful login
        try:
            for c in self.s.cookies:
                if c.name == "TKEY":
                    self.tokens.tkey = c.value
                elif c.name == "USERKEY":
                    self.tokens.userkey = c.value
        except Exception:
            pass
        return self.tokens.login_at

    def refresh(self) -> Optional[str]:
        generation = self._auth_generation
        with self._auth_lock:
            if generation != self._auth_generation and self.tokens.login_at:
                return self.tokens.login_at
            token = self._refresh()
            self._auth_generation += 1
            return token

    def _refresh(self) -> Optional[str]:
        url = f"{const.API_BASE}/v1/login/refresh"
        # /v1/login/refresh works with session cookies (including TKEY).
        # Do NOT send login-at header — if the JWT is expired, the API
        # will reject the request even though cookies would succeed.
        r = request_with_retries(
            self.s, "GET", url,
            headers={"login-at": None},
            timeout=self.timeout, max_retries=2,
        )
        self.tokens.login_at = _session_token(r, "refresh")
        for cookie in self.s.cookies:
            if cookie.name == "USERKEY":
                self.tokens.userkey = cookie.value
            elif cookie.name == "TKEY":
                self.tokens.tkey = cookie.value
        save_config({
            "login_at": self.tokens.login_at,
            "userkey": self.tokens.userkey or "",
            "tkey": self.tokens.tkey or "",
        })
        return self.tokens.login_at

    def _on_rate_limit(self):
        """Increase both interval bounds when a 429 response occurs."""
        with self._interval_lock:
            old_min = self.interval_min
            old_max = self.interval_max
            self.interval_min = min(15.0, self.interval_min + 1.5)
            self.interval_max = min(15.0, max(self.interval_min, self.interval_max + 1.5))
        if const.HTTP_LOG:
            print(
                "[api] Increased request interval from "
                f"{old_min:.2f}-{old_max:.2f}s to "
                f"{self.interval_min:.2f}-{self.interval_max:.2f}s due to rate limit."
            )

    def me(self) -> Dict:
        url = f"{const.API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s, "GET", url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit
        )
        if not r.ok or _is_auth_error(r):
            raise AuthenticationError("The saved Novelpia session is no longer valid. Sign in again in the Login tab.")
        data = r.json()
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not result.get("login"):
            raise AuthenticationError("Novelpia did not recognize the saved session. Sign in again in the Login tab.")
        return data

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

    def _episode_ticket_response(self, episode_no: int, *, max_retries: int = 4) -> requests.Response:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        return request_with_retries(
            self.s, "GET", url,
            headers=headers, params=params,
            timeout=self.timeout, allow_refresh=True, 
            refresh_fn=self.refresh, login_fn=self.login,
            on_rate_limit=self._on_rate_limit, max_retries=max_retries,
        )

    def episode_ticket(self, episode_no: int) -> Dict:
        # Randomized pause before the ticket endpoint to avoid rate limits.
        if cancel_event.is_set():
            raise requests.RequestException("Cancelled by user")
        self._sleep_request_interval()
        if cancel_event.is_set():
            raise requests.RequestException("Cancelled by user")
        r = self._episode_ticket_response(episode_no)
        if _is_ad_required(r):
            print(
                f"[ad] Episode {episode_no} requires an advertisement. Opening a minimized ad window; "
                "Continue will be auto-clicked when the ad finishes.", flush=True,
            )
            r = watch_episode_ad(
                episode_no,
                probe=lambda: self._episode_ticket_response(episode_no, max_retries=1),
                cancelled=cancel_event,
                is_unlocked=_has_episode_ticket,
                max_retries=self.ad_retries,
                retry_cooldown=self.ad_retry_cooldown,
            )
            if isinstance(r, AdvertisementResult):
                print(f"[ad] Episode {episode_no}: Advertisement complete. Chapter received; resuming download.", flush=True)
                data = dict(r.ticket)
                data["_viewer_content"] = r.content
                return data
            print(f"[ad] Episode {episode_no}: Access confirmed. Resuming download.", flush=True)
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

    @classmethod
    def signed_image_cookies(cls, ticket_data: Dict) -> Dict[str, str]:
        """Extract temporary CDN cookies without persisting them to disk."""
        result = ticket_data.get("result") or {}
        signed_key = result.get("signed_key") or {}
        if not isinstance(signed_key, dict):
            return {}
        return {
            name: str(signed_key[name])
            for name in cls.IMAGE_COOKIE_NAMES
            if signed_key.get(name)
        }

    def episode_image_cookies(self, episode_no: int) -> Dict[str, str]:
        """Fetch fresh signed cookies for protected images in a cached chapter."""
        return self.signed_image_cookies(self.episode_ticket(episode_no))

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
        except AdvertisementError as e:
            # A closed/unavailable ad cannot be fixed by rotating credentials.
            return {
                "error": str(e), "epi_no": epi_no, "epi_title": epi_title,
                "idx": idx, "retryable": False,
            }
        except Exception as e:
            return {"error": str(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        token_t, direct_url = extract_t_token(tdata)
        if not token_t and not direct_url:
            return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        if cancel_event.is_set():
            return {"error": "cancelled", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        # 2) Content
        try:
            if "_viewer_content" in tdata:
                cdata = tdata["_viewer_content"]
            elif token_t:
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

        result = {
            "html": html_from_episode_text(html_text),
            "epi_title": epi_title,
            "epi_no": epi_no,
            "idx": idx,
        }
        image_cookies = self.signed_image_cookies(tdata)
        if image_cookies:
            # Private, in-memory field. save_cached_episode deliberately does
            # not persist these temporary signed values.
            result["_image_cookies"] = image_cookies
        return result

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
                if self.recover_attempts > 0 and (not res or res.get("retryable", True)):
                    res = self._recover_episode(ep, idx)
            results[idx - 1] = res
            if progress_cb:
                ok = bool(res) and "error" not in res
                progress_cb(idx, ok, res)
        return results

    def _fetch_episodes_concurrent(self, ep_list: List[Dict[str, Any]], max_workers: int, progress_cb=None) -> List[Dict[str, Any]]:
        """Refill each worker independently, including its delays and recovery."""
        print(
            f"[info] Fetching with {max_workers} concurrent workers, "
            f"{self.interval_min:.1f}-{self.interval_max:.1f}s random delay per chapter request. "
            "Each free worker starts the next queued chapter.", flush=True,
        )
        total = len(ep_list)
        results: List[Dict[str, Any]] = [{} for _ in range(total)]
        if not total:
            return results

        def check_cancelled():
            if cancel_event.is_set():
                raise KeyboardInterrupt("Cancelled by user")

        def fetch_with_recovery(ep, idx):
            check_cancelled()
            try:
                res = self.fetch_episode(ep, idx)
            except Exception as exc:
                res = {"error": str(exc), "idx": idx}
            check_cancelled()
            if not res or "error" in res:
                err = res.get("error") if res else "Unknown error"
                print(f"[warn] Chapter {idx} failed: {err}", flush=True)
                if self.recover_attempts > 0 and (not res or res.get("retryable", True)):
                    # Recovery keeps this slot; it cannot block the scheduler
                    # or create an extra download outside the worker limit.
                    res = self._recover_episode(ep, idx)
            return res

        next_index = 0
        pending = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            def fill_available_slots():
                nonlocal next_index
                while next_index < total and len(pending) < max_workers:
                    check_cancelled()
                    idx = next_index + 1
                    ep = ep_list[next_index]
                    pending[executor.submit(fetch_with_recovery, ep, idx)] = (idx, ep)
                    next_index += 1

            try:
                fill_available_slots()
                while pending:
                    check_cancelled()
                    done, _ = concurrent.futures.wait(
                        pending, timeout=0.2,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        check_cancelled()
                        idx, ep = pending.pop(future)
                        try:
                            res = future.result()
                        except Exception as exc:
                            res = {"error": str(exc), "idx": idx,
                                   "epi_no": ep.get("episode_no"), "epi_title": ep.get("epi_title")}
                        results[idx - 1] = res
                        # Keep progress/cache consumers on this coordinator
                        # thread, with the original index even out of order.
                        if progress_cb:
                            progress_cb(idx, bool(res) and "error" not in res, res)
                        fill_available_slots()
            finally:
                for future in pending:
                    future.cancel()
        return results

    def _recover_episode(self, ep: Dict[str, Any], idx: int) -> Dict[str, Any]:
        if cancel_event.is_set():
            return {"error": "cancelled", "idx": idx}
        previous_bounds = getattr(self._request_context, "recovery_bounds", None)
        with self._interval_lock:
            recovery_min = min(10.0, max(self.interval_min + 1.0, self.recover_throttle))
            recovery_max = min(10.0, max(self.interval_max + 1.0, recovery_min))
        # Recovery changes only this worker's delay. Global 429 backoff remains
        # in force and cannot be rolled back by another worker finishing a retry.
        self._request_context.recovery_bounds = (recovery_min, recovery_max)
        retry_res: Optional[Dict[str, Any]] = None
        try:
            for attempt in range(1, self.recover_attempts + 1):
                if cancel_event.is_set():
                    return {"error": "cancelled", "idx": idx}
                auth_generation = self._auth_generation
                cooldown = random.uniform(self.recover_cooldown_min, self.recover_cooldown_max)
                print(
                    f"[warn] Cooling down {cooldown:.1f}s before recovery attempt {attempt}/{self.recover_attempts} "
                    f"for chapter {idx}..."
                )
                if cancel_event.wait(cooldown):
                    return {"error": "cancelled", "idx": idx}

                if self.rotate_session_on_failure:
                    # Other workers keep downloading. Only auth changes share
                    # a lock, including cookies and the saved session file.
                    with self._auth_lock:
                        if cancel_event.is_set():
                            return {"error": "cancelled", "idx": idx}
                        if auth_generation == self._auth_generation:
                            refreshed = False
                            try:
                                if self.tokens.login_at:
                                    print("[info] Trying session refresh before retry...")
                                    self.refresh()
                                    refreshed = True
                            except Exception as e:
                                print(f"[warn] Session refresh failed before retry: {e}")

                            try:
                                if not refreshed and self.email and self.password:
                                    print("[info] Trying full re-login before retry...")
                                    self.login()
                            except Exception as e:
                                print(f"[warn] Full re-login failed before retry: {e}")

                retry_res = self.fetch_episode(ep, idx)
                if retry_res and not retry_res.get("retryable", True):
                    return retry_res
                if retry_res and "error" not in retry_res:
                    print(f"[info] Recovered chapter {idx} on recovery attempt {attempt}/{self.recover_attempts}.")
                    return retry_res

                err = retry_res.get("error") if retry_res else "Unknown error"
                print(f"[warn] Recovery attempt {attempt}/{self.recover_attempts} failed for chapter {idx}: {err}")

            return retry_res if retry_res else {"error": "recovery failed", "idx": idx}
        finally:
            self._request_context.recovery_bounds = previous_bounds


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

def _is_ad_required(response: requests.Response) -> bool:
    """The episode API reports ad gates as HTTP 500 / application code 0010."""
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    result = body.get("result")
    result = result if isinstance(result, dict) else {}
    code = str(body.get("code") or result.get("code") or "")
    message = str(body.get("errmsg") or body.get("message") or result.get("message") or "").lower()
    return code == "0010" or "basic advertisement" in message


def _has_episode_ticket(response: requests.Response) -> bool:
    if not response.ok or _is_ad_required(response):
        return False
    try:
        body = response.json()
        if not isinstance(body, dict) or str(body.get("statusCode", 200)) != "200":
            return False
        if not isinstance(body.get("result"), dict):
            return False
        token, url = extract_t_token(body)
        return bool(token or url)
    except ValueError:
        return False


def _is_auth_error(response: requests.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    try:
        body = response.json()
        if not isinstance(body, dict):
            return False
        result = body.get("result")
        result = result if isinstance(result, dict) else {}
        message = str(body.get("errmsg") or body.get("message") or result.get("message") or "").lower()
        return (
            "logged in" in message or "login" in message
            or ("token" in message and ("expire" in message or "invalid" in message))
            or str(body.get("code") or result.get("code")) == "0004"
        )
    except ValueError:
        return False


def request_with_retries(session: requests.Session, method: str, url: str, *,
                          headers=None, params=None, json=None, data=None,
                          timeout=30, max_retries=3, backoff=1.25,
                          allow_refresh=False, refresh_fn=None,
                          login_fn=None, on_rate_limit=None):
    """Retry transient failures and send the NEW token after auth recovery."""
    request_headers = dict(headers or {})
    did_refresh = False
    did_login = False
    attempt = 0
    while attempt < max(1, max_retries):
        attempt += 1
        if cancel_event.is_set():
            raise requests.RequestException("Cancelled by user")
        try:
            # Let requests build cookies from the current jar on every attempt.
            # A manually cached Cookie header would miss cookies rotated by refresh.
            if const.HTTP_LOG:
                print(f"[api]   -> {method} {url} (attempt {attempt}/{max_retries})")
                if params:
                    print(f"[api]   params:  {j(mask_kv(params))}")
                if json is not None:
                    print(f"[api]   json:    {j(mask_kv(json))}")
            response = session.request(
                method, url, headers=request_headers, params=params,
                json=json, data=data, timeout=timeout,
            )
        except requests.RequestException:
            if attempt >= max_retries:
                raise
            time.sleep(backoff ** attempt)
            continue

        if const.HTTP_LOG and response.status_code != 200:
            print(f"[api]   <- {response.status_code} {response.reason}")

        # This is an application gate, not a transient server/auth failure.
        # Return it immediately so episode_ticket can show the advertisement.
        if url.split("?", 1)[0].rstrip("/").endswith("/v1/novel/episode") and _is_ad_required(response):
            return response

        auth_error = _is_auth_error(response)
        if allow_refresh and auth_error:
            token = None
            if refresh_fn and not did_refresh:
                did_refresh = True
                try:
                    token = refresh_fn()
                except (requests.RequestException, AuthenticationError, ValueError, KeyError):
                    pass
            if not token and login_fn and not did_login:
                did_login = True
                try:
                    token = login_fn()
                except (requests.RequestException, AuthenticationError, ValueError, KeyError):
                    pass
            if token:
                # Header names are case insensitive. Remove the expired value
                # before merging, including values supplied as Login-At.
                request_headers = {
                    key: value for key, value in request_headers.items()
                    if key.lower() != "login-at"
                }
                request_headers = merge_login_at(request_headers, token)
                # Auth recovery has its own one-refresh/one-login limit, so it
                # still works when the original request had only one attempt.
                attempt -= 1
                continue

        if not auth_error and (response.status_code == 429 or response.status_code >= 500):
            if attempt < max_retries:
                if on_rate_limit:
                    on_rate_limit()
                if response.status_code == 429:
                    delay = max(5.0, backoff ** (attempt + 2)) + random.uniform(0.5, 1.5)
                else:
                    delay = min(3.0, backoff ** attempt) + random.uniform(0.2, 0.8)
                time.sleep(delay)
                continue
        return response
