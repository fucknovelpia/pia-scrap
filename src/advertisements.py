"""Let the official Novelpia viewer complete its advertisement flow."""
from __future__ import annotations

import multiprocessing
import json
import queue
import threading
import time
import uuid
from typing import Callable
from urllib.parse import urlsplit

import requests

from src import const


class AdvertisementError(RuntimeError):
    """An advertisement could not be completed in the official viewer."""


_HOST_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_VIEWER_HOST = None
_BROWSER_ERROR = (
    "The advertisement browser could not start. Check that pywebview and the "
    "Microsoft Edge WebView2 Runtime are installed and the application folder "
    "is writable."
)


def _log_browser_event(message: str) -> None:
    """Record fixed event descriptions only; never pass browser/request data."""
    try:
        log_dir = const.APP_DIR / "output" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with (log_dir / "advertisements.log").open("a", encoding="utf-8") as log:
                log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _exception_location(exc: Exception) -> str:
    """Keep codes and code locations, excluding exception text and path data."""
    details = [type(exc).__name__]
    for attribute in ("errno", "winerror"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            details.append(f"{attribute}={value}")
    frame = exc.__traceback__
    locations = []
    while frame is not None and len(locations) < 8:
        locations.append(f"{frame.tb_frame.f_code.co_name}:{frame.tb_lineno}")
        frame = frame.tb_next
    if locations:
        details.append("frames=" + ">".join(locations))
    return ", ".join(details)

# This activates only the ordinary Continue control exposed by the provider
# after its own countdown. The website still decides when the ad is finished.
_CONTINUE_AD_JS = r"""
(function() {
    var expectedPath = __EPISODE_PATH__;
    if (window.location.origin !== 'https://global.novelpia.com' ||
        (window.location.pathname !== expectedPath &&
         window.location.pathname !== expectedPath + '/')) return false;
    var button = document.getElementById('ez-rewarded-continue-button');
    if (!button || button.tagName !== 'BUTTON' || button.__piaAdContinueClicked ||
        button.disabled || button.matches(':disabled') ||
        button.getAttribute('aria-disabled') === 'true' ||
        button.textContent.trim() !== 'Continue' ||
        button.getClientRects().length === 0) return false;
    var style = window.getComputedStyle(button);
    var rect = button.getBoundingClientRect();
    if (style.display === 'none' || style.visibility !== 'visible' ||
        Number(style.opacity) === 0 || rect.width <= 0 || rect.height <= 0 ||
        rect.bottom <= 0 || rect.right <= 0 ||
        rect.top >= window.innerHeight || rect.left >= window.innerWidth) return false;
    var x = (Math.max(0, rect.left) + Math.min(window.innerWidth, rect.right)) / 2;
    var y = (Math.max(0, rect.top) + Math.min(window.innerHeight, rect.bottom)) / 2;
    var visibleTarget = document.elementFromPoint(x, y);
    if (visibleTarget !== button && !button.contains(visibleTarget)) return false;
    button.__piaAdContinueClicked = true;
    button.click();
    return true;
})();
"""


def _continue_finished_ad(window, episode_no: int) -> bool:
    """Click the provider's visible, enabled Continue button on this viewer."""
    try:
        if not window.events.loaded.is_set():
            return False
        url = urlsplit(window.get_current_url() or "")
        expected_path = f"/viewer/{episode_no}"
        if (
            url.scheme != "https" or url.hostname != "global.novelpia.com"
            or url.port not in (None, 443) or url.username or url.password
            or url.path not in (expected_path, expected_path + "/")
        ):
            return False
        return window.evaluate_js(
            _CONTINUE_AD_JS.replace("__EPISODE_PATH__", json.dumps(expected_path))
        ) is True
    except Exception:
        # Navigation can interrupt the check. Retry without logging page data.
        return False


def _run_ad_host(stop, commands, statuses) -> None:
    """Host independent viewer windows together in one persistent profile."""
    parent = multiprocessing.parent_process()
    windows = {}
    windows_lock = threading.Lock()
    _log_browser_event("Advertisement host started.")
    try:
        try:
            from pythonnet import load
            load()
        except Exception as exc:
            _log_browser_event(f"Runtime load returned {type(exc).__name__}.")
        import webview
        _log_browser_event("Browser runtime imported.")

        # A required OAuth sign-in must stay in this browser's saved profile,
        # just as it does in the dedicated login window.
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

        storage_dir = const.APP_DIR / ".webview_data"
        storage_dir.mkdir(parents=True, exist_ok=True)
        # Retain the GUI loop while registered workers open/close their windows.
        # This prevents the last closing window racing with the next open command.
        anchor = webview.create_window(
            "PIA advertisement host", html="<html><body></body></html>", hidden=True,
        )
        _log_browser_event("Hidden host window created.")

        def destroy(window) -> None:
            try:
                window.destroy()
            except Exception:
                pass

        def open_viewer(gate_id: str, episode_no: int) -> None:
            window_closed = threading.Event()
            _log_browser_event(f"Opening episode {episode_no}.")
            try:
                window = webview.create_window(
                    f"Novelpia -- Advertisement for episode {episode_no}",
                    f"{const.BASE_URL}/viewer/{episode_no}",
                    width=1000, height=780,
                )

                def on_closed() -> None:
                    _log_browser_event(f"Closed episode {episode_no}.")
                    window_closed.set()
                    with windows_lock:
                        windows.pop(gate_id, None)
                    statuses.put(("closed", gate_id))

                def on_loaded() -> None:
                    _log_browser_event(f"Loaded episode {episode_no}.")
                    statuses.put(("ready", gate_id))

                window.events.closed += on_closed
                window.events.loaded += on_loaded
                with windows_lock:
                    windows[gate_id] = (window, window_closed)

                def continue_when_ready() -> None:
                    while not window_closed.wait(0.5):
                        if stop.is_set():
                            return
                        # Edge's JS bridge can wait indefinitely if this renderer
                        # fails. Isolate it so other viewers and shutdown proceed.
                        if _continue_finished_ad(window, episode_no):
                            _log_browser_event(f"Pressed finished-ad Continue for episode {episode_no}.")

                threading.Thread(
                    target=continue_when_ready, name=f"PIA ad Continue {episode_no}",
                    daemon=True,
                ).start()
            except Exception as exc:
                _log_browser_event(f"Opening episode {episode_no} failed ({type(exc).__name__}).")
                window_closed.set()
                statuses.put(("error", gate_id))

        def dispatch() -> None:
            _log_browser_event("Window dispatcher started.")
            operation = "starting dispatcher"
            try:
                while True:
                    operation = "checking stop request"
                    if stop.is_set():
                        _log_browser_event("Host stop requested.")
                        break
                    operation = "checking download parent"
                    if parent is not None and not parent.is_alive():
                        _log_browser_event("Download parent exited.")
                        break
                    try:
                        operation = "reading window command"
                        command = commands.get(timeout=0.2)
                    except queue.Empty:
                        command = None
                    if command:
                        operation = "dispatching window command"
                        action, gate_id, episode_no = command
                        if action == "open":
                            open_viewer(gate_id, episode_no)
                        elif action == "close":
                            _log_browser_event("Closing completed or cancelled viewer.")
                            with windows_lock:
                                entry = windows.pop(gate_id, None)
                            if entry is not None:
                                entry[1].set()
                                destroy(entry[0])
            except Exception as exc:
                _log_browser_event(f"Window dispatcher failed while {operation} ({_exception_location(exc)}).")
                statuses.put(("error", None))
            finally:
                stop.set()
                with windows_lock:
                    current_windows = list(windows.values())
                    windows.clear()
                for window, window_closed in current_windows:
                    window_closed.set()
                    destroy(window)
                destroy(anchor)
                _log_browser_event("Window dispatcher finished.")

        _log_browser_event("Browser GUI loop starting.")
        webview.start(
            dispatch, debug=False, private_mode=False,
            storage_path=str(storage_dir),
        )
        _log_browser_event("Browser GUI loop returned.")
    except Exception as exc:
        _log_browser_event(f"Advertisement host failed ({type(exc).__name__}).")
        # Native browser errors may include page URLs or session information.
        # Only fixed status strings cross the process boundary.
        statuses.put(("error", None))
    finally:
        _log_browser_event("Advertisement host finished.")
        statuses.put(("closed", None))


def _stop_viewer(process, stop) -> None:
    stop.set()
    process.join(timeout=3.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
    if not process.is_alive():
        process.close()


def _close_queue(channel) -> None:
    channel.close()
    channel.cancel_join_thread()


class _AdvertisementHost:
    def __init__(self):
        self.gates = {}
        self.process = None
        self.commands = None
        self.statuses = None
        self.closed = False
        started = False
        try:
            context = multiprocessing.get_context("spawn")
            self.stop = context.Event()
            self.commands = context.Queue()
            self.statuses = context.Queue()
            self.process = context.Process(
                target=_run_ad_host, args=(self.stop, self.commands, self.statuses),
                name="PIA advertisement host", daemon=True,
            )
            self.process.start()
            started = True
        except Exception:
            if started:
                _stop_viewer(self.process, self.stop)
            elif self.process is not None:
                self.process.close()
            for channel in (self.commands, self.statuses):
                if channel is not None:
                    _close_queue(channel)
            raise AdvertisementError(_BROWSER_ERROR) from None

    def status(self, gate_id: str) -> str:
        # Caller holds _HOST_LOCK; one consumer distributes statuses to gates.
        while True:
            try:
                status, target = self.statuses.get_nowait()
            except queue.Empty:
                break
            targets = list(self.gates) if target is None else (target,)
            for gate in targets:
                if gate in self.gates and self.gates[gate] not in ("error", "closed"):
                    self.gates[gate] = status
        status = self.gates[gate_id]
        if status not in ("error", "closed") and not self.process.is_alive():
            return "exited"
        return status

    def shutdown(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                _stop_viewer(self.process, self.stop)
            finally:
                _close_queue(self.commands)
                _close_queue(self.statuses)


def _register_viewer(episode_no: int, cancelled: threading.Event):
    global _VIEWER_HOST
    while not _HOST_LOCK.acquire(timeout=0.2):
        if cancelled.is_set():
            raise AdvertisementError("Advertisement viewing was cancelled.")
    try:
        if cancelled.is_set():
            raise AdvertisementError("Advertisement viewing was cancelled.")
        if _VIEWER_HOST is None or not _VIEWER_HOST.process.is_alive():
            _VIEWER_HOST = _AdvertisementHost()
        host = _VIEWER_HOST
        gate_id = uuid.uuid4().hex
        host.gates[gate_id] = "opening"
        host.commands.put(("open", gate_id, episode_no))
        return host, gate_id
    finally:
        _HOST_LOCK.release()


def _unregister_viewer(host, gate_id: str) -> None:
    global _VIEWER_HOST
    with _HOST_LOCK:
        if gate_id in host.gates:
            host.gates.pop(gate_id)
            host.commands.put(("close", gate_id, None))
        if not host.gates:
            # Finish using the profile before another caller starts a new host.
            if _VIEWER_HOST is host:
                _VIEWER_HOST = None
            host.shutdown()


def watch_episode_ad(
    episode_no: int,
    probe: Callable[[], requests.Response],
    cancelled: threading.Event,
    is_unlocked: Callable[[requests.Response], bool],
    *,
    timeout: float = 300.0,
    poll_interval: float = 3.0,
) -> requests.Response:
    """Open the real viewer and return only a server-confirmed episode ticket.

    A loaded page, elapsed ad timer or closed window never proves completion.
    ``probe`` must request the episode ticket normally; ``is_unlocked`` must
    validate both the HTTP status and the successful ticket response body.
    """
    if not isinstance(episode_no, int) or isinstance(episode_no, bool) or episode_no <= 0:
        raise ValueError("Episode number must be a positive integer.")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("Advertisement timeout and polling interval must be positive.")

    def check_cancelled() -> None:
        if cancelled.is_set():
            raise AdvertisementError("Advertisement viewing was cancelled.")

    def unlocked_ticket():
        check_cancelled()
        try:
            response = probe()
        except requests.RequestException:
            # A temporary network failure is not evidence of an unlock.
            check_cancelled()
            return None
        check_cancelled()
        return response if is_unlocked(response) else None

    # A preceding ad may already have cleared this episode's gate.
    response = unlocked_ticket()
    if response is not None:
        return response
    host, gate_id = _register_viewer(episode_no, cancelled)
    try:
        deadline = time.monotonic() + timeout
        next_probe = time.monotonic() + poll_interval
        while True:
            check_cancelled()
            with _HOST_LOCK:
                status = host.status(gate_id)
            if status == "error":
                raise AdvertisementError(_BROWSER_ERROR)
            if time.monotonic() >= next_probe or status in ("closed", "exited"):
                response = unlocked_ticket()
                if response is not None:
                    return response
                next_probe = time.monotonic() + poll_interval
            if status == "closed":
                raise AdvertisementError(
                    "The advertisement window closed before Novelpia confirmed "
                    "that the episode was unlocked. Retry and let the ad finish."
                )
            if status == "exited":
                exit_code = host.process.exitcode
                safe_code = str(exit_code) if isinstance(exit_code, int) else "unknown"
                raise AdvertisementError(
                    "The advertisement browser exited unexpectedly "
                    f"(exit code {safe_code}). See output/logs/advertisements.log."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdvertisementError(
                    "Novelpia did not confirm the advertisement within "
                    f"{timeout:g} seconds. In the viewer, sign in to the same "
                    "account and allow the advertisement to finish, then retry."
                )
            cancelled.wait(min(0.2, remaining))
    finally:
        _unregister_viewer(host, gate_id)
