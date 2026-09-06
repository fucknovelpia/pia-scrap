import json
import shutil
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from src.ad_viewer import (
    _ResponseHandoff, _SPOILER_GUARD_JS, _SSR_TICKET_JS, _response_kind, _viewer_url_matches,
    _wait_task, install_viewer_handoff, validate_handoff,
)


API = "https://api-global.novelpia.com/v1/novel/episode"


def ticket(episode=42, token="real-token"):
    return {"statusCode": 200, "result": {
        "_t": token, "data": {"episode_no": episode}, "signed_key": {"CloudFront-Policy": "cookie"},
    }}


def content(text="<p>Chapter body</p>"):
    return {"statusCode": 200, "result": {"data": {"epi_content": text}}}


class Event(threading.Event):
    def __init__(self):
        super().__init__()
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def __isub__(self, callback):
        self.handlers.remove(callback)
        return self


class Task:
    IsCompleted = True
    IsFaulted = False
    IsCanceled = False
    Result = "script-id"


class HandoffValidationTests(unittest.TestCase):
    def test_response_bodies_can_finish_in_reverse_order(self):
        completed = Mock()
        collector = _ResponseHandoff(42, completed)
        collector.receive(API + "/content?_t=real-token", 200, content())
        completed.assert_not_called()
        collector.receive(API + "?episode_no=42", 200, ticket())
        payload = completed.call_args.args[0]
        self.assertTrue(validate_handoff(payload, 42))
        self.assertEqual(payload["ticket"]["result"]["signed_key"]["CloudFront-Policy"], "cookie")
        collector.receive(API + "/content?_t=real-token", 200, content())
        completed.assert_called_once()

    def test_chapter_token_pairing_survives_reload(self):
        completed = Mock()
        collector = _ResponseHandoff(42, completed)
        collector.receive(API + "?episode_no=42", 200, ticket(token="first"))
        collector.receive(API + "?episode_no=42", 200, ticket(token="second"))
        collector.receive(API + "/content?_t=unrelated", 200, content("wrong"))
        completed.assert_not_called()
        collector.receive(API + "/content?_t=second", 200, content("right"))
        self.assertEqual(completed.call_args.args[0]["content"], content("right"))

    def test_rejects_wrong_episode_and_failed_or_empty_responses(self):
        completed = Mock()
        collector = _ResponseHandoff(42, completed)
        for url, status, body in [
            (API + "?episode_no=43", 200, ticket()),
            (API + "?episode_no=42", 200, ticket(43)),
            (API + "?episode_no=42", 500, ticket()),
            (API + "?episode_no=42", 200, {**ticket(), "code": "0010"}),
        ]:
            collector.receive(url, status, body)
        collector.receive(API + "/content?_t=real-token", 200, content())
        completed.assert_not_called()
        collector = _ResponseHandoff(42, completed)
        collector.receive(API + "?episode_no=42", 200, ticket())
        for body in [content(" "), content(15), {}, {**content(), "statusCode": 500},
                     {"statusCode": 200, "result": {"data": {"title": "not chapter text"}}},
                     {"statusCode": 200, "result": {"data": {"epi_content": "wrong", "episode_no": 43}}}]:
            collector.receive(API + "/content?_t=real-token", 200, body)
        completed.assert_not_called()

    def test_filters_exact_api_origin_path_and_unambiguous_query(self):
        for url in [
            API.replace("https:", "http:") + "?episode_no=42",
            API.replace("api-global.novelpia.com", "api-global.novelpia.com.evil") + "?episode_no=42",
            API.replace("https://", "https://user@") + "?episode_no=42",
            API.replace(".com/", ".com:444/") + "?episode_no=42",
            API + "?episode_no=42&episode_no=43", API + "/more?episode_no=42",
            API + "/content?_t=a&_t=b", API + "/content?_t=",
        ]:
            self.assertIsNone(_response_kind(url, 42), url)
        self.assertEqual(_response_kind(API + "/content?_t=a%2Bb", 42), ("content", "a+b"))

    def test_parent_validation_checks_episode_identity(self):
        payload = {"episode_no": 42, "ticket": ticket(), "content": content()}
        self.assertTrue(validate_handoff(payload, 42))
        self.assertFalse(validate_handoff(payload, 43))
        self.assertFalse(validate_handoff({**payload, "episode_no": "42"}, 42))
        self.assertFalse(validate_handoff({**payload, "ticket": ticket(43)}, 42))
        self.assertFalse(validate_handoff({"episode_no": True, "ticket": ticket(1), "content": content()}, 1))
        self.assertTrue(_viewer_url_matches("https://global.novelpia.com/viewer/42/", 42))
        self.assertFalse(_viewer_url_matches("https://global.novelpia.com/viewer/43", 42))

    def test_four_viewers_keep_concurrent_responses_separate(self):
        callbacks = [Mock() for _ in range(4)]
        collectors = [_ResponseHandoff(episode, callback) for episode, callback in zip(range(40, 44), callbacks)]
        barrier = threading.Barrier(4)

        def receive(index):
            collector = collectors[index]
            episode = 40 + index
            collector.receive(API + f"/content?_t=token-{episode}", 200, content(str(episode)))
            barrier.wait(1)
            collector.receive(API + f"?episode_no={episode}", 200, ticket(episode, f"token-{episode}"))

        workers = [threading.Thread(target=receive, args=(index,)) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        for episode, callback in zip(range(40, 44), callbacks):
            callback.assert_called_once()
            self.assertEqual(callback.call_args.args[0]["content"], content(str(episode)))


class NativeSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep imports made by the viewer daemon outside each sys.modules
        # snapshot; cleanup must not remove a module during its first import.
        import src.ad_navigation
        import src.ad_continue

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {
            "System": SimpleNamespace(Action=lambda callback: callback),
            "System.Windows.Forms": SimpleNamespace(FormWindowState=SimpleNamespace(Minimized="minimized")),
        }).start()
        patch("src.ad_viewer.secrets.token_urlsafe", return_value="test-bridge-token").start()
        self.sequence = []
        self.task = Task()
        self.read_script = Mock(return_value=SimpleNamespace(
            IsCompleted=True, IsFaulted=False, IsCanceled=False, Result="null",
        ))
        self.core = SimpleNamespace(
            WebResourceResponseReceived=Event(),
            WebMessageReceived=Event(),
            NavigationStarting=Event(),
            NavigationCompleted=Event(),
            AddScriptToExecuteOnDocumentCreatedAsync=Mock(side_effect=self.register_script),
            Source="https://global.novelpia.com/viewer/42",
            ExecuteScriptAsync=Mock(side_effect=self.execute_script),
        )
        self.window = SimpleNamespace(
            events=SimpleNamespace(loaded=Event(), closed=Event()),
            native=SimpleNamespace(
                webview=SimpleNamespace(CoreWebView2=self.core),
                WindowState="normal",
                Show=Mock(side_effect=self.show_native),
            ),
            show=Mock(),
            load_url=Mock(side_effect=lambda url: self.sequence.append("navigate")),
        )
        self.window.events.loaded.set()
        self.stock_message = Mock()
        self.window.native.browser = SimpleNamespace(on_script_notify=self.stock_message)
        self.window.native.webview.WebMessageReceived = Event()
        self.window.native.webview.WebMessageReceived += self.stock_message
        self.window.native.BeginInvoke = Mock(side_effect=lambda callback: callback())
        self.complete = Mock()
        self.error = Mock()
        self.continued = Mock()

    def show_native(self):
        self.assertEqual(self.window.native.WindowState, "minimized")
        self.sequence.append("show")

    def execute_script(self, script):
        if script.startswith("window.location.replace("):
            self.sequence.append("navigate")
            return SimpleNamespace(IsCompleted=True, IsFaulted=False, IsCanceled=False, Result="null")
        return self.read_script(script)

    def tearDown(self):
        for handler in self.window.events.closed.handlers:
            handler()

    def register_script(self, script):
        self.assertEqual(len(self.core.WebResourceResponseReceived.handlers), 1)
        self.assertEqual(len(self.core.WebMessageReceived.handlers), 1)
        self.assertIn('"/viewer/42"', script)
        self.sequence.append("register")
        return self.task

    def install(self, **settings):
        install_viewer_handoff(self.window, 42, self.complete, self.error, on_continue=self.continued, **settings)

    def message_args(self, message=None, source="https://global.novelpia.com/viewer/42"):
        if message is None:
            message = {"type": "pia-ad-continue", "episode_no": 42,
                       "token": "test-bridge-token", "click_id": "first-click"}
        return SimpleNamespace(Source=source, get_WebMessageAsJson=lambda: json.dumps(message))

    def test_configured_retries_and_cooldown_reach_the_native_watchdog(self):
        constructed = threading.Event()
        captured = {}

        def watchdog(_now, **settings):
            captured.update(settings)
            constructed.set()
            return SimpleNamespace(observe=lambda state, now: None)

        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=watchdog):
            self.install(max_retries="7", retry_cooldown="12.5")
            self.assertTrue(constructed.wait(1))
        self.assertEqual(captured, {"max_retries": 7, "retry_delay": 12.5})
        self.error.assert_not_called()

    def test_completed_response_between_retry_decision_and_ui_dispatch_prevents_reload(self):
        collector = _ResponseHandoff(42, self.complete)
        dispatch_finished = threading.Event()

        def complete_during_retry(status):
            self.assertEqual(status, "retrying")
            collector.receive(API + "?episode_no=42", 200, ticket())
            collector.receive(API + "/content?_t=real-token", 200, content())

        def invoke(callback):
            callback()
            if collector.completed.is_set():
                dispatch_finished.set()

        self.window.native.BeginInvoke.side_effect = invoke
        with (
            patch("src.ad_viewer._ResponseHandoff", return_value=collector),
            patch("src.ad_navigation.ViewerNavigationWatchdog", return_value=SimpleNamespace(
                observe=lambda state, now: "retry",
            )),
        ):
            self.install(on_navigation_status=complete_during_retry)
            self.assertTrue(dispatch_finished.wait(1))
        self.complete.assert_called_once()
        self.assertEqual(self.sequence.count("navigate"), 1)
        self.error.assert_not_called()

    def test_observer_and_document_start_registration_finish_before_navigation(self):
        def await_registration(task, timeout, cancelled):
            if task is not self.task:
                return task.Result
            self.assertEqual(self.sequence, ["register"])
            self.window.show.assert_not_called()
            self.window.native.Show.assert_not_called()
            self.window.load_url.assert_not_called()
            self.sequence.append("registered")
            return "script-id"

        with patch("src.ad_viewer._wait_task", side_effect=await_registration):
            self.install()
        self.assertEqual(self.sequence, ["register", "registered", "show", "navigate"])
        self.window.show.assert_not_called()
        self.window.load_url.assert_not_called()
        self.assertEqual(self.window.real_url, "https://global.novelpia.com/viewer/42")
        self.core.ExecuteScriptAsync.assert_any_call('window.location.replace("https://global.novelpia.com/viewer/42");')
        self.error.assert_not_called()

    def test_failed_script_registration_never_exposes_or_navigates_viewer(self):
        self.task.IsFaulted = True
        self.install()
        self.error.assert_called_once()
        self.window.show.assert_not_called()
        self.window.native.Show.assert_not_called()
        self.window.load_url.assert_not_called()

    def test_late_native_callback_cannot_navigate_after_timeout(self):
        pending = []
        self.window.native.BeginInvoke.side_effect = pending.append
        with patch("src.ad_viewer._SETUP_TIMEOUT", 0.01):
            self.install()
        self.error.assert_called_once()
        for callback in pending:
            callback()
        self.window.load_url.assert_not_called()
        self.core.AddScriptToExecuteOnDocumentCreatedAsync.assert_not_called()

    def test_response_observer_ignores_other_page_or_endpoint(self):
        self.install()
        callback = self.core.WebResourceResponseReceived.handlers[0]
        response = SimpleNamespace(StatusCode=200, GetContentAsync=Mock())
        args = SimpleNamespace(Request=SimpleNamespace(Uri=API + "?episode_no=42"), Response=response)
        callback(SimpleNamespace(Source="https://global.novelpia.com/viewer/43"), args)
        args.Request.Uri = "https://ads.example.com/v1/novel/episode?episode_no=42"
        callback(SimpleNamespace(Source="https://global.novelpia.com/viewer/42"), args)
        response.GetContentAsync.assert_not_called()

    def test_continue_bridge_validates_source_episode_nonce_and_unique_click(self):
        self.install()
        callback = self.core.WebMessageReceived.handlers[0]
        valid = {"type": "pia-ad-continue", "episode_no": 42,
                 "token": "test-bridge-token", "click_id": "first-click"}
        for source in ["https://evil.example/viewer/42", "https://global.novelpia.com/viewer/43",
                       "https://name@global.novelpia.com/viewer/42"]:
            callback(self.core, self.message_args(source=source))
        for message in [
            {**valid, "episode_no": 43}, {**valid, "episode_no": "42"},
            {**valid, "token": "wrong"}, {**valid, "type": "other"},
            {**valid, "click_id": ""}, {**valid, "click_id": "x" * 129},
        ]:
            callback(self.core, self.message_args(message))
        callback(SimpleNamespace(Source="https://global.novelpia.com/viewer/43"), self.message_args())
        self.continued.assert_not_called()
        callback(self.core, self.message_args())
        callback(self.core, self.message_args())
        self.continued.assert_called_once_with()
        callback(self.core, self.message_args({**valid, "click_id": "new-document-click"}))
        self.assertEqual(self.continued.call_count, 2)
        self.complete.assert_not_called()

    def test_continue_notification_does_not_replace_other_pywebview_messages(self):
        self.install()
        forwarder = self.window.native.webview.WebMessageReceived.handlers
        self.assertEqual(len(forwarder), 1)
        normal = self.message_args(["usual_function", "{}", "result-id"])
        files_dropped = self.message_args("FilesDropped")
        forwarder[0](self.core, normal)
        forwarder[0](self.core, files_dropped)
        forwarder[0](self.core, self.message_args())
        self.stock_message.assert_has_calls([call(self.core, normal), call(self.core, files_dropped)])
        self.assertEqual(self.stock_message.call_count, 2)

    def test_continue_bridge_stops_when_viewer_closes(self):
        self.install()
        callback = self.core.WebMessageReceived.handlers[0]
        for handler in self.window.events.closed.handlers:
            handler()
        callback(self.core, self.message_args())
        self.continued.assert_not_called()

    def test_native_read_diagnostics_exclude_sensitive_exception_text(self):
        diagnostics = []
        read_failed = threading.Event()

        def report(message):
            diagnostics.append(message)
            if "Reading ticket response failed" in message:
                read_failed.set()

        install_viewer_handoff(self.window, 42, self.complete, self.error, report)
        callback = self.core.WebResourceResponseReceived.handlers[0]
        args = SimpleNamespace(
            Request=SimpleNamespace(Uri=API + "?episode_no=42&secret=DO-NOT-LOG"),
            Response=SimpleNamespace(StatusCode=200, GetContentAsync=Mock(return_value=Task())),
        )
        with patch("src.ad_viewer._read_response", side_effect=RuntimeError("PRIVATE TOKEN AND CHAPTER")):
            callback(SimpleNamespace(Source="https://global.novelpia.com/viewer/42"), args)
            self.assertTrue(read_failed.wait(1))
        combined = "\n".join(diagnostics)
        self.assertIn("document-start guard ready", combined)
        self.assertIn("status=200, viewer_match=True", combined)
        self.assertIn("RuntimeError", combined)
        self.assertNotIn("DO-NOT-LOG", combined)
        self.assertNotIn("PRIVATE TOKEN", combined)
        self.assertNotIn(API, combined)

    def test_pending_native_task_has_bounded_wait_and_honors_close(self):
        task = Task()
        task.IsCompleted = False
        with self.assertRaises(TimeoutError):
            _wait_task(task, 0.005, threading.Event())
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(RuntimeError):
            _wait_task(task, 1, cancelled)

    def test_hydrated_ticket_completes_content_captured_before_hydration(self):
        hydration = SimpleNamespace(
            IsCompleted=False, IsFaulted=False, IsCanceled=False,
            Result=json.dumps(ticket()),
        )
        self.read_script.return_value = hydration
        done = threading.Event()
        self.complete.side_effect = lambda payload: done.set()
        self.install()
        callback = self.core.WebResourceResponseReceived.handlers[0]
        args = SimpleNamespace(
            Request=SimpleNamespace(Uri=API + "/content?_t=real-token"),
            Response=SimpleNamespace(StatusCode=200, GetContentAsync=Mock(return_value=Task())),
        )
        read = threading.Event()

        def read_content(task, cancelled):
            read.set()
            return content()

        with patch("src.ad_viewer._read_response", side_effect=read_content):
            callback(SimpleNamespace(Source=self.core.Source), args)
            self.assertTrue(read.wait(1))
        self.complete.assert_not_called()
        hydration.IsCompleted = True
        self.assertTrue(done.wait(1))
        self.assertTrue(validate_handoff(self.complete.call_args.args[0], 42))
        self.assertIn("viewer_", self.read_script.call_args.args[0])

    def test_return_to_neutral_after_viewer_retries_without_waiting_for_ad_timeout(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        first_read = threading.Event()
        retried = threading.Event()
        statuses = []
        ad_body = {"__pia_viewer_state": "ad"}

        def read_ad(_script):
            first_read.set()
            return SimpleNamespace(IsCompleted=True, IsFaulted=False, IsCanceled=False, Result=json.dumps(ad_body))

        def navigate(script):
            task = self.execute_script(script)
            if script.startswith("window.location.replace("):
                navigation_id = self.sequence.count("navigate")
                self.core.NavigationStarting.handlers[0](self.core, SimpleNamespace(
                    Uri="https://global.novelpia.com/viewer/42", NavigationId=navigation_id,
                ))
                self.core.Source = "https://global.novelpia.com/viewer/42"
                self.core.NavigationCompleted.handlers[0](self.core, SimpleNamespace(
                    NavigationId=navigation_id, IsSuccess=True, WebErrorStatus="Unknown",
                ))
                if navigation_id == 2:
                    retried.set()
            return task

        self.read_script.side_effect = read_ad
        self.core.ExecuteScriptAsync.side_effect = navigate
        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=lambda now, **settings: ViewerNavigationWatchdog(now, retry_delay=0)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=statuses.append)
            self.assertTrue(first_read.wait(1))
            self.core.NavigationStarting.handlers[0](self.core, SimpleNamespace(
                Uri="about:blank", NavigationId=10,
            ))
            self.core.Source = "about:blank"
            self.core.NavigationCompleted.handlers[0](self.core, SimpleNamespace(
                NavigationId=10, IsSuccess=True, WebErrorStatus="Unknown",
            ))
            self.assertTrue(retried.wait(2), "Returning to the preparation page was not recovered promptly")
        self.assertEqual(statuses, ["retrying"])
        self.assertEqual(self.sequence.count("navigate"), 2)
        self.complete.assert_not_called()
        self.error.assert_not_called()

    def test_native_navigation_failure_while_neutral_has_bounded_page_error(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        statuses = []
        finished = threading.Event()
        original_execute = self.execute_script

        def fail_navigation(script):
            task = original_execute(script)
            if script.startswith("window.location.replace("):
                # A failed first navigation can retain about:blank; it has
                # never reached the viewer, so this must use the native error.
                navigation_id = self.sequence.count("navigate")
                self.core.NavigationStarting.handlers[0](self.core, SimpleNamespace(
                    Uri="https://global.novelpia.com/viewer/42", NavigationId=navigation_id,
                ))
                self.core.Source = "about:blank"
                self.core.NavigationCompleted.handlers[0](
                    self.core, SimpleNamespace(NavigationId=navigation_id, IsSuccess=False,
                                               WebErrorStatus="HostNameNotResolved"),
                )
            return task

        def status(value):
            statuses.append(value)
            if value == "load_error":
                finished.set()

        self.core.ExecuteScriptAsync.side_effect = fail_navigation
        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=lambda now, **settings: ViewerNavigationWatchdog(now, load_timeout=30, **settings)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=status,
                                   max_retries=2, retry_cooldown=0)
            self.assertTrue(finished.wait(3), "Known native navigation failure used the ordinary load timeout")
        self.assertEqual(statuses, ["retrying", "retrying", "load_error"])
        self.assertEqual(self.sequence.count("navigate"), 3)
        self.complete.assert_not_called()
        self.error.assert_not_called()

    def test_stale_navigation_completion_does_not_fail_newer_navigation(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        observed = threading.Event()
        states = []
        diagnostics = []
        status = Mock()

        class RecordingWatchdog(ViewerNavigationWatchdog):
            def observe(watchdog, state, now):
                states.append(state)
                action = super().observe(state, now)
                if len(states) >= 2:
                    observed.set()
                return action

        def navigate(script):
            task = self.execute_script(script)
            if script.startswith("window.location.replace("):
                for navigation_id in (1, 2):
                    self.core.NavigationStarting.handlers[0](self.core, SimpleNamespace(
                        Uri="https://global.novelpia.com/viewer/42", NavigationId=navigation_id,
                    ))
                # The superseded document can report either a failure or a
                # successful completion after the replacement has started.
                for success in (False, True):
                    self.core.NavigationCompleted.handlers[0](self.core, SimpleNamespace(
                        NavigationId=1, IsSuccess=success, WebErrorStatus="ConnectionAborted",
                    ))
            return task

        self.core.ExecuteScriptAsync.side_effect = navigate
        self.read_script.return_value = SimpleNamespace(
            IsCompleted=True, IsFaulted=False, IsCanceled=False,
            Result=json.dumps({"__pia_viewer_state": "ad"}),
        )
        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=lambda now, **settings: RecordingWatchdog(now, retry_delay=0)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, diagnostics.append,
                                   on_navigation_status=status)
            self.assertTrue(observed.wait(2))
            self.assertFalse(any("navigation finished" in item for item in diagnostics))
            self.core.NavigationCompleted.handlers[0](self.core, SimpleNamespace(
                NavigationId=2, IsSuccess=True, WebErrorStatus="Unknown",
            ))
        self.assertEqual(states, ["ad", "ad"])
        self.assertEqual(sum("navigation finished" in item for item in diagnostics), 1)
        self.assertEqual(self.sequence.count("navigate"), 1)
        status.assert_not_called()
        self.error.assert_not_called()

    def test_stale_ssr_error_during_navigation_uses_load_timeout(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        observations = []
        retried = threading.Event()
        statuses = []
        watchdog = ViewerNavigationWatchdog(0, load_timeout=30, retry_delay=5)
        observed_times = iter((0, 6, 34, 35))

        def observe(state, _now):
            instant = next(observed_times)
            action = watchdog.observe(state, instant)
            observations.append((state, instant, action))
            return action

        def navigate(script):
            task = self.execute_script(script)
            if script.startswith("window.location.replace("):
                navigation_id = self.sequence.count("navigate")
                self.core.NavigationStarting.handlers[0](self.core, SimpleNamespace(
                    Uri="https://global.novelpia.com/viewer/42", NavigationId=navigation_id,
                ))
                # Leave navigation pending while the old document's Nuxt
                # payload still reports its server error.
                if navigation_id == 2:
                    retried.set()
            return task

        self.core.ExecuteScriptAsync.side_effect = navigate
        self.read_script.return_value = SimpleNamespace(
            IsCompleted=True, IsFaulted=False, IsCanceled=False,
            Result=json.dumps({"__pia_viewer_state": "error", "status": 500, "code": "0034"}),
        )
        with patch("src.ad_navigation.ViewerNavigationWatchdog", return_value=SimpleNamespace(observe=observe)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=statuses.append)
            self.assertTrue(retried.wait(3))
        self.assertEqual(observations, [
            ("loading", 0, None), ("loading", 6, None),
            ("loading", 34, None), ("loading", 35, "retry"),
        ])
        self.assertEqual(statuses, ["retrying"])
        self.assertEqual(self.sequence.count("navigate"), 2)
        self.error.assert_not_called()

    def test_trusted_sign_in_pages_are_interactive_without_navigation_retry(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        login_urls = (
            "https://accounts.google.com/v3/signin/identifier",
            "https://global.novelpia.com/login",
            "https://global.novelpia.com/auth/callback",
        )
        self.core.Source = login_urls[0]
        observed = threading.Event()
        states = []
        status = Mock()

        class RecordingWatchdog(ViewerNavigationWatchdog):
            def observe(watchdog, state, now):
                states.append(state)
                action = super().observe(state, now)
                if len(states) < len(login_urls):
                    self.core.Source = login_urls[len(states)]
                else:
                    observed.set()
                return action

        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=lambda now, **settings: RecordingWatchdog(now - 1000, load_timeout=0.001, retry_delay=0)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=status)
            self.assertTrue(observed.wait(2))
        self.assertEqual(states, ["interactive"] * len(login_urls))
        self.assertEqual(self.sequence.count("navigate"), 1)
        self.read_script.assert_not_called()
        status.assert_not_called()
        self.error.assert_not_called()

    def test_healthy_ad_marker_does_not_trigger_navigation_retry(self):
        from src.ad_navigation import ViewerNavigationWatchdog

        observed = threading.Event()
        reads = 0
        status = Mock()

        def read_ad(_script):
            nonlocal reads
            reads += 1
            if reads >= 3:
                observed.set()
            return SimpleNamespace(IsCompleted=True, IsFaulted=False, IsCanceled=False,
                                   Result=json.dumps({"__pia_viewer_state": "ad"}))

        self.read_script.side_effect = read_ad
        with patch("src.ad_navigation.ViewerNavigationWatchdog", side_effect=lambda now, **settings: ViewerNavigationWatchdog(now - 1000, load_timeout=0.001, retry_delay=0)):
            install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=status)
            self.assertTrue(observed.wait(2))
        status.assert_not_called()
        self.assertEqual(self.sequence.count("navigate"), 1)
        self.complete.assert_not_called()
        self.error.assert_not_called()

    def test_late_navigation_failure_cannot_override_delivered_chapter(self):
        status = Mock()
        done = threading.Event()
        self.complete.side_effect = lambda payload: done.set()
        self.read_script.return_value = SimpleNamespace(
            IsCompleted=True, IsFaulted=False, IsCanceled=False, Result=json.dumps(ticket()),
        )
        install_viewer_handoff(self.window, 42, self.complete, self.error, on_navigation_status=status)
        response = SimpleNamespace(StatusCode=200, GetContentAsync=Mock(return_value=Task()))
        args = SimpleNamespace(Request=SimpleNamespace(Uri=API + "/content?_t=real-token"), Response=response)
        with patch("src.ad_viewer._read_response", return_value=content()):
            self.core.WebResourceResponseReceived.handlers[0](self.core, args)
            self.assertTrue(done.wait(1))
        self.core.Source = "about:blank"
        self.core.NavigationCompleted.handlers[0](
            self.core, SimpleNamespace(IsSuccess=False, WebErrorStatus="ConnectionAborted"),
        )
        self.complete.assert_called_once()
        self.assertTrue(validate_handoff(self.complete.call_args.args[0], 42))
        status.assert_not_called()
        self.error.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute the document-start guard")
