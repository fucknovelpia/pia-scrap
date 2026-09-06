"""Capture a Novelpia session in an embedded browser after any login method."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from urllib.parse import urlsplit

LOGIN_URL = "https://global.novelpia.com/"
LOGIN_TIMEOUT = 15 * 60

# Each request owns its result object so a delayed response cannot overwrite a
# later request. Abort before the Python polling deadline if the server hangs.
FETCH_REFRESH_JS = r"""
(function() {
    if (window.location.origin !== 'https://global.novelpia.com') return false;
    var state = {done: false, status: 0, data: null};
    window.__pia_refresh_state = state;
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, 8000);
    fetch('https://api-global.novelpia.com/v1/login/refresh', {
        method: 'GET',
        credentials: 'include',
        headers: {'accept': 'application/json'},
        signal: controller.signal
    })
    .then(function(response) {
        state.status = response.status;
        return response.json();
    })
    .then(function(data) { state.data = data; })
    .catch(function() { state.failed = true; })
    .finally(function() { clearTimeout(timer); state.done = true; });
    return true;
})();
"""


def _is_login_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "global.novelpia.com"
            and parsed.port in (None, 443)
        )
    except (TypeError, ValueError):
        return False


def _login_at_from_refresh(state: object) -> str | None:
    """Accept only a successful refresh, never an error body's apparent token."""
    if (
        not isinstance(state, dict)
        or not state.get("done")
        or state.get("status") != 200
        or state.get("failed")
    ):
        return None
    data = state.get("data")
    if not isinstance(data, dict) or str(data.get("statusCode")) != "200":
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    token = result.get("LOGINAT")
    return token.strip() if isinstance(token, str) and token.strip() else None


def _get_auth_cookies(window) -> dict[str, str]:
    """Prefer native cookies, which include HttpOnly cookies hidden from JS."""
    cookies: dict[str, str] = {}
    try:
        for part in (window.evaluate_js("document.cookie") or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name in ("USERKEY", "TKEY"):
                cookies[name] = value
    except Exception:
        pass
    try:
        # pywebview returns a list of http.cookies.SimpleCookie objects.
        for cookie in window.get_cookies() or []:
            for name, morsel in cookie.items():
                if name in ("USERKEY", "TKEY"):
                    cookies[name] = morsel.value
    except Exception:
        # Older engines may not provide a native cookie API.
        pass
    return cookies


def _try_refresh_token(window, stopped: threading.Event, debug) -> str | None:
    try:
        if stopped.is_set() or not window.evaluate_js(FETCH_REFRESH_JS):
            return None
        for _ in range(40):
            if stopped.wait(0.25):
                return None
            state = window.evaluate_js("window.__pia_refresh_state || null")
            if not isinstance(state, dict):
                # Navigation clears the previous page's pending request.
                return None
            if state.get("done"):
                token = _login_at_from_refresh(state)
                if not token:
                    status = state.get("status")
                    safe_status = status if isinstance(status, int) else "unknown"
                    debug(f"Refresh not authenticated (HTTP {safe_status}).")
                return token
        debug("Refresh request timed out; will retry.")
    except Exception as exc:
        # Exceptions/response bodies can contain tokens or OAuth redirect URLs.
        debug(f"Refresh unavailable ({type(exc).__name__}); will retry.")
    return None


def _poll_for_login(window, stopped: threading.Event, debug, timeout: float = LOGIN_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            # Do not require observing a Google redirect: email login, popups,
            # fast redirects and restored sessions can all keep the same URL.
            if window.events.loaded.is_set() and _is_login_origin(window.get_current_url() or ""):
                token = _try_refresh_token(window, stopped, debug)
                if token and not stopped.is_set() and _is_login_origin(window.get_current_url() or ""):
                    cookies = _get_auth_cookies(window)
                    if stopped.is_set():
                        break
                    debug("Login session captured successfully.")
                    return {
                        "status": "success",
                        "login_at": token,
                        "userkey": cookies.get("USERKEY", ""),
                        "tkey": cookies.get("TKEY", ""),
                    }
        except Exception as exc:
            debug(f"Login page unavailable ({type(exc).__name__}); will retry.")
        stopped.wait(min(3.0, max(0.0, deadline - time.monotonic())))
    if stopped.is_set():
        return {"status": "cancelled"}
    return {"status": "timeout", "error": "Login was not completed within 15 minutes."}


def _run_webview_login(output_path: str) -> None:
    from src.const import APP_DIR

    log_dir = os.path.join(str(APP_DIR), "output", "logs")
    debug_path = os.path.join(log_dir, "webview_debug.log")

    def debug(message: str) -> None:
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(debug_path, "a", encoding="utf-8") as log:
                log.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
        except Exception:
            pass

    debug("=== webview login started ===")
    stopped = threading.Event()
    result_lock = threading.Lock()
    finished = False

    def finish(data: dict) -> None:
        nonlocal finished
        with result_lock:
            if not finished:
                _write_result(output_path, data)
                finished = True

    try:
        try:
            from pythonnet import load
            load()
        except Exception:
            pass
        import webview

        # OAuth popup links must stay in this browser's cookie storage. The
        # default sends them to the user's browser, whose session is separate.
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

        try:
            import ctypes
            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:
            screen_width, screen_height = 1200, 900

        storage_dir = os.path.join(str(APP_DIR), ".webview_data")
        os.makedirs(storage_dir, exist_ok=True)
        window = webview.create_window(
            "Novelpia Global -- Login (wait for auto-close)",
            LOGIN_URL,
            width=int(screen_width * 0.6),
            height=int(screen_height * 0.7),
        )

        def on_closed() -> None:
            stopped.set()

        window.events.closed += on_closed

        def poll_for_login() -> None:
            try:
                finish(_poll_for_login(window, stopped, debug))
            finally:
                if not stopped.is_set():
                    try:
                        window.destroy()
                    except Exception:
                        pass

        webview.start(poll_for_login, debug=False, private_mode=False, storage_path=storage_dir)
    except Exception as exc:
        debug(f"Browser login failed ({type(exc).__name__}).")
        finish({
            "status": "error",
            "error": (
                "The login browser could not start. Check that pywebview and the "
                "Microsoft Edge WebView2 Runtime are installed and the application "
                "folder is writable."
            ),
        })
    finally:
        stopped.set()
        finish({"status": "cancelled"})
        debug("=== webview login ended ===")


def _write_result(path: str, data: dict) -> None:
    """Publish a complete payload; the UI must never read half a JSON object."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(os.path.abspath(path)),
            suffix=".loginkey", delete=False,
        ) as result_file:
            temporary_path = result_file.name
            json.dump(data, result_file)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
