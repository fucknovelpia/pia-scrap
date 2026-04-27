"""Webview-based Google login for Novelpia Global.

Opens an embedded browser to https://global.novelpia.com/ so the user can
authenticate via Google OAuth. After the redirect back, waits for the page
to complete its own login flow, then extracts tokens from cookies and
localStorage. Results are written to a JSON temp file that the caller polls.

Designed to run in a separate multiprocessing.Process to avoid conflicts
with the tkinter main loop.
"""
from __future__ import annotations

import json
import os
import time
import traceback

LOGIN_URL = "https://global.novelpia.com/"


def _run_webview_login(output_path: str) -> None:
    """Entry point for the subprocess. Writes JSON to *output_path*."""

    # --- debug logger ---
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

    # pythonnet 3.x requires explicit runtime init
    try:
        from pythonnet import load
        load()
        _dbg("pythonnet loaded OK")
    except Exception as e:
        _dbg(f"pythonnet load (non-fatal): {e}")

    try:
        import webview
        _dbg(f"webview imported OK (version: {getattr(webview, '__version__', '?')})")
    except Exception:
        _dbg(f"webview import FAILED:\n{traceback.format_exc()}")
        _write_result(output_path, {})
        return

    # ---- cookie extraction helpers ----
    def get_cookies_dict() -> dict:
        """Read all cookies from the webview window."""
        cookies = {}
        try:
            raw = window.get_cookies()
            if raw:
                for cookie in raw:
                    name = getattr(cookie, "name", "")
                    value = getattr(cookie, "value", "")
                    if name and value:
                        cookies[name] = value
        except Exception:
            pass
        try:
            js_str = window.evaluate_js("document.cookie") or ""
            for part in js_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies.setdefault(k.strip(), v.strip())
        except Exception:
            pass
        return cookies

    def try_extract_login_at() -> str | None:
        """Try multiple methods to get the login-at token from the browser."""
        # Method 1: localStorage (Novelpia Global stores it here)
        for key in ("LOGINAT", "login_at", "loginAt", "login-at", "token", "accessToken", "access_token"):
            try:
                val = window.evaluate_js(f"localStorage.getItem('{key}')")
                if val and isinstance(val, str) and len(val) > 10:
                    _dbg(f"Found login-at in localStorage['{key}']: {val[:30]}...")
                    return val
            except Exception:
                pass

        # Method 2: scan all localStorage keys for JWT-like values
        try:
            all_keys = window.evaluate_js(
                "JSON.stringify(Object.keys(localStorage))"
            )
            if all_keys:
                keys = json.loads(all_keys)
                _dbg(f"localStorage keys: {keys}")
                for k in keys:
                    try:
                        v = window.evaluate_js(f"localStorage.getItem('{k}')")
                        if v and isinstance(v, str) and v.count(".") == 2 and len(v) > 50:
                            _dbg(f"Found JWT in localStorage['{k}']: {v[:30]}...")
                            return v
                    except Exception:
                        pass
        except Exception:
            pass

        # Method 3: sessionStorage
        for key in ("LOGINAT", "login_at", "loginAt", "login-at", "token"):
            try:
                val = window.evaluate_js(f"sessionStorage.getItem('{key}')")
                if val and isinstance(val, str) and len(val) > 10:
                    _dbg(f"Found login-at in sessionStorage['{key}']: {val[:30]}...")
                    return val
            except Exception:
                pass

        # Method 4: cookies
        cookies = get_cookies_dict()
        for key in ("LOGINAT", "login_at", "loginAt", "login-at"):
            if key in cookies and len(cookies[key]) > 10:
                _dbg(f"Found login-at in cookie '{key}': {cookies[key][:30]}...")
                return cookies[key]

        # Method 5: intercept XHR by checking if there's an auth header set globally
        try:
            val = window.evaluate_js("""
                (function() {
                    try {
                        // Check common auth patterns in window/global state
                        if (window.__NUXT__ && window.__NUXT__.state) {
                            var s = JSON.stringify(window.__NUXT__.state);
                            var m = s.match(/"LOGINAT":"([^"]+)"/);
                            if (m) return m[1];
                            m = s.match(/"login_at":"([^"]+)"/);
                            if (m) return m[1];
                            m = s.match(/"token":"([^"]+)"/);
                            if (m) return m[1];
                        }
                    } catch(e) {}
                    try {
                        // Pinia/Vuex store
                        if (window.__pinia) {
                            var s = JSON.stringify(window.__pinia);
                            var m = s.match(/"LOGINAT":"([^"]+)"/);
                            if (m) return m[1];
                        }
                    } catch(e) {}
                    return null;
                })()
            """)
            if val and isinstance(val, str) and len(val) > 10:
                _dbg(f"Found login-at in page state: {val[:30]}...")
                return val
        except Exception:
            pass

        return None

    # ---- poll logic ----
    result_holder = {"login_at": None, "userkey": None, "tkey": None}
    was_on_google = False

    def poll_for_login(_window=None) -> None:
        nonlocal was_on_google
        _dbg("poll_for_login started, waiting for page load...")
        time.sleep(3)

        for i in range(900):
            try:
                cur_url = ""
                try:
                    cur_url = window.get_current_url() or ""
                except Exception:
                    pass

                if "accounts.google.com" in cur_url:
                    if not was_on_google:
                        _dbg(f"poll #{i}: navigated to Google OAuth")
                        was_on_google = True

                if was_on_google and "novelpia.com" in cur_url and "accounts.google" not in cur_url:
                    _dbg(f"poll #{i}: redirected back to novelpia: {cur_url[:80]}")
                    # Wait for the page's own JS login flow to complete
                    _dbg("Waiting 8s for page login flow to complete...")
                    time.sleep(8)

                    # Try to extract tokens
                    for attempt in range(10):
                        cookies = get_cookies_dict()
                        userkey = cookies.get("USERKEY", "")
                        tkey = cookies.get("TKEY", "")

                        login_at = try_extract_login_at()

                        if login_at:
                            _dbg(f"Login successful on attempt {attempt+1}!")
                            result_holder["login_at"] = login_at
                            result_holder["userkey"] = userkey
                            result_holder["tkey"] = tkey
                            _write_result(output_path, result_holder)
                            try:
                                window.destroy()
                            except Exception:
                                pass
                            return

                        _dbg(f"Token extraction attempt {attempt+1}/10 - not found yet, waiting 3s...")
                        time.sleep(3)

                    _dbg("All extraction attempts failed, resetting google flag")
                    was_on_google = False

                if i < 5 or (i % 30 == 0):
                    _dbg(f"poll #{i}: url={cur_url[:60] if cur_url else None}, google={was_on_google}")

            except Exception as e:
                if i < 5:
                    _dbg(f"poll #{i} error: {e}")
            time.sleep(1)

        _dbg("poll_for_login timed out after 900s")

    # ---- window sizing ----
    try:
        import ctypes
        user32 = ctypes.windll.user32
        _sw = user32.GetSystemMetrics(0)
        _sh = user32.GetSystemMetrics(1)
    except Exception:
        try:
            import tkinter as _tk
            _r = _tk.Tk()
            _r.withdraw()
            _sw = _r.winfo_screenwidth()
            _sh = _r.winfo_screenheight()
            _r.destroy()
        except Exception:
            _sw, _sh = 1200, 900

    # ---- launch webview ----
    try:
        window = webview.create_window(
            "Novelpia Global — Google Login",
            LOGIN_URL,
            width=int(_sw * 0.6),
            height=int(_sh * 0.7),
        )
        _dbg("window created, calling webview.start()")
        webview.start(poll_for_login, window, debug=False)
        _dbg("webview.start() returned")
    except Exception:
        _dbg(f"webview.start FAILED:\n{traceback.format_exc()}")

    # ---- final attempt after window close ----
    if result_holder["login_at"] is None:
        _dbg("No token captured, writing empty output")
        _write_result(output_path, {})

    _dbg("=== webview login ended ===")


def _write_result(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass
