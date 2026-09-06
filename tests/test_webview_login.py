import json
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.webview_login import (
    FETCH_REFRESH_JS,
    LOGIN_URL,
    _get_auth_cookies,
    _is_login_origin,
    _login_at_from_refresh,
    _poll_for_login,
    _run_webview_login,
    _try_refresh_token,
    _write_result,
)


def make_window():
    window = Mock()
    window.events.loaded.is_set.return_value = True
    window.get_current_url.return_value = LOGIN_URL
    window.evaluate_js.return_value = ""
    window.get_cookies.return_value = []
    return window


class BrowserLoginTests(unittest.TestCase):
    def test_login_is_detected_after_initial_failure_without_google_navigation(self):
        window = make_window()
        stopped = threading.Event()
        with (
            patch("src.webview_login._try_refresh_token", side_effect=[None, "new-session"]) as refresh,
            patch.object(stopped, "wait", return_value=False),
        ):
            result = _poll_for_login(window, stopped, Mock())
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(result, {
            "status": "success", "login_at": "new-session", "userkey": "", "tkey": "",
        })

    def test_waits_for_trusted_origin_before_refreshing(self):
        window = make_window()
        window.get_current_url.side_effect = [
            "https://accounts.google.com/signin", "https://global.novelpia.com.evil.test/",
            LOGIN_URL, LOGIN_URL,
        ]
        stopped = threading.Event()
        with (
            patch("src.webview_login._try_refresh_token", return_value="new-session") as refresh,
            patch.object(stopped, "wait", return_value=False),
        ):
            self.assertEqual(_poll_for_login(window, stopped, Mock())["status"], "success")
        refresh.assert_called_once()

    def test_native_cookies_capture_httponly_session_and_preserve_equals(self):
        window = make_window()
        window.evaluate_js.return_value = "USERKEY=old; unrelated=ignore; TKEY=visible=="
        cookie = SimpleCookie()
        cookie["USERKEY"] = "native-session"
        cookie["USERKEY"]["httponly"] = True
        window.get_cookies.return_value = [cookie]
        self.assertEqual(_get_auth_cookies(window), {"USERKEY": "native-session", "TKEY": "visible=="})

    def test_document_cookies_still_work_without_native_cookie_support(self):
        window = make_window()
        window.evaluate_js.return_value = "TKEY=session=="
        window.get_cookies.side_effect = NotImplementedError()
        self.assertEqual(_get_auth_cookies(window), {"TKEY": "session=="})

    def test_closing_browser_interrupts_pending_refresh(self):
        window = make_window()
        window.evaluate_js.return_value = True
        stopped = threading.Event()

        def close(_seconds):
            stopped.set()
            return True

        with patch.object(stopped, "wait", side_effect=close):
            result = _poll_for_login(window, stopped, Mock())
        self.assertEqual(result, {"status": "cancelled"})
        window.evaluate_js.assert_called_once_with(FETCH_REFRESH_JS)
        window.get_cookies.assert_not_called()

    def test_refresh_rejects_server_errors_and_does_not_log_response_secrets(self):
        secret = "secret-session-token"
        window = make_window()
        state = {"done": True, "status": 500, "data": {"statusCode": 200, "result": {"LOGINAT": secret}}}
        window.evaluate_js.side_effect = [True, state]
        stopped = threading.Event()
        messages = []
        with patch.object(stopped, "wait", return_value=False):
            self.assertIsNone(_try_refresh_token(window, stopped, messages.append))
        self.assertIn("HTTP 500", " ".join(messages))
        self.assertNotIn(secret, " ".join(messages))
        window.evaluate_js.side_effect = RuntimeError(secret)
        self.assertIsNone(_try_refresh_token(window, stopped, messages.append))
        self.assertNotIn(secret, " ".join(messages))

    def test_refresh_validates_both_http_and_api_response(self):
        for body in (None, [], {"statusCode": 500}, {"statusCode": 200, "result": []}):
            with self.subTest(body=body):
                self.assertIsNone(_login_at_from_refresh({"done": True, "status": 200, "data": body}))
        self.assertEqual(_login_at_from_refresh({
            "done": True, "status": 200,
            "data": {"statusCode": "200", "result": {"LOGINAT": " current-session "}},
        }), "current-session")

    def test_origin_requires_https_and_correct_hostname_and_port(self):
        self.assertTrue(_is_login_origin(LOGIN_URL))
        self.assertTrue(_is_login_origin("https://global.novelpia.com:443/sign"))
        for url in (
            "http://global.novelpia.com/", "https://global.novelpia.com:8443/",
            "https://global.novelpia.com.evil.test/", "https://evil.test/?global.novelpia.com",
            "https://global.novelpia.com:bad/", "",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_login_origin(url))

    def test_atomic_result_replaces_existing_payload_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.loginkey"
            path.write_text("old", encoding="utf-8")
            _write_result(str(path), {"status": "cancelled"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "cancelled"})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_publish_keeps_previous_complete_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.loginkey"
            path.write_text('{"status":"old"}', encoding="utf-8")
            with patch("src.webview_login.os.replace", side_effect=PermissionError()):
                with self.assertRaises(PermissionError):
                    _write_result(str(path), {"status": "new"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "old"})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_window_close_publishes_cancelled_and_keeps_popups_in_session_browser(self):
        class ClosedEvent:
            def __iadd__(self, callback):
                self.callback = callback
                return self

        window = make_window()
        closed = ClosedEvent()
        window.events = SimpleNamespace(closed=closed)
        fake_webview = SimpleNamespace(
            settings={"OPEN_EXTERNAL_LINKS_IN_BROWSER": True},
            create_window=Mock(return_value=window),
            start=Mock(side_effect=lambda *args, **kwargs: closed.callback()),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.const.APP_DIR", Path(directory)),
            patch.dict("sys.modules", {"webview": fake_webview, "pythonnet": SimpleNamespace(load=Mock())}),
        ):
            path = Path(directory) / "result.loginkey"
            _run_webview_login(str(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "cancelled"})
        self.assertFalse(fake_webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"])


if __name__ == "__main__":
    unittest.main()
