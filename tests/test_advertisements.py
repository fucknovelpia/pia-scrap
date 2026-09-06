import json
import queue
import shutil
import subprocess
import concurrent.futures
import time
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from src.advertisements import (
    AdvertisementError, AdvertisementResult, _CONTINUE_AD_JS, _continue_finished_ad, _run_ad_host,
    _exception_location, _register_viewer, _unregister_viewer, watch_episode_ad,
)


def ticket(status=500, unlocked=False):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"unlocked": unlocked}).encode()
    return response


def is_unlocked(response):
    return response.status_code == 200 and response.json().get("unlocked") is True


def handoff(episode_no=670403):
    return {
        "episode_no": episode_no,
        "ticket": {"statusCode": 200, "result": {
            "_t": f"private-ticket-{episode_no}", "data": {"episode_no": episode_no},
        }},
        "content": {"statusCode": 200, "result": {"data": {
            "epi_content": f"<p>Private chapter body {episode_no}</p>",
        }}},
    }


class StatusQueue(queue.Queue):
    def __init__(self, statuses=()):
        super().__init__()
        self.on_put = None
        self.close = Mock()
        self.cancel_join_thread = Mock()
        for status in statuses:
            self.put(status)

    def put(self, item, *args, **kwargs):
        super().put(item, *args, **kwargs)
        if self.on_put is not None:
            self.on_put(item)


class FakeProcess:
    def __init__(self):
        self.alive = False
        self.exitcode = 17
        self.start = Mock(side_effect=self._start)
        self.join = Mock(side_effect=self._join)
        self.is_alive = Mock(side_effect=lambda: self.alive)
        self.terminate = Mock()
        self.kill = Mock()
        self.close = Mock()

    def _start(self):
        self.alive = True

    def _join(self, timeout):
        self.alive = False


class AdvertisementTests(unittest.TestCase):
    def setUp(self):
        self.cancelled = threading.Event()
        self.stop = threading.Event()
        self.commands = StatusQueue()
        self.statuses = StatusQueue()
        self.process = FakeProcess()
        self.context = SimpleNamespace(
            Event=Mock(return_value=self.stop),
            Queue=Mock(side_effect=[self.commands, self.statuses]),
            Process=Mock(return_value=self.process),
        )
        self.clock = 100.0
        self.addCleanup(patch.stopall)
        patch("src.advertisements._VIEWER_HOST", None).start()
        self.get_context = patch(
            "src.advertisements.multiprocessing.get_context", return_value=self.context,
        ).start()
        patch("src.advertisements.time.monotonic", side_effect=lambda: self.clock).start()
        patch.object(self.cancelled, "wait", side_effect=self.advance).start()

    def advance(self, seconds):
        self.clock += seconds
        return self.cancelled.is_set()

    def watch(self, probe, **kwargs):
        return watch_episode_ad(670403, probe, self.cancelled, is_unlocked, **kwargs)

    def assert_browser_cleaned_up(self):
        self.assertTrue(self.stop.is_set())
        self.process.join.assert_called()
        self.process.close.assert_called_once()
        self.statuses.close.assert_called_once()
        self.statuses.cancel_join_thread.assert_called_once()
        self.commands.close.assert_called_once()
        self.commands.cancel_join_thread.assert_called_once()

    def complete_on_open(self, payload, after=()):
        def publish(command):
            action, gate_id, _episode_no = command
            if action == "open":
                self.statuses.put(("complete", gate_id, payload))
                for status in after:
                    self.statuses.put((status, gate_id))
        self.commands.on_put = publish

    def test_rechecks_ticket_before_opening_shared_browser(self):
        unlocked = ticket(200, True)
        probe = Mock(return_value=unlocked)
        self.assertIs(self.watch(probe), unlocked)
        probe.assert_called_once_with()
        self.get_context.assert_not_called()

    def test_browser_consumes_one_use_unlock_without_further_api_probes(self):
        self.statuses.put(("ready", None))
        payload = handoff()
        probe = Mock(side_effect=[ticket(), AssertionError("Unlock has already been consumed")])

        def deliver_after_wait(seconds):
            self.advance(seconds)
            if self.clock >= 101.0:
                command = self.commands.get_nowait()
                self.statuses.put(("complete", command[1], payload))

        self.cancelled.wait.side_effect = deliver_after_wait
        result = self.watch(probe)
        self.assertIsInstance(result, AdvertisementResult)
        self.assertEqual(result.ticket, payload["ticket"])
        self.assertEqual(result.content, payload["content"])
        probe.assert_called_once_with()
        self.get_context.assert_called_once_with("spawn")
        self.context.Process.assert_called_once_with(
            target=_run_ad_host, args=(self.stop, self.commands, self.statuses),
            name="PIA advertisement host", daemon=True,
        )
        self.assert_browser_cleaned_up()

    def test_closed_window_does_not_count_as_completion(self):
        self.statuses.put(("closed", None))
        with self.assertRaisesRegex(AdvertisementError, "closed before its chapter was received"):
            self.watch(Mock(return_value=ticket()))
        self.assert_browser_cleaned_up()

    def test_captured_result_survives_close_error_and_reload_at_same_time(self):
        payload = handoff()
        self.complete_on_open(payload, after=("closed", "error", "ready"))
        probe = Mock(side_effect=[ticket(), AssertionError("Must not fetch another ticket")])
        result = self.watch(probe)
        self.assertEqual(result, AdvertisementResult(payload["ticket"], payload["content"]))
        probe.assert_called_once_with()
        self.assert_browser_cleaned_up()

    def test_child_error_is_reported_without_response_or_exception_secrets(self):
        self.statuses.put(("error", None))
        with self.assertRaisesRegex(AdvertisementError, "browser could not start"):
            self.watch(Mock(return_value=ticket()))
        self.assert_browser_cleaned_up()

    def test_process_start_failure_cleans_resources_and_releases_lock(self):
        self.process.start.side_effect = RuntimeError("session-secret")
        with self.assertRaises(AdvertisementError) as failure:
            self.watch(Mock(return_value=ticket()))
        self.assertNotIn("session-secret", str(failure.exception))
        self.process.close.assert_called_once()
        self.statuses.close.assert_called_once()
        self.process.join.assert_not_called()
        self.assertTrue(is_unlocked(self.watch(Mock(return_value=ticket(200, True)))))

    def test_cancel_before_start_does_not_probe_or_open_browser(self):
        self.cancelled.set()
        probe = Mock()
        with self.assertRaisesRegex(AdvertisementError, "cancelled"):
            self.watch(probe)
        probe.assert_not_called()
        self.get_context.assert_not_called()

    def test_cancel_during_viewing_closes_browser(self):
        self.cancelled.wait.side_effect = lambda seconds: self.cancelled.set()
        with self.assertRaisesRegex(AdvertisementError, "cancelled"):
            self.watch(Mock(return_value=ticket()))
        self.assert_browser_cleaned_up()

    def test_page_load_and_elapsed_time_never_count_as_ad_completion(self):
        self.statuses.put(("ready", None))
        probe = Mock(return_value=ticket())
        with self.assertRaisesRegex(AdvertisementError, "did not deliver the chapter"):
            self.watch(probe, timeout=0.6)
        probe.assert_called_once_with()
        self.assert_browser_cleaned_up()

    def test_failed_initial_probe_still_allows_valid_browser_handoff(self):
        payload = handoff()
        self.complete_on_open(payload)
        probe = Mock(side_effect=requests.Timeout("private-response"))
        result = self.watch(probe)
        self.assertEqual(result.content, payload["content"])
        probe.assert_called_once_with()
        self.assert_browser_cleaned_up()

    def test_child_exit_is_detected_even_without_close_status(self):
        self.process.start.side_effect = None
        with self.assertRaisesRegex(AdvertisementError, "exited unexpectedly .*exit code 17"):
            self.watch(Mock(return_value=ticket()))
        self.assert_browser_cleaned_up()

    def test_hung_browser_is_terminated_after_graceful_shutdown_attempt(self):
        self.process.join.side_effect = None
        self.process.terminate.side_effect = lambda: setattr(self.process, "alive", False)
        self.statuses.put(("closed", None))
        with self.assertRaises(AdvertisementError):
            self.watch(Mock(return_value=ticket()))
        self.process.terminate.assert_called_once_with()
        self.process.kill.assert_not_called()
        self.assert_browser_cleaned_up()

    def test_four_gates_share_host_but_keep_window_status_and_cleanup_independent(self):
        registrations = [_register_viewer(670403 + i, self.cancelled) for i in range(4)]
        host = registrations[0][0]
        self.assertTrue(all(item[0] is host for item in registrations))
        self.context.Process.assert_called_once()
        opens = [self.commands.get_nowait() for _ in registrations]
        self.assertEqual([entry[0] for entry in opens], ["open"] * 4)
        self.assertEqual([entry[2] for entry in opens], list(range(670403, 670407)))
        self.assertEqual(len(set(entry[1] for entry in opens)), 4)
        first = registrations[0][1]
        second = registrations[1][1]
        self.statuses.put(("closed", first))
        self.statuses.put(("ready", second))
        self.assertEqual(host.status(first), "closed")
        self.assertEqual(host.status(second), "ready")
        _unregister_viewer(host, first)
        self.process.join.assert_not_called()
        self.assertFalse(self.stop.is_set())
        for remaining_host, gate in registrations[1:]:
            _unregister_viewer(remaining_host, gate)
        self.assertEqual(host.results, {})
        self.assert_browser_cleaned_up()

    def test_four_ad_watchers_receive_their_own_captured_result_concurrently(self):
        patch("src.advertisements.time.monotonic", side_effect=time.perf_counter).start()
        barrier = threading.Barrier(4)
        payloads = [handoff(670403 + index) for index in range(4)]
        gates = {}

        def record_gate(command):
            if command[0] == "open":
                gates[command[2]] = command[1]

        self.commands.on_put = record_gate

        def watch_one(index):
            cancelled = threading.Event()
            probe = Mock(side_effect=[ticket(), AssertionError("One-use unlock already consumed")])

            def publish(seconds):
                barrier.wait(timeout=3)
                self.statuses.put(("complete", gates[670403 + index], payloads[index]))

            with patch.object(cancelled, "wait", side_effect=publish):
                result = watch_episode_ad(
                    670403 + index, probe, cancelled, is_unlocked,
                    poll_interval=0.01, timeout=5,
                )
            probe.assert_called_once_with()
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(watch_one, range(4)))
        self.assertEqual(results, [AdvertisementResult(p["ticket"], p["content"]) for p in payloads])
        self.context.Process.assert_called_once()
        self.assert_browser_cleaned_up()

    def test_malformed_or_mismatched_browser_payload_is_rejected_and_cleaned_up(self):
        invalid = [None, {}, handoff(670404)]
        wrong_ticket = handoff()
        wrong_ticket["ticket"]["result"]["data"]["episode_no"] = 670404
        empty_content = handoff()
        empty_content["content"]["result"]["data"] = {}
        error_content = handoff()
        error_content["content"]["statusCode"] = 500
        invalid.extend((wrong_ticket, empty_content, error_content))
        for payload in invalid:
            with self.subTest(payload=payload):
                host = SimpleNamespace(status=Mock(return_value="complete"), results={"gate": payload})
                with (
                    patch("src.advertisements._register_viewer", return_value=(host, "gate")),
                    patch("src.advertisements._unregister_viewer") as cleanup,
                ):
                    probe = Mock(return_value=ticket())
                    with self.assertRaisesRegex(AdvertisementError, "incomplete episode data") as failed:
                        self.watch(probe)
                    self.assertNotIn("private-ticket", str(failed.exception))
                    self.assertNotIn("Private chapter body", str(failed.exception))
                    cleanup.assert_called_once_with(host, "gate")
                    probe.assert_called_once_with()

    def test_unrelated_gate_result_does_not_complete_current_episode(self):
        def publish(command):
            if command[0] == "open":
                self.statuses.put(("complete", "unregistered-gate", handoff()))
        self.commands.on_put = publish
        with self.assertRaisesRegex(AdvertisementError, "did not deliver"):
            self.watch(Mock(return_value=ticket()), timeout=0.4)
        self.assert_browser_cleaned_up()

    def test_completed_payload_is_removed_when_its_gate_is_unregistered(self):
        host, gate = _register_viewer(670403, self.cancelled)
        payload = handoff()
        self.statuses.put(("complete", gate, payload))
        self.statuses.put(("closed", None))
        self.assertEqual(host.status(gate), "complete")
        self.assertIs(host.results[gate], payload)
        _unregister_viewer(host, gate)
        self.assertEqual(host.results, {})
        self.assertEqual(host.gates, {})
        self.assert_browser_cleaned_up()


