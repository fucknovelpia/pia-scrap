"""Let the official Novelpia viewer complete its advertisement flow."""
from __future__ import annotations

import multiprocessing
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import requests

from src import const


class AdvertisementError(RuntimeError):
    """An advertisement could not be completed in the official viewer."""


@dataclass(frozen=True)
class AdvertisementResult:
    """Ticket and content already delivered to the authorized official viewer."""

    ticket: dict
    content: dict


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
        from src.ad_viewer import install_viewer_handoff
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
                    html="<html><body>Preparing advertisement...</body></html>",
                    width=480, height=540, min_size=(420, 480), hidden=True,
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

                def on_complete(payload: dict) -> None:
                    # Deliver the actual browser responses before closing. The
                    # viewer may have consumed a one-use unlock already.
                    statuses.put(("complete", gate_id, payload))
                    _log_browser_event(f"Received viewer content for episode {episode_no}.")
                    try:
                        window.hide()
                    except Exception:
                        pass

                def on_error() -> None:
                    if not window_closed.is_set() and not stop.is_set():
                        _log_browser_event(f"Viewer handoff setup failed for episode {episode_no}.")
                        statuses.put(("error", gate_id))

                def on_continue() -> None:
                    statuses.put(("continued", gate_id))
                    _log_browser_event(f"Auto-clicked Continue for episode {episode_no}.")

                def on_navigation_status(status: str) -> None:
                    if status in ("retrying", "load_error"):
                        statuses.put((status, gate_id))

                def prepare_viewer() -> None:
                    try:
                        install_viewer_handoff(
                            window, episode_no, on_complete, on_error,
                            lambda message: _log_browser_event(f"Episode {episode_no}: {message}"),
                            on_continue=on_continue,
                            on_navigation_status=on_navigation_status,
                        )
                    except Exception:
                        on_error()
                        return
                threading.Thread(
                    target=prepare_viewer, name=f"PIA ad setup {episode_no}",
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
        # Report native failures with fixed status strings only.
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
        self.results = {}
        self.continue_counts = {}
        self.recovery_counts = {}
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
                event = self.statuses.get_nowait()
            except queue.Empty:
                break
            status, target = event[:2]
            targets = list(self.gates) if target is None else (target,)
            for gate in targets:
                if gate not in self.gates:
                    continue
                if status == "continued" and target is not None:
                    self.continue_counts[gate] = self.continue_counts.get(gate, 0) + 1
                elif status == "retrying" and target is not None:
                    self.recovery_counts[gate] = self.recovery_counts.get(gate, 0) + 1
                elif status == "complete" and target is not None and len(event) == 3:
                    self.results[gate] = event[2]
                    self.gates[gate] = "complete"
                elif self.gates[gate] not in ("complete", "error", "load_error", "closed"):
                    self.gates[gate] = status
        status = self.gates[gate_id]
        if status not in ("complete", "error", "load_error", "closed") and not self.process.is_alive():
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
        host.continue_counts[gate_id] = 0
        host.recovery_counts[gate_id] = 0
        host.commands.put(("open", gate_id, episode_no))
        return host, gate_id
    finally:
        _HOST_LOCK.release()


def _unregister_viewer(host, gate_id: str) -> None:
    global _VIEWER_HOST
    with _HOST_LOCK:
        if gate_id in host.gates:
            host.gates.pop(gate_id)
            host.results.pop(gate_id, None)
            host.continue_counts.pop(gate_id, None)
            host.recovery_counts.pop(gate_id, None)
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
    poll_interval: float = 0.2,
) -> requests.Response | AdvertisementResult:
    """Return a pre-existing ticket or the official viewer's authorized content.

    A loaded page, elapsed ad timer or closed window never proves completion.
    Once a viewer opens, it owns the ticket/content requests. Polling for another
    ticket races its one-use unlock and can hang after the chapter has loaded.
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
        reported_continues = 0
        reported_recoveries = 0
        while True:
            check_cancelled()
            with _HOST_LOCK:
                status = host.status(gate_id)
                payload = host.results.get(gate_id)
                continue_count = host.continue_counts.get(gate_id, 0)
                recovery_count = host.recovery_counts.get(gate_id, 0)
            if continue_count > reported_continues:
                print(f"[ad] Episode {episode_no}: Continue auto-clicked. Waiting for chapter...", flush=True)
                reported_continues = continue_count
            if recovery_count > reported_recoveries:
                print(f"[ad] Episode {episode_no}: Viewer page failed to load; retrying automatically (attempt {recovery_count}/2).", flush=True)
                reported_recoveries = recovery_count
            if status == "complete":
                from src.ad_viewer import validate_handoff
                if not validate_handoff(payload, episode_no):
                    raise AdvertisementError("The advertisement viewer returned incomplete episode data. Retry the chapter.")
                return AdvertisementResult(ticket=payload["ticket"], content=payload["content"])
            if status == "error":
                raise AdvertisementError(_BROWSER_ERROR)
            if status == "load_error":
                raise AdvertisementError(
                    "The official viewer could not load this chapter after automatic retries. "
                    "Try again later. Details: output/logs/advertisements.log."
                )
            if status == "closed":
                raise AdvertisementError(
                    "The advertisement window closed before its chapter was received. "
                    "Retry and let the ad finish."
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
                    "The advertisement viewer did not deliver the chapter within "
                    f"{timeout:g} seconds. In the viewer, sign in to the same "
                    "account and allow the advertisement to finish, then retry."
                )
            cancelled.wait(min(poll_interval, 0.2, remaining))
    finally:
        _unregister_viewer(host, gate_id)