class SpoilerGuardTests(unittest.TestCase):
    def test_ssr_errors_are_sanitized_and_valid_later_ticket_beats_stale_ad(self):
        script = _SSR_TICKET_JS.replace("__EPISODE_PATH__", '"/viewer/42"').replace("__EPISODE_NO__", "42")
        harness = r"""
const vm = require('vm');
const script = JSON.parse(process.argv[1]);
function run(first, second) {
  const window = {__NUXT__: {data: {viewer_42: first}}}; window.top = window;
  const app = {config: {globalProperties: {$nuxt: {payload: {data: {viewer_42: second}}}}}};
  return vm.runInNewContext(script, {window,
    location: {origin: 'https://global.novelpia.com', pathname: '/viewer/42'},
    document: {querySelector: () => ({__vue_app__: app})}});
}
const secretError = {statusCode: 500, code: 'PRIVATE_TOKEN', errmsg: 'PRIVATE_ACCOUNT_MESSAGE',
  result: {_t: 'PRIVATE_SESSION', data: {episode_no: 42, epi_content: 'PRIVATE_CHAPTER'}}};
const ad = {...secretError, code: '0010'};
const valid = {statusCode: 200, result: {_t: 'actual-ticket', data: {episode_no: 42}}};
console.log(JSON.stringify([
  run(secretError, null), run(ad, null), run(ad, valid), run(secretError, valid),
  run({...secretError, code: '0034'}, null),
  run({...secretError, code: '0001', result: {...secretError.result, name: 'AUTH_ERROR'}}, null),
  run({...secretError, code: '0001', result: {...secretError.result, name: 'OTHER_ERROR'}}, null)
]));
"""
        process = subprocess.run([shutil.which("node"), "-e", harness, json.dumps(script)], capture_output=True, text=True, check=True)
        results = json.loads(process.stdout)
        self.assertEqual(results[0], {"__pia_viewer_state": "error", "status": 500, "code": ""})
        self.assertEqual(results[1], {"__pia_viewer_state": "ad"})
        self.assertEqual(results[2]["result"]["_t"], "actual-ticket")
        self.assertEqual(results[2], results[3])
        self.assertEqual(results[4], {"__pia_viewer_state": "error", "status": 500, "code": "0034"})
        self.assertEqual(results[5], {"__pia_viewer_state": "interactive"})
        self.assertEqual(results[6], {"__pia_viewer_state": "error", "status": 500, "code": "0001"})
        self.assertNotIn("PRIVATE_", process.stdout)

    def test_reads_only_the_matching_hydrated_server_ticket(self):
        script = _SSR_TICKET_JS.replace("__EPISODE_PATH__", '"/viewer/42"').replace("__EPISODE_NO__", "42")
        harness = r"""
const vm = require('vm');
const script = JSON.parse(process.argv[1]);
const ticket = {statusCode: 200, result: {_t: 'from-SSR', data: {episode_no: 42}}};
function run(origin, path, payload, fallback=false) {
  const window = {__NUXT__: fallback ? undefined : {data: {viewer_42: payload}}};
  window.top = window;
  const app = {config: {globalProperties: {$nuxt: {payload: {data: {viewer_42: payload}}}}}};
  return vm.runInNewContext(script, {window, location: {origin, pathname: path},
    document: {querySelector: () => fallback ? {__vue_app__: app} : null}});
}
console.log(JSON.stringify([
  run('https://global.novelpia.com', '/viewer/42', ticket),
  run('https://global.novelpia.com', '/viewer/42', ticket, true),
  run('https://global.novelpia.com', '/viewer/43', ticket),
  run('https://evil.example', '/viewer/42', ticket),
  run('https://global.novelpia.com', '/viewer/42', {...ticket, statusCode: 500}),
  run('https://global.novelpia.com', '/viewer/42', {...ticket, result: {...ticket.result, data: {episode_no: 43}}})
]));
"""
        process = subprocess.run([shutil.which("node"), "-e", harness, json.dumps(script)], capture_output=True, text=True, check=True)
        results = json.loads(process.stdout)
        self.assertEqual(results[0]["result"]["_t"], "from-SSR")
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[2:4], [None] * 2)
        self.assertEqual(results[4], {"__pia_viewer_state": "error", "status": 500, "code": ""})
        self.assertIsNone(results[5])

    def test_early_guard_waits_for_root_and_keeps_ad_containers_visible(self):
        script = _SPOILER_GUARD_JS.replace("__EPISODE_PATH__", '"/viewer/42"')
        harness = r"""
const vm = require('vm');
const guard = JSON.parse(process.argv[1]);
function run(origin, path, framed) {
  let observer = null;
  let inserted = null;
  const window = {};
  window.top = framed ? {} : window;
  const document = {
    documentElement: null,
    createElement: tag => ({tag}),
    getElementById: id => inserted && inserted.id === id ? inserted : null
  };
  const context = {window, document, location: {origin, pathname: path},
    MutationObserver: function(callback) { observer = callback; this.observe = () => {}; }};
  vm.runInNewContext(guard, context);
  if (!observer) return {active: false};
  document.documentElement = {prepend: node => {inserted = node;}};
  observer();
  const first = inserted;
  observer();
  if (inserted !== first) throw new Error('Replaced the guard unnecessarily');
  inserted = null;
  observer();
  return {active: true, css: inserted.textContent};
}
console.log(JSON.stringify([
  run('https://global.novelpia.com', '/viewer/42', false),
  run('https://global.novelpia.com', '/viewer/43', false),
  run('https://accounts.google.com', '/viewer/42', false),
  run('https://global.novelpia.com', '/viewer/42', true)
]));
"""
        process = subprocess.run([shutil.which("node"), "-e", harness, json.dumps(script)], capture_output=True, text=True, check=True)
        results = json.loads(process.stdout)
        self.assertTrue(results[0]["active"])
        css = results[0]["css"]
        self.assertIn("#book-content *", css)
        self.assertIn("visibility: hidden !important", css)
        self.assertNotIn("#book-box", css)
        self.assertNotIn("ez-rewarded", css)
        self.assertNotIn("ezoic-pub-ad", css)
        self.assertFalse(any(item["active"] for item in results[1:]))


if __name__ == "__main__":
    unittest.main()
