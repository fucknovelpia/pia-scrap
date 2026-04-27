"""Webview-based Google login for Novelpia Global.

After Google OAuth redirect, calls /v1/login/refresh from the browser
(which works with session cookies alone) to obtain the LOGINAT JWT.
"""
from __future__ import annotations

import json
import os
import time
import traceback

LOGIN_URL = "https://global.novelpia.com/"


def _run_webview_login(output_path: str) -> None:
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "logs")
    os.makedirs(log_dir, exist_ok=True)
    debug_path = os.path.join(log_dir, "webview_debug.log")

    def _dbg(msg: str) -> None:
        try:
            with open(debug_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except Exception:
            pass

    _dbg("=== webview login started ===")

    try:
        from pythonnet import load
        load()
    except Exception:
        pass

    try:
        import webview
    except Exception:
        _dbg(f"webview import FAILED:\n{traceback.format_exc()}")
        _write_result(output_path, {})
        return

    # JS: call /v1/login/refresh from the browser with credentials:include.
    # This endpoint works with session cookies alone and returns the LOGINAT JWT.
    FETCH_REFRESH_JS = r"""
    (function() {
        window.__pia_refresh_result = null;
        window.__pia_refresh_error = null;
        window.__pia_refresh_done = false;

        fetch('https://api-global.novelpia.com/v1/login/refresh', {
            method: 'GET',
            credentials: 'include',
            headers: {
                'accept': 'application/json',
                'origin': 'https://global.novelpia.com',
                'referer': 'https://global.novelpia.com/'
            }
        })
        .then(function(r) { return r.text(); })
        .then(function(text) {
            window.__pia_refresh_result = text;
            window.__pia_refresh_done = true;
        })
        .catch(function(e) {
            window.__pia_refresh_error = e.message || String(e);
            window.__pia_refresh_done = true;
        });
    })();
    """

    def get_cookies_dict() -> dict:
        cookies = {}
        try:
            js_str = window.evaluate_js("document.cookie") or ""
            for part in js_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        except Exception:
            pass
        return cookies

    def try_refresh_token() -> str | None:
        """Call /v1/login/refresh from the browser to get the LOGINAT JWT."""
        try:
            window.evaluate_js(FETCH_REFRESH_JS)

            for _ in range(20):
                time.sleep(0.5)
                done = window.evaluate_js("window.__pia_refresh_done")
                if done:
                    break

            error = window.evaluate_js("window.__pia_refresh_error")
            if error:
                _dbg(f"Refresh fetch error: {error}")
                return None

            raw = window.evaluate_js("window.__pia_refresh_result")
            if not raw:
                _dbg("Refresh fetch: no result")
                return None

            _dbg(f"Refresh response: {raw[:200]}")

            data = json.loads(raw)
            if str(data.get("statusCode")) != "200":
                _dbg(f"Refresh failed: {data.get('errmsg')}")
                return None

            login_at = (data.get("result") or {}).get("LOGINAT")
            if login_at and isinstance(login_at, str) and len(login_at) > 20:
                _dbg(f"Got LOGINAT: {login_at[:50]}...")
                return login_at

            _dbg(f"No LOGINAT in refresh response: {list((data.get('result') or {}).keys())}")
        except Exception as e:
            _dbg(f"Refresh error: {e}")
        return None

    result_holder = {"login_at": None, "userkey": None, "tkey": None}
    was_on_google = False

    def poll_for_login(_window=None) -> None:
        nonlocal was_on_google
        _dbg("poll started...")
        time.sleep(5)

        # Try immediate refresh — user may already be logged in from a previous session
        _dbg("Trying immediate refresh (returning user)...")
        login_at = try_refresh_token()
        if login_at:
            cookies = get_cookies_dict()
            _dbg(f"Returning user SUCCESS! token={login_at[:50]}...")
            result_holder["login_at"] = login_at
            result_holder["userkey"] = cookies.get("USERKEY", "")
            result_holder["tkey"] = cookies.get("TKEY", "")
            _write_result(output_path, result_holder)
            try:
                window.destroy()
            except Exception:
                pass
            return

        _dbg("Not logged in yet, waiting for Google OAuth...")

        for i in range(900):
            try:
                cur_url = ""
                try:
                    cur_url = window.get_current_url() or ""
                except Exception:
                    pass

                if "accounts.google.com" in cur_url:
                    if not was_on_google:
                        _dbg(f"poll #{i}: Google OAuth detected")
                        was_on_google = True

                if was_on_google and "novelpia.com" in cur_url and "accounts.google" not in cur_url:
                    _dbg(f"poll #{i}: redirect back: {cur_url[:80]}")
                    was_on_google = False

                    # Wait for sign page to process
                    _dbg("Waiting 8s for login to complete...")
                    time.sleep(8)

                    for attempt in range(15):
                        _dbg(f"Token attempt {attempt+1}/15...")
                        login_at = try_refresh_token()

                        if login_at:
                            cookies = get_cookies_dict()
                            _dbg(f"SUCCESS! LOGINAT={login_at[:50]}...")
                            result_holder["login_at"] = login_at
                            result_holder["userkey"] = cookies.get("USERKEY", "")
                            result_holder["tkey"] = cookies.get("TKEY", "")
                            _write_result(output_path, result_holder)
                            try:
                                window.destroy()
                            except Exception:
                                pass
                            return

                        time.sleep(3)

                    _dbg("All attempts failed")

                if i < 5 or (i % 60 == 0):
                    _dbg(f"poll #{i}: url={cur_url[:60] if cur_url else None}, google={was_on_google}")

            except Exception as e:
                if i < 5:
                    _dbg(f"poll #{i} error: {e}")
            time.sleep(1)

    # Window sizing
    try:
        import ctypes
        _sw = ctypes.windll.user32.GetSystemMetrics(0)
        _sh = ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        _sw, _sh = 1200, 900

    # Persistent storage — keeps Google login cookies between sessions
    storage_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".webview_data")
    os.makedirs(storage_dir, exist_ok=True)
    _dbg(f"Storage path: {storage_dir}")

    try:
        window = webview.create_window(
            "Novelpia Global -- Login (wait for auto-close)",
            LOGIN_URL,
            width=int(_sw * 0.6),
            height=int(_sh * 0.7),
        )
        webview.start(poll_for_login, window, debug=False,
                      private_mode=False, storage_path=storage_dir)
    except Exception:
        _dbg(f"webview FAILED:\n{traceback.format_exc()}")

    if result_holder["login_at"] is None:
        _write_result(output_path, {})

    _dbg("=== webview login ended ===")


def _write_result(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass
