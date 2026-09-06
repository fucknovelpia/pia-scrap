import json
import shutil
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda callback: callback)}).start()
        self.sequence = []
        self.task = Task()
        self.core = SimpleNamespace(
            WebResourceResponseReceived=Event(),
            AddScriptToExecuteOnDocumentCreatedAsync=Mock(side_effect=self.register_script),
            Source="https://global.novelpia.com/viewer/42",
            ExecuteScriptAsync=Mock(return_value=SimpleNamespace(
                IsCompleted=True, IsFaulted=False, IsCanceled=False, Result="null",
            )),
        )
        self.window = SimpleNamespace(
            events=SimpleNamespace(loaded=Event(), closed=Event()),
            native=SimpleNamespace(webview=SimpleNamespace(CoreWebView2=self.core)),
            show=Mock(side_effect=lambda: self.sequence.append("show")),
            load_url=Mock(side_effect=lambda url: self.sequence.append("navigate")),
        )
        self.window.events.loaded.set()
        self.window.native.BeginInvoke = Mock(side_effect=lambda callback: callback())
        self.complete = Mock()
        self.error = Mock()

    def tearDown(self):
        for handler in self.window.events.closed.handlers:
            handler()

    def register_script(self, script):
        self.assertEqual(len(self.core.WebResourceResponseReceived.handlers), 1)
        self.assertIn('"/viewer/42"', script)
        self.sequence.append("register")
        return self.task

    def install(self):
        install_viewer_handoff(self.window, 42, self.complete, self.error)

    def test_observer_and_document_start_registration_finish_before_navigation(self):
        def await_registration(task, timeout, cancelled):
            self.assertEqual(self.sequence, ["register"])
            self.window.show.assert_not_called()
            self.window.load_url.assert_not_called()
            self.sequence.append("registered")
            return "script-id"

        with patch("src.ad_viewer._wait_task", side_effect=await_registration):
            self.install()
        self.assertEqual(self.sequence, ["register", "registered", "show", "navigate"])
        self.window.load_url.assert_called_once_with("https://global.novelpia.com/viewer/42")
        self.error.assert_not_called()

    def test_failed_script_registration_never_exposes_or_navigates_viewer(self):
        self.task.IsFaulted = True
        self.install()
        self.error.assert_called_once()
        self.window.show.assert_not_called()
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
        self.core.ExecuteScriptAsync.return_value = hydration
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
        self.assertIn("viewer_", self.core.ExecuteScriptAsync.call_args.args[0])


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute the document-start guard")
class SpoilerGuardTests(unittest.TestCase):
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
        self.assertEqual(results[2:], [None] * 4)

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
