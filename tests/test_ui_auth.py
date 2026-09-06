import io
import json
import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from src import ui


class AuthenticationArgumentTests(unittest.TestCase):
    def test_captured_token_wins_over_saved_password_without_userkey(self):
        self.assertEqual(
            ui.build_auth_args("old@example.test", "old-password", " access-token ", "", ""),
            ["--login-at", "access-token"],
        )

    def test_cookie_session_wins_over_saved_password(self):
        for userkey, tkey, expected in (
            ("user-cookie", "", ["--userkey", "user-cookie"]),
            ("", "refresh-cookie", ["--tkey", "refresh-cookie"]),
        ):
            with self.subTest(userkey=userkey, tkey=tkey):
                self.assertEqual(
                    ui.build_auth_args("old@example.test", "old-password", "", userkey, tkey),
                    expected,
                )

    def test_password_is_used_without_session_and_preserves_spaces(self):
        self.assertEqual(
            ui.build_auth_args(" user@example.test ", " password ", "", "", ""),
            ["--user", "user@example.test", "--pass", " password "],
        )

    def test_command_logs_redact_all_auth_values_without_changing_execution_args(self):
        args = ["123", "--out", "output", "--user", "email", "--pass", "password",
                "--login-at=token", "--userkey", "user-cookie", "--tkey", "refresh-cookie"]
        redacted = ui.redact_auth_args(args)
        self.assertEqual(redacted[:3], args[:3])
        self.assertEqual(redacted[3:], [
            "--user", "[REDACTED]", "--pass", "[REDACTED]", "--login-at=[REDACTED]",
            "--userkey", "[REDACTED]", "--tkey", "[REDACTED]",
        ])
        self.assertEqual(args[6], "password")


class LoginResultTests(unittest.TestCase):
    def test_frozen_log_keeps_bounded_recent_output_and_preserves_full_stream(self):
        queue = Queue()
        log = io.StringIO()
        writer = ui.QueueWriter(queue, log)
        old_output = "old output\n" * 10000
        error = "[error] Your session expired. Use Login with Google to sign in again.\n"
        self.assertEqual(writer.write(old_output), len(old_output))
        self.assertEqual(writer.write(error), len(error))
        self.assertEqual(log.getvalue(), old_output + error)
        self.assertEqual(queue.get_nowait() + queue.get_nowait(), old_output + error)
        self.assertEqual(len(writer.recent_output), writer.RECENT_OUTPUT_LIMIT)
        self.assertTrue(writer.recent_output.endswith(error))

    def test_empty_terminal_result_is_not_treated_as_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            self.assertIsNone(ui.read_webview_login_result(path))
            path.write_text("", encoding="utf-8")
            self.assertIsNone(ui.read_webview_login_result(path))
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(ui.read_webview_login_result(path), {})


class GoogleLoginUiTests(unittest.TestCase):
    """Exercise the real Tk callbacks without starting a browser or using an account."""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.stack = self.enterContext(ExitStack())
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.scheduled = []

        def schedule(_delay, callback, *args):
            self.scheduled.append((callback, args))
            return f"test-{len(self.scheduled)}"

        self.stack.enter_context(patch.object(ui.tk, "Tk", return_value=self.root))
        self.stack.enter_context(patch.object(self.root, "mainloop"))
        self.stack.enter_context(patch.object(self.root, "after", side_effect=schedule))
        self.stack.enter_context(patch.object(self.root, "after_cancel"))
        self.stack.enter_context(patch.object(ui, "ENV_PATH", Path(directory) / "missing.env"))
        self.stack.enter_context(patch.object(ui, "load_config", return_value={
            "login_at": "stale-token", "userkey": "stale-user", "tkey": "stale-refresh",
        }))
        self.stack.enter_context(patch.object(ui, "list_chrome_profiles", return_value=[]))
        self.save = self.stack.enter_context(patch.object(ui, "save_config"))
        self.error = self.stack.enter_context(patch.object(ui.messagebox, "showerror"))
        self.process = Mock()
        self.process.is_alive.return_value = True
        self.process.terminate.side_effect = lambda: setattr(self.process.is_alive, "return_value", False)
        self.context = self.stack.enter_context(patch.object(ui.multiprocessing, "get_context"))
        self.context.return_value.Process.return_value = self.process
        ui.launch_ui()
        self.login_button = next(
            widget for widget in self.walk_widgets(self.root)
            if isinstance(widget, ui.ttk.Button) and widget.cget("text") == "Login with Google"
        )

    def walk_widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.walk_widgets(child)

    def begin_login(self, payload):
        def start():
            path = self.context.return_value.Process.call_args.kwargs["args"][0]
            self.result_path = Path(path)
            if payload is not None:
                self.result_path.write_text(json.dumps(payload), encoding="utf-8")

        self.process.start.side_effect = start
        self.login_button.invoke()
        self.assertEqual(str(self.login_button.cget("state")), "disabled")
        callback, args = self.scheduled[-1]
        callback(*args)

    def test_success_updates_and_saves_session_on_tk_callback_and_clears_stale_cookies(self):
        self.begin_login({"status": "success", "login_at": "new-token"})
        self.save.assert_called_once_with({"login_at": "new-token", "userkey": "", "tkey": ""})
        fields = {
            int(widget.grid_info()["row"]): self.root.getvar(widget.cget("textvariable"))
            for widget in self.login_button.master.winfo_children()
            if isinstance(widget, ui.ttk.Entry)
        }
        self.assertEqual([fields[8], fields[9], fields[10]], ["new-token", "", ""])
        self.assertEqual(str(self.login_button.cget("state")), "normal")
        self.assertFalse(self.result_path.exists())
        self.process.terminate.assert_called_once()
        self.error.assert_not_called()

    def test_empty_result_finishes_immediately_instead_of_waiting_fifteen_minutes(self):
        self.begin_login({})
        self.assertEqual(str(self.login_button.cget("state")), "normal")
        self.assertFalse(self.result_path.exists())
        self.save.assert_not_called()
        self.error.assert_called_once()

    def test_child_exit_without_result_is_reported_immediately(self):
        self.process.is_alive.return_value = False
        self.begin_login(None)
        self.assertEqual(str(self.login_button.cget("state")), "normal")
        self.assertFalse(self.result_path.exists())
        self.assertIn("exited", self.error.call_args.args[1])

    def test_user_closing_browser_cleans_up_without_error_dialog(self):
        self.begin_login({"status": "cancelled"})
        self.assertEqual(str(self.login_button.cget("state")), "normal")
        self.assertFalse(self.result_path.exists())
        self.save.assert_not_called()
        self.error.assert_not_called()

    def test_frozen_authentication_error_reaches_result_dialog(self):
        def fail_authentication():
            print("[error] Your session expired. Use Login with Google to sign in again.")
            raise SystemExit(1)

        batch_button = next(
            widget for widget in self.walk_widgets(self.root)
            if isinstance(widget, ui.ttk.Button) and widget.cget("text") == "Run File Batch"
        )
        directory = self.enterContext(tempfile.TemporaryDirectory())
        with (
            patch.object(ui.sys, "frozen", True, create=True),
            patch.object(ui, "LOG_DIR", Path(directory)),
            patch("main.main", side_effect=fail_authentication),
            patch.object(ui.threading, "Thread") as thread,
        ):
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            batch_button.invoke()
        # Invoke after the worker and its exception scope have both ended.
        callback, args = self.scheduled[-1]
        callback(*args)
        self.error.assert_called_once()
        self.assertIn("Your session expired. Use Login with Google", self.error.call_args.args[1])
        self.assertNotIn("Exit code 1", self.error.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
