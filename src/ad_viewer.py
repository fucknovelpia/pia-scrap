"""Protect the ad viewer and receive the chapter it legitimately loads."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from src import const
from src.ad_navigation import (
    DEFAULT_AD_RETRIES, DEFAULT_AD_RETRY_COOLDOWN, validate_ad_retry_settings,
)


_SETUP_TIMEOUT = 20.0
_RESPONSE_TIMEOUT = 30.0
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CONTENT_KEY = re.compile(r"epi_content(?:[2-9][0-9]*)?\Z")

# Installed before navigation, so this also covers the site's post-ad reload.
# These are the reader's text containers, not the advertisement overlay.
_SPOILER_GUARD_JS = r"""
(() => {
    if (window.top !== window ||
        location.origin !== 'https://global.novelpia.com' ||
        (location.pathname !== __EPISODE_PATH__ &&
         location.pathname !== __EPISODE_PATH__ + '/')) return;
    const id = '__pia_ad_reader_guard';
    const style = document.createElement('style');
    style.id = id;
    style.textContent = `
        #book-content, #book-content *,
        .viewer-ep-tit, .viewer-ep-tit *,
        .viewer-contents .break-words.whitespace-pre-line,
        .viewer-contents .break-words.whitespace-pre-line * {
            visibility: hidden !important;
        }
    `;
    const protect = () => {
        if (document.documentElement && !document.getElementById(id)) {
            document.documentElement.prepend(style);
        }
    };
    new MutationObserver(protect).observe(document, {childList: true, subtree: true});
    protect();
})();
"""

# Nuxt renders the initial ticket into its HTML payload. Reading that existing
# server response avoids another ticket request, which would race the viewer.
_SSR_TICKET_JS = r"""
(() => {
    if (window.top !== window ||
        location.origin !== 'https://global.novelpia.com' ||
        (location.pathname !== __EPISODE_PATH__ &&
         location.pathname !== __EPISODE_PATH__ + '/')) return null;
    const key = 'viewer_' + __EPISODE_NO__;
    const app = document.querySelector('#__nuxt')?.__vue_app__;
    const candidates = [
        window.__NUXT__?.data?.[key],
        app?.$nuxt?.payload?.data?.[key],
        app?.config?.globalProperties?.$nuxt?.payload?.data?.[key]
    ];
    for (const ticket of candidates) {
        if (ticket && String(ticket.statusCode) === '200' &&
            String(ticket.result?.data?.episode_no) === String(__EPISODE_NO__) &&
            typeof ticket.result?._t === 'string' && ticket.result._t.trim()) {
            return ticket;
        }
    }
    for (const ticket of candidates) {
        if (ticket && ticket.statusCode != null && String(ticket.statusCode) !== '200') {
            const code = String(ticket.code || '');
            if (code === '0010' || code === '0008') return {__pia_viewer_state: 'ad'};
            if (code === '0001' && ticket.result?.name === 'AUTH_ERROR') {
                return {__pia_viewer_state: 'interactive'};
            }
            // Expose only state codes, never account/error messages or URLs.
            return {__pia_viewer_state: 'error',
                status: Number(ticket.statusCode) || 0,
                code: /^\d{1,6}$/.test(code) ? code : ''};
        }
    }
    return null;
})();
"""


def _viewer_url_matches(url: str, episode_no: int) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https" and parsed.hostname == "global.novelpia.com"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and parsed.path in (f"/viewer/{episode_no}", f"/viewer/{episode_no}/")
        )
    except (TypeError, ValueError):
        return False


def _authentication_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
            return False
        return parsed.hostname == "accounts.google.com" or (
            parsed.hostname == "global.novelpia.com"
            and any(part in ("login", "signin", "sign-in", "auth", "oauth") for part in parsed.path.lower().split("/"))
        )
    except (TypeError, ValueError):
        return False


def _response_kind(url: str, episode_no: int):
    """Recognize only this episode's actual first-party API requests."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.hostname != "api-global.novelpia.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
        ):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/v1/novel/episode" and query.get("episode_no") == [str(episode_no)]:
            return "ticket", None
        tokens = query.get("_t", [])
        if parsed.path == "/v1/novel/episode/content" and len(tokens) == 1 and tokens[0]:
            return "content", tokens[0]
    except (TypeError, ValueError):
        pass
    return None


def _success_data(body):
    if not isinstance(body, dict) or str(body.get("statusCode")) != "200":
        return None
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        return None
    if str(body.get("code", "")) == "0010" or str(result.get("code", "")) == "0010":
        return None
    return result["data"]


