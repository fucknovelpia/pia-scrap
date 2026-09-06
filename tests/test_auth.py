import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

import main
from src.api import AuthenticationError, NovelpiaClient, cancel_event, request_with_retries


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.url = "https://api-global.novelpia.com/v1/login/me"
    return result


class AuthenticationRecoveryTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()

    def test_expired_session_retries_with_refreshed_token_and_rotated_cookie(self):
        client = NovelpiaClient(userkey="user", tkey="old-cookie")
        client.tokens.login_at = "expired-token"
        calls = []

        def send(method, url, **kwargs):
            calls.append((url, dict(kwargs["headers"])))
            if url.endswith("/refresh"):
                self.assertIsNone(kwargs["headers"].get("login-at"))
                client.s.cookies.set("TKEY", "new-cookie", domain=".novelpia.com", path="/")
                return response(200, {"result": {"LOGINAT": "fresh-token"}})
            if len(calls) == 1:
                return response(401, {"errmsg": "token expired"})
            self.assertEqual(kwargs["headers"]["login-at"], "fresh-token")
            prepared = client.s.prepare_request(requests.Request("GET", url, headers=kwargs["headers"]))
            self.assertIn("TKEY=new-cookie", prepared.headers["Cookie"])
            return response(200, {"result": {"login": {"mem_no": 123}}})

        with patch.object(client.s, "request", side_effect=send), patch("src.api.save_config") as save:
            result = client.me()
        self.assertTrue(result["result"]["login"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(client.tokens.tkey, "new-cookie")
        self.assertEqual(save.call_args.args[0]["tkey"], "new-cookie")

    def test_auth_recovery_works_with_one_attempt_and_case_insensitive_header(self):
        session = Mock()
        session.request.side_effect = [response(403, {}), response(200, {})]
        refreshed = Mock(return_value="fresh")
        result = request_with_retries(
            session, "GET", "https://api-global.novelpia.com/v1/novel",
            headers={"Login-At": "expired"}, allow_refresh=True,
            refresh_fn=refreshed, max_retries=1,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.request.call_args.kwargs["headers"], {"login-at": "fresh"})
        refreshed.assert_called_once()

    def test_rejected_refresh_token_can_fall_back_to_password_login_once(self):
        session = Mock()
        session.request.side_effect = [response(401, {}), response(401, {}), response(200, {})]
        refresh = Mock(return_value="rejected-token")
        login = Mock(return_value="working-token")
        result = request_with_retries(
            session, "GET", "https://api-global.novelpia.com/v1/novel",
            allow_refresh=True, refresh_fn=refresh, login_fn=login, max_retries=1,
        )
        self.assertEqual(result.status_code, 200)
        refresh.assert_called_once()
        login.assert_called_once()
        self.assertEqual(session.request.call_args.kwargs["headers"]["login-at"], "working-token")

    def test_recovery_is_bounded_when_every_token_is_rejected(self):
        session = Mock()
        session.request.return_value = response(401, {})
        refresh = Mock(return_value="bad-refresh")
        login = Mock(return_value="bad-login")
        result = request_with_retries(
            session, "GET", "https://api-global.novelpia.com/v1/novel",
            allow_refresh=True, refresh_fn=refresh, login_fn=login,
        )
        self.assertEqual(result.status_code, 401)
        self.assertEqual(session.request.call_count, 3)

    def test_cookie_login_never_posts_missing_password_credentials(self):
        client = NovelpiaClient(userkey="user", tkey="cookie")
        with patch.object(client.s, "request") as request:
            with self.assertRaises(AuthenticationError):
                client.login()
        request.assert_not_called()

    def test_server_login_failure_is_actionable(self):
        client = NovelpiaClient(email="example@example.com", password="secret")
        with patch("src.api.request_with_retries", return_value=response(500, {})):
            with self.assertRaisesRegex(AuthenticationError, "Login with Google") as caught:
                client.login()
        self.assertNotIn("secret", str(caught.exception))

    def test_missing_token_is_not_treated_as_success(self):
        client = NovelpiaClient(email="example@example.com", password="secret")
        for body in ({"result": {}}, {"result": None}, {"result": {"LOGINAT": ""}}, []):
            with self.subTest(body=body), patch("src.api.request_with_retries", return_value=response(200, body)):
                with self.assertRaises(AuthenticationError):
                    client.login()


class AuthenticationSourceTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            login_at=None, userkey=None, tkey=None, chrome_profile=None,
            email=None, password=None, proxy=None, throttle=None,
            min_interval=0.5, max_interval=2.0, threads=1, save_session=False,
        )
        self.client = Mock()
        self.client.tokens = SimpleNamespace(login_at=None, userkey="generated", tkey=None)
        self.factory = patch("main.NovelpiaClient", return_value=self.client).start()
        patch("main.dotenv_values", return_value={}).start()
        patch.dict("main.os.environ", {}, clear=True).start()
        patch("main.save_config").start()
        self.addCleanup(patch.stopall)

    def test_saved_browser_login_wins_over_env_credentials(self):
        with patch.dict("main.os.environ", {"NOVELPIA_EMAIL": "old@example.com", "NOVELPIA_PASSWORD": "old"}):
            main.create_authenticated_client(self.args, {"login_at": "browser", "tkey": "cookie"})
        self.client.login.assert_not_called()
        self.client.me.assert_called_once()
        self.assertIsNone(self.factory.call_args.kwargs["email"])

    def test_explicit_browser_token_wins_even_if_ui_also_supplies_credentials(self):
        self.args.login_at = "browser"
        self.args.email, self.args.password = "old@example.com", "old"
        main.create_authenticated_client(self.args, {})
        self.client.login.assert_not_called()
        self.client.me.assert_called_once()

    def test_token_without_userkey_is_detected(self):
        self.args.login_at = "browser"
        main.create_authenticated_client(self.args, {})
        self.client.me.assert_called_once()
        self.client.refresh.assert_not_called()

    def test_tkey_only_import_refreshes_instead_of_using_old_saved_token(self):
        self.args.tkey = "new-account-cookie"
        main.create_authenticated_client(self.args, {"login_at": "old-account-token", "userkey": "old-user"})
        self.client.refresh.assert_called_once()
        self.client.me.assert_called_once()
        self.assertIsNone(self.factory.call_args.kwargs["userkey"])

    def test_explicit_password_login_overrides_saved_session(self):
        self.args.email, self.args.password = "chosen@example.com", "chosen-password"
        main.create_authenticated_client(self.args, {"login_at": "saved", "tkey": "saved-cookie"})
        self.client.login.assert_called_once()
        self.client.me.assert_not_called()
        self.assertIsNone(self.factory.call_args.kwargs["tkey"])

    def test_session_failure_does_not_fall_back_to_unrelated_env_password(self):
        self.client.me.side_effect = AuthenticationError("Session expired")
        with patch.dict("main.os.environ", {"NOVELPIA_EMAIL": "old@example.com", "NOVELPIA_PASSWORD": "old"}):
            with self.assertRaises(AuthenticationError):
                main.create_authenticated_client(self.args, {"login_at": "browser"})
        self.client.login.assert_not_called()

    def test_no_credentials_keeps_anonymous_download_support(self):
        main.create_authenticated_client(self.args, {})
        self.client.login.assert_not_called()
        self.client.me.assert_not_called()

    def test_env_file_is_read_from_app_directory_on_each_run(self):
        from dotenv import dotenv_values
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            with patch("main.const.APP_DIR", Path(folder)), patch("main.dotenv_values", wraps=dotenv_values):
                path.write_text("NOVELPIA_EMAIL=one@example.com\nNOVELPIA_PASSWORD=first\n", encoding="utf-8")
                main.create_authenticated_client(self.args, {})
                self.assertEqual(self.factory.call_args.kwargs["password"], "first")
                path.write_text("NOVELPIA_EMAIL=two@example.com\nNOVELPIA_PASSWORD=second\n", encoding="utf-8")
                main.create_authenticated_client(self.args, {})
                self.assertEqual(self.factory.call_args.kwargs["password"], "second")


if __name__ == "__main__":
    unittest.main()