class BrowserEvent:
    def __iadd__(self, callback):
        self.callback = callback
        return self


class AdvertisementViewerTests(unittest.TestCase):
    def setUp(self):
        installer = patch("src.ad_viewer.install_viewer_handoff")
        self.install_handoff = installer.start()
        self.addCleanup(installer.stop)

    def test_exception_diagnostics_keep_codes_and_frames_without_private_message(self):
        try:
            raise OSError(9, "session-secret-and-private-url")
        except OSError as exc:
            description = _exception_location(exc)
        self.assertIn("OSError", description)
        self.assertIn("errno=9", description)
        self.assertIn("frames=test_exception_diagnostics", description)
        self.assertNotIn("session-secret", description)
        self.assertNotIn("private-url", description)

    def test_continue_waits_for_loaded_exact_viewer_and_handles_navigation(self):
        window = Mock()
        window.events.loaded.is_set.return_value = True
        window.evaluate_js.return_value = True
        for url in (
            "http://global.novelpia.com/viewer/670403",
            "https://global.novelpia.com.evil.test/viewer/670403",
            "https://global.novelpia.com/viewer/670404",
            "https://global.novelpia.com:8443/viewer/670403",
            "https://global.novelpia.com/login",
        ):
            window.get_current_url.return_value = url
            self.assertFalse(_continue_finished_ad(window, 670403))
        window.evaluate_js.assert_not_called()
        window.get_current_url.return_value = "https://global.novelpia.com/viewer/670403"
        window.events.loaded.is_set.return_value = False
        self.assertFalse(_continue_finished_ad(window, 670403))
        window.evaluate_js.assert_not_called()
        window.events.loaded.is_set.return_value = True
        self.assertTrue(_continue_finished_ad(window, 670403))
        window.evaluate_js.assert_called_once()
        self.assertNotIn("__EPISODE_PATH__", window.evaluate_js.call_args.args[0])
        window.evaluate_js.side_effect = RuntimeError("private-page-data")
        self.assertFalse(_continue_finished_ad(window, 670403))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the DOM helper test")
    def test_continue_dom_check_rejects_early_hidden_disabled_and_unrelated_controls(self):
        harness = r"""
const vm = require('vm');
const script = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function check(change) {
    let clicks = 0;
    const button = {
        tagName: 'BUTTON', textContent: 'Continue', disabled: false,
        matches: () => false, getAttribute: () => null,
        getClientRects: () => [1], contains: () => false,
        getBoundingClientRect: () => ({left: 10, top: 10, right: 110, bottom: 50, width: 100, height: 40}),
        click: () => {clicks++;}
    };
    const window = {
        location: {origin: 'https://global.novelpia.com', pathname: '/viewer/670403'},
        innerWidth: 1000, innerHeight: 780,
        getComputedStyle: () => ({display: 'block', visibility: 'visible', opacity: '1'})
    };
    const document = {
        getElementById: id => id === 'ez-rewarded-continue-button' ? button : null,
        elementFromPoint: () => button
    };
    change({button, window, document});
    const context = vm.createContext({window, document});
    vm.runInContext(script, context);
    vm.runInContext(script, context);
    return clicks;
}
const results = [
    check(() => {}),
    check(({document}) => {document.getElementById = () => null;}),
    check(({button}) => {button.disabled = true;}),
    check(({button}) => {button.getAttribute = () => 'true';}),
    check(({button}) => {button.getClientRects = () => [];}),
    check(({button}) => {button.textContent = 'Continue in 10';}),
    check(({button}) => {button.tagName = 'DIV';}),
    check(({document}) => {document.elementFromPoint = () => ({});}),
    check(({window}) => {window.getComputedStyle = () => ({display:'none', visibility:'visible', opacity:'1'});}),
    check(({window}) => {window.getComputedStyle = () => ({display:'block', visibility:'hidden', opacity:'1'});}),
    check(({window}) => {window.getComputedStyle = () => ({display:'block', visibility:'visible', opacity:'0'});}),
    check(({window}) => {window.location.origin = 'https://another-site.test';}),
    check(({window}) => {window.location.pathname = '/viewer/670404';})
];
process.stdout.write(JSON.stringify(results));
"""
        script = _CONTINUE_AD_JS.replace("__EPISODE_PATH__", json.dumps("/viewer/670403"))
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], input=json.dumps(script),
            text=True, capture_output=True, check=True, timeout=10,
        )
        self.assertEqual(json.loads(result.stdout), [1] + [0] * 12)

    def test_viewer_closes_when_source_download_parent_exits(self):
        stop = threading.Event()
        statuses = StatusQueue()
        window = Mock()
        window.events = SimpleNamespace(closed=BrowserEvent(), loaded=BrowserEvent())
        webview = SimpleNamespace(
            settings={}, create_window=Mock(return_value=window),
            start=Mock(side_effect=lambda callback, **kwargs: callback()),
        )
        parent = Mock()
        parent.is_alive.return_value = False
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.advertisements.const.APP_DIR", Path(directory)),
            patch("src.advertisements.multiprocessing.parent_process", return_value=parent),
            patch.dict("sys.modules", {"webview": webview, "pythonnet": SimpleNamespace(load=Mock())}),
        ):
            _run_ad_host(stop, StatusQueue(), statuses)
        window.destroy.assert_called_once_with()
        window.evaluate_js.assert_not_called()

    def test_dynamic_windows_share_profile_and_close_independently(self):
        stop = threading.Event()
        statuses = StatusQueue()
        anchor, first, second = Mock(), Mock(), Mock()
        for window in (anchor, first, second):
            window.events = SimpleNamespace(closed=BrowserEvent(), loaded=BrowserEvent())
        pending = iter([
            ("open", "first", 670403), ("open", "second", 670404),
            ("close", "first", None),
        ])

        def get_command(timeout):
            try:
                return next(pending)
            except StopIteration:
                first.destroy.assert_called_once_with()
                second.destroy.assert_not_called()
                stop.set()
                raise queue.Empty

        webview = SimpleNamespace(
            settings={"OPEN_EXTERNAL_LINKS_IN_BROWSER": True},
            create_window=Mock(side_effect=[anchor, first, second]),
            start=Mock(side_effect=lambda callback, **kwargs: callback()),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.advertisements.const.APP_DIR", Path(directory)),
            patch("src.advertisements._continue_finished_ad") as click_continue,
            patch("src.advertisements.threading.Thread") as monitor,
            patch.dict("sys.modules", {"webview": webview, "pythonnet": SimpleNamespace(load=Mock())}),
        ):
            _run_ad_host(stop, SimpleNamespace(get=Mock(side_effect=get_command)), statuses)
            calls = webview.create_window.call_args_list
            self.assertTrue(calls[0].kwargs["hidden"])
            for call in calls[1:]:
                self.assertTrue(call.kwargs["hidden"])
                self.assertIn("Preparing advertisement", call.kwargs["html"])
                self.assertEqual(len(call.args), 1)
                self.assertNotIn("url", call.kwargs)
            self.assertEqual(webview.start.call_args.kwargs, {
                "debug": False, "private_mode": False,
                "storage_path": str(Path(directory) / ".webview_data"),
            })
            self.assertTrue((Path(directory) / ".webview_data").is_dir())
            click_continue.assert_not_called()
            self.assertEqual(monitor.call_count, 2)
            self.assertTrue(all(call.kwargs["daemon"] for call in monitor.call_args_list))
        first.destroy.assert_called_once_with()
        second.destroy.assert_called_once_with()
        anchor.destroy.assert_called_once_with()
        self.assertFalse(webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"])
        self.assertEqual(statuses.get_nowait(), ("closed", None))

    def test_completed_browser_handoff_hides_viewer_and_keeps_content_out_of_logs(self):
        stop = threading.Event()
        completed = threading.Event()
        statuses = StatusQueue()
        anchor, viewer = Mock(), Mock()
        viewer.events = SimpleNamespace(closed=BrowserEvent(), loaded=BrowserEvent())
        payload = handoff()
        step = 0

        def install(window, episode_no, on_complete, on_error, on_diagnostic=None):
            self.assertIs(window, viewer)
            self.assertEqual(episode_no, 670403)
            on_complete(payload)
            completed.set()

        def get_command(timeout):
            nonlocal step
            step += 1
            if step == 1:
                return ("open", "specific-gate", 670403)
            if step == 2:
                self.assertTrue(completed.wait(2))
                return ("close", "specific-gate", None)
            stop.set()
            raise queue.Empty

        self.install_handoff.side_effect = install
        webview = SimpleNamespace(
            settings={}, create_window=Mock(side_effect=[anchor, viewer]),
            start=Mock(side_effect=lambda callback, **kwargs: callback()),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.advertisements.const.APP_DIR", Path(directory)),
            patch.dict("sys.modules", {"webview": webview, "pythonnet": SimpleNamespace(load=Mock())}),
        ):
            _run_ad_host(stop, SimpleNamespace(get=Mock(side_effect=get_command)), statuses)
            log = (Path(directory) / "output" / "logs" / "advertisements.log").read_text(encoding="utf-8")
        self.assertEqual(statuses.get_nowait(), ("complete", "specific-gate", payload))
        self.assertEqual(statuses.get_nowait(), ("closed", None))
        self.assertIn("Received viewer content for episode 670403", log)
        self.assertNotIn("private-ticket", log)
        self.assertNotIn("Private chapter body", log)
        viewer.hide.assert_called_once_with()
        viewer.destroy.assert_called_once_with()
        viewer.load_url.assert_not_called()

    def test_native_browser_error_exposes_only_fixed_status(self):
        statuses = StatusQueue()
        webview = SimpleNamespace(
            settings={}, create_window=Mock(side_effect=RuntimeError("secret-url")),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.advertisements.const.APP_DIR", Path(directory)),
            patch.dict("sys.modules", {"webview": webview, "pythonnet": SimpleNamespace(load=Mock())}),
        ):
            _run_ad_host(threading.Event(), StatusQueue(), statuses)
            log = (Path(directory) / "output" / "logs" / "advertisements.log").read_text(encoding="utf-8")
            self.assertIn("Advertisement host failed (RuntimeError)", log)
            self.assertNotIn("secret-url", log)
        self.assertEqual(statuses.get_nowait(), ("error", None))
        self.assertEqual(statuses.get_nowait(), ("closed", None))
        self.assertTrue(statuses.empty())

    def test_hung_continue_check_does_not_block_other_windows_or_shutdown(self):
        stop = threading.Event()
        first_blocked = threading.Event()
        second_checked = threading.Event()
        release_first = threading.Event()
        statuses = StatusQueue()
        anchor, first, second = Mock(), Mock(), Mock()
        for window in (anchor, first, second):
            window.events = SimpleNamespace(closed=BrowserEvent(), loaded=BrowserEvent())
        step = 0

        def get_command(timeout):
            nonlocal step
            step += 1
            if step == 1:
                return ("open", "first", 670403)
            if step == 2:
                self.assertTrue(first_blocked.wait(2))
                return ("open", "second", 670404)
            if step == 3:
                self.assertTrue(second_checked.wait(2))
                return ("close", "first", None)
            if step == 4:
                first.destroy.assert_called_once_with()
                return ("close", "second", None)
            stop.set()
            raise queue.Empty

        def continue_check(window, episode_no):
            if window is first:
                first_blocked.set()
                release_first.wait(10)
            else:
                second_checked.set()
            return False

        webview = SimpleNamespace(
            settings={}, create_window=Mock(side_effect=[anchor, first, second]),
            start=Mock(side_effect=lambda callback, **kwargs: callback()),
        )
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch("src.advertisements.const.APP_DIR", Path(directory)),
                patch("src.advertisements._continue_finished_ad", side_effect=continue_check),
                patch.dict("sys.modules", {"webview": webview, "pythonnet": SimpleNamespace(load=Mock())}),
            ):
                _run_ad_host(stop, SimpleNamespace(get=Mock(side_effect=get_command)), statuses)
            self.assertTrue(second_checked.is_set())
            first.destroy.assert_called_once_with()
            second.destroy.assert_called_once_with()
            anchor.destroy.assert_called_once_with()
            self.assertEqual(statuses.get_nowait(), ("closed", None))
            self.assertTrue(statuses.empty())
        finally:
            release_first.set()


if __name__ == "__main__":
    unittest.main()