def _ticket_token(body, episode_no: int):
    data = _success_data(body)
    if data is None or str(data.get("episode_no")) != str(episode_no):
        return None
    token = body["result"].get("_t")
    return token if isinstance(token, str) and token.strip() and len(token) <= 32768 else None


def _valid_content(body, episode_no: int) -> bool:
    data = _success_data(body)
    if data is None:
        return False
    if "episode_no" in data and str(data["episode_no"]) != str(episode_no):
        return False
    return any(
        isinstance(key, str) and _CONTENT_KEY.fullmatch(key)
        and isinstance(value, str) and value.strip()
        for key, value in data.items()
    )


def validate_handoff(payload, episode_no: int) -> bool:
    """Check chapter identity and server-success bodies across the IPC boundary."""
    return (
        isinstance(episode_no, int) and not isinstance(episode_no, bool) and episode_no > 0
        and isinstance(payload, dict) and type(payload.get("episode_no")) is int
        and payload.get("episode_no") == episode_no
        and _ticket_token(payload.get("ticket"), episode_no) is not None
        and _valid_content(payload.get("content"), episode_no)
    )


class _ResponseHandoff:
    """Pair read-only response copies despite asynchronous body-read ordering."""

    def __init__(self, episode_no: int, on_complete: Callable[[dict], None]):
        self.episode_no = episode_no
        self.on_complete = on_complete
        self._lock = threading.Lock()
        self._tickets = {}
        self._contents = {}
        self.completed = threading.Event()

    def receive(self, url: str, status: int, body) -> None:
        kind = _response_kind(url, self.episode_no)
        if kind is None or status != 200 or self.completed.is_set():
            return
        response_type, token = kind
        if response_type == "ticket":
            token = _ticket_token(body, self.episode_no)
            if token is None:
                return
        elif not _valid_content(body, self.episode_no):
            return
        payload = None
        with self._lock:
            if self.completed.is_set():
                return
            destination = self._tickets if response_type == "ticket" else self._contents
            destination[token] = body
            # Each viewer needs one pair; bound temporary data during reloads.
            while len(destination) > 8:
                destination.pop(next(iter(destination)))
            if token in self._tickets and token in self._contents:
                payload = {
                    "episode_no": self.episode_no,
                    "ticket": self._tickets[token],
                    "content": self._contents[token],
                }
                self.completed.set()
                self._tickets.clear()
                self._contents.clear()
        if payload is not None:
            self.on_complete(payload)


def _wait_task(task, timeout: float, cancelled: threading.Event):
    """Wait outside the native GUI thread, with an explicit deadline."""
    deadline = time.monotonic() + timeout
    while not task.IsCompleted:
        if cancelled.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
            raise RuntimeError("Viewer closed")
        if time.monotonic() >= deadline:
            raise TimeoutError("Browser operation timed out")
    if cancelled.is_set():
        raise RuntimeError("Viewer closed")
    if task.IsFaulted or task.IsCanceled:
        raise RuntimeError("Browser operation failed")
    return task.Result


def _read_response(task, cancelled: threading.Event):
    """Read the browser's response copy, without requesting the URL again."""
    from System import Array, Byte

    deadline = time.monotonic() + _RESPONSE_TIMEOUT
    stream = _wait_task(task, _RESPONSE_TIMEOUT, cancelled)
    if stream is None:
        return None
    try:
        chunks = []
        size = 0
        buffer = Array.CreateInstance(Byte, 8192)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response read timed out")
            count = int(_wait_task(stream.ReadAsync(buffer, 0, len(buffer)), remaining, cancelled))
            if not count:
                return json.loads(b"".join(chunks).decode("utf-8-sig"))
            size += count
            if size > _MAX_RESPONSE_BYTES:
                raise ValueError("Response too large")
            chunks.append(bytes(buffer)[:count])
    finally:
        stream.Dispose()


def install_viewer_handoff(
    window,
    episode_no: int,
    on_complete: Callable[[dict], None],
    on_error: Callable[[], None],
    on_diagnostic: Callable[[str], None] | None = None,
    on_continue: Callable[[], None] | None = None,
    on_navigation_status: Callable[[str], None] | None = None,
    *,
    max_retries: int = DEFAULT_AD_RETRIES,
    retry_cooldown: float = DEFAULT_AD_RETRY_COOLDOWN,
) -> None:
    """Prepare a neutral hidden Edge window before loading the real viewer.

    Call on a per-window daemon thread. Native setup uses bounded UI dispatch,
    and the document-start registration must finish before showing/navigating.
    The observer only copies successful responses already made by the website.
    """
    max_retries, retry_cooldown = validate_ad_retry_settings(max_retries, retry_cooldown)
    stopped = threading.Event()
    failed = threading.Event()
    collector = _ResponseHandoff(episode_no, on_complete)
    registered = {}
    bridge_token = secrets.token_urlsafe(32)
    continued_clicks = set()
    navigation = {"visited_viewer": False, "failed": False, "loading": False,
                  "awaiting_start": False, "id": None}

    def diagnostic(message: str) -> None:
        if on_diagnostic is not None:
            try:
                on_diagnostic(message)
            except Exception:
                pass

    def diagnostic_error(stage: str, error: Exception) -> None:
        # Report only code locations and exception types, never messages or
        # locals: native exceptions can include response URLs or credentials.
        frame = error.__traceback__
        locations = []
        while frame is not None and len(locations) < 6:
            locations.append(f"{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}")
            frame = frame.tb_next
        diagnostic(f"{stage} failed ({type(error).__name__}; frames={' > '.join(locations)}).")

    def fail(*, page_load=False) -> None:
        if not failed.is_set() and not stopped.is_set() and not collector.completed.is_set():
            failed.set()
            stopped.set()
            if page_load and on_navigation_status is not None:
                on_navigation_status("load_error")
            else:
                on_error()

    def on_closed() -> None:
        stopped.set()

    window.events.closed += on_closed
    try:
        if not isinstance(episode_no, int) or isinstance(episode_no, bool) or episode_no <= 0:
            raise ValueError("Episode number must be positive")
        if not window.events.loaded.wait(_SETUP_TIMEOUT) or window.events.closed.is_set():
            raise RuntimeError("Neutral viewer did not initialize")

        from System import Action

        native = window.native

        def on_ui(callback):
            done = threading.Event()
            abandoned = threading.Event()
            outcome = {}

            def execute():
                try:
                    if abandoned.is_set():
                        return
                    if stopped.is_set():
                        raise RuntimeError("Viewer closed")
                    outcome["value"] = callback()
                except Exception as exc:
                    diagnostic_error("Native UI callback", exc)
                    outcome["failed"] = True
                finally:
                    done.set()

            native.BeginInvoke(Action(execute))
            if not done.wait(_SETUP_TIMEOUT):
                abandoned.set()
                raise RuntimeError("Native viewer setup timed out")
            if outcome.get("failed"):
                raise RuntimeError("Native viewer setup failed")
            return outcome.get("value")

        def on_response(sender, args):
            if stopped.is_set() or collector.completed.is_set():
                return
            try:
                url = str(args.Request.Uri)
                status = int(args.Response.StatusCode)
                kind = _response_kind(url, episode_no)
                if kind is None:
                    return
                page_matches = _viewer_url_matches(str(sender.Source), episode_no)
                diagnostic(f"Candidate {kind[0]} response: status={status}, viewer_match={page_matches}.")
                if page_matches and kind[0] == "content" and status >= 400:
                    navigation["failed"] = True
                if status != 200 or not page_matches:
                    return
                # GetContentAsync must start on the GUI thread. Its returned
                # stream is thread-safe, and reading it does not consume the
                # website's own response or repeat a one-use content request.
                task = args.Response.GetContentAsync()

                def read():
                    try:
                        body = _read_response(task, stopped)
                        if not stopped.is_set():
                            collector.receive(url, status, body)
                            valid = (
                                _ticket_token(body, episode_no) is not None
                                if kind[0] == "ticket" else _valid_content(body, episode_no)
                            )
                            diagnostic(
                                f"Read {kind[0]} response: json_object={isinstance(body, dict)}, "
                                f"valid={valid}, completed={collector.completed.is_set()}."
                            )
                    except Exception as exc:
                        # Other responses/reloads may still produce the pair.
                        # Never log response content, URLs, tokens, or errors.
                        diagnostic_error(f"Reading {kind[0]} response", exc)

                threading.Thread(target=read, name="PIA viewer response", daemon=True).start()
            except Exception as exc:
                diagnostic_error("Response observer", exc)

        def continue_message(args):
            try:
                raw = str(args.get_WebMessageAsJson())
                if len(raw) > 8192:
                    return None
                message = json.loads(raw)
                if isinstance(message, dict) and message.get("type") == "pia-ad-continue":
                    return message
            except Exception:
                pass
            return None

        def on_message(sender, args):
            if stopped.is_set() or collector.completed.is_set():
                return
            try:
                message = continue_message(args)
                if message is None or not _viewer_url_matches(str(args.Source), episode_no):
                    return
                if not _viewer_url_matches(str(sender.Source), episode_no):
                    return
                token = message.get("token")
                click_id = message.get("click_id")
                if (
                    type(message.get("episode_no")) is not int
                    or message["episode_no"] != episode_no
                    or not isinstance(token, str) or not secrets.compare_digest(token, bridge_token)
                    or not isinstance(click_id, str) or not 1 <= len(click_id) <= 128
                    or click_id in continued_clicks
                ):
                    return
                continued_clicks.add(click_id)
                if on_continue is not None:
                    on_continue()
            except Exception as exc:
                diagnostic_error("Continue message observer", exc)

        def prepare():
            from src.ad_continue import CONTINUE_AD_JS

            core = native.webview.CoreWebView2
            if core is None:
                raise RuntimeError("Edge WebView2 is unavailable")
            core.WebResourceResponseReceived += on_response
            core.WebMessageReceived += on_message
            def navigation_start(sender, args):
                if stopped.is_set() or collector.completed.is_set():
                    return
                matches = _viewer_url_matches(str(args.Uri), episode_no)
                navigation["id"] = getattr(args, "NavigationId", None)
                navigation["loading"] = True
                navigation["awaiting_start"] = False
                navigation["failed"] = False
                diagnostic(f"Viewer navigation started: viewer_match={matches}.")

            def navigation_complete(sender, args):
                if stopped.is_set() or collector.completed.is_set():
                    return
                if navigation["awaiting_start"] or (
                    navigation["id"] is not None
                    and getattr(args, "NavigationId", None) != navigation["id"]
                ):
                    return
                navigation["loading"] = False
                matches = _viewer_url_matches(str(sender.Source), episode_no)
                success = bool(args.IsSuccess)
                if matches and success:
                    navigation["visited_viewer"] = True
                # Replaced/superseded navigations are ordinary browser events.
                cancelled = str(args.WebErrorStatus) == "OperationCanceled"
                if not success and not cancelled:
                    navigation["failed"] = True
                diagnostic(f"Viewer navigation finished: viewer_match={matches}, success={success}.")

            core.NavigationStarting += navigation_start
            core.NavigationCompleted += navigation_complete
            registered["core"] = core
            registered["response_handler"] = on_response
            registered["message_handler"] = on_message
            registered["navigation_start_handler"] = navigation_start
            registered["navigation_complete_handler"] = navigation_complete
            # pywebview's normal bridge expects a three-element JSON array.
            # Preserve it for its own messages and keep our native notification
            # objects out of that parser (and its exception logger).
            stock_handler = getattr(getattr(native, "browser", None), "on_script_notify", None)
            if stock_handler is not None:
                def forward_stock_message(sender, args):
                    if continue_message(args) is None:
                        stock_handler(sender, args)

                native.webview.WebMessageReceived -= stock_handler
                native.webview.WebMessageReceived += forward_stock_message
                registered["stock_message_forwarder"] = forward_stock_message
            script = (
                _SPOILER_GUARD_JS + "\n" + CONTINUE_AD_JS
            ).replace("__EPISODE_PATH__", json.dumps(f"/viewer/{episode_no}"))
            script = script.replace("__EPISODE_NO__", str(episode_no))
            script = script.replace("__BRIDGE_TOKEN__", json.dumps(bridge_token))
            return core.AddScriptToExecuteOnDocumentCreatedAsync(script)

        task = on_ui(prepare)
        registered["script_id"] = _wait_task(task, _SETUP_TIMEOUT, stopped)
        diagnostic("Response observer and document-start guard ready.")

        viewer_url = f"{const.BASE_URL}/viewer/{episode_no}"
        replace_script = f"window.location.replace({json.dumps(viewer_url)});"

        def replace_viewer():
            if stopped.is_set() or collector.completed.is_set():
                return None
            # Do not retain NavigateToString's preparation page in history:
            # the site's error handler can navigate back after an ad reload.
            window.real_url = viewer_url
            navigation["failed"] = False
            navigation["loading"] = True
            navigation["awaiting_start"] = True
            return registered["core"].ExecuteScriptAsync(replace_script)

        def navigate():
            from System.Windows.Forms import FormWindowState

            # Let the hidden neutral page lay out at normal size first. Creating
            # an already-minimized WebView gives it a tiny, clipped viewport.
            # Minimize before revealing, and avoid pywebview's show(), which
            # also calls Activate(). The taskbar can restore it normally.
            native.WindowState = FormWindowState.Minimized
            native.Show()
            return replace_viewer()

        navigation_task = on_ui(navigate)
        if navigation_task is not None:
            _wait_task(navigation_task, _SETUP_TIMEOUT, stopped)

        def read_hydrated_ticket():
            from src.ad_navigation import ViewerNavigationWatchdog

            seen_tokens = set()
            watchdog = ViewerNavigationWatchdog(
                time.monotonic(), max_retries=max_retries, retry_delay=retry_cooldown,
            )
            prior_state = None
            script = (
                _SSR_TICKET_JS.replace("__EPISODE_PATH__", json.dumps(f"/viewer/{episode_no}"))
                .replace("__EPISODE_NO__", str(episode_no))
            )
            while not stopped.is_set() and not collector.completed.is_set():
                try:
                    body = None
                    def execute():
                        core = registered["core"]
                        if not _viewer_url_matches(str(core.Source), episode_no):
                            neutral = str(core.Source) in ("", "about:blank")
                            if navigation["failed"]:
                                state = "error"
                            elif _authentication_url(str(core.Source)):
                                state = "interactive"
                            else:
                                state = "neutral" if neutral and navigation["visited_viewer"] and not navigation["loading"] else "loading"
                            return None, state, navigation["loading"]
                        return core.ExecuteScriptAsync(script), "error" if navigation["failed"] else "loading", navigation["loading"]

                    task, state, loading = on_ui(execute)
                    if task is not None:
                        result = _wait_task(task, _SETUP_TIMEOUT, stopped)
                        body = json.loads(str(result))
                        token = _ticket_token(body, episode_no)
                        if token is not None:
                            # A failed content response still needs page recovery;
                            # a successful ticket alone does not prove delivery.
                            state = "error" if state == "error" else "ticket"
                        elif isinstance(body, dict) and state != "error":
                            marker = body.get("__pia_viewer_state", "loading")
                            # Until the new navigation finishes, an error can
                            # still belong to the document being replaced.
                            state = marker if not loading or marker in ("ad", "interactive") else "loading"
                        if token is not None and token not in seen_tokens:
                            seen_tokens.add(token)
                            # This URL identifies the existing SSR ticket to
                            # the collector; it is never requested over HTTP.
                            collector.receive(
                                f"{const.API_BASE}/v1/novel/episode?episode_no={episode_no}",
                                200, body,
                            )
                            diagnostic(
                                "Read hydrated server ticket: valid=True, "
                                f"completed={collector.completed.is_set()}."
                            )
                    if stopped.is_set() or collector.completed.is_set():
                        return
                    if state != prior_state:
                        diagnostic(f"Viewer state: {state}.")
                        if isinstance(body, dict) and body.get("__pia_viewer_state") == "error":
                            status_code = str(body.get("status", ""))
                            error_code = str(body.get("code", ""))
                            status_code = status_code if re.fullmatch(r"\d{3}", status_code) else "unknown"
                            error_code = error_code if re.fullmatch(r"\d{1,6}", error_code) else "unknown"
                            diagnostic(f"Viewer server status: {status_code}, code={error_code}.")
                        prior_state = state
                    action = watchdog.observe(state, time.monotonic())
                    if action == "failed":
                        diagnostic("Viewer page failed after bounded navigation retries.")
                        fail(page_load=True)
                        return
                    if action == "retry":
                        diagnostic("Retrying official viewer after a page loading failure.")
                        if on_navigation_status is not None:
                            on_navigation_status("retrying")
                        navigation_task = on_ui(replace_viewer)
                        if navigation_task is not None:
                            _wait_task(navigation_task, _SETUP_TIMEOUT, stopped)
                except Exception as exc:
                    if not stopped.is_set() and not collector.completed.is_set():
                        diagnostic_error("Reading hydrated server ticket", exc)
                        action = watchdog.observe("error", time.monotonic())
                        if action == "failed":
                            fail(page_load=True)
                            return
                        if action == "retry":
                            try:
                                if on_navigation_status is not None:
                                    on_navigation_status("retrying")
                                navigation_task = on_ui(replace_viewer)
                                if navigation_task is not None:
                                    _wait_task(navigation_task, _SETUP_TIMEOUT, stopped)
                            except Exception as retry_error:
                                diagnostic_error("Retrying viewer navigation", retry_error)
                if stopped.wait(0.5):
                    return

        threading.Thread(
            target=read_hydrated_ticket, name="PIA viewer server ticket", daemon=True,
        ).start()
    except Exception as exc:
        # Fail closed: the starting window contains only neutral HTML, and no
        # real viewer navigation happens before its guard has been installed.
        diagnostic_error("Viewer handoff setup", exc)
        fail()
