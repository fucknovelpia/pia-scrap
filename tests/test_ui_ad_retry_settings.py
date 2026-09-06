import tempfile
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src import ui


class AdRetryDownloadUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.stack = self.enterContext(ExitStack())
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(ui.tk, "Tk", return_value=self.root))
        self.stack.enter_context(patch.object(self.root, "mainloop"))
        self.stack.enter_context(patch.object(self.root, "after", return_value="test-callback"))
        self.stack.enter_context(patch.object(self.root, "after_cancel"))
        self.stack.enter_context(patch.object(ui, "ENV_PATH", directory / "missing.env"))
        self.stack.enter_context(patch.object(ui, "LOG_DIR", directory / "logs"))
        self.stack.enter_context(patch.object(ui, "list_chrome_profiles", return_value=[]))
        self.config = self.stack.enter_context(patch.object(ui, "load_config", return_value={}))
        self.save = self.stack.enter_context(patch.object(ui, "save_config"))
        self.error = self.stack.enter_context(patch.object(ui.messagebox, "showerror"))
        self.stack.enter_context(patch.object(ui.messagebox, "showinfo"))

    def widgets(self, parent=None):
        for child in (parent or self.root).winfo_children():
            yield child
            yield from self.widgets(child)

    def find(self, widget_type, text, parent=None):
        return next(widget for widget in self.widgets(parent)
                    if isinstance(widget, widget_type) and widget.cget("text") == text)

    def launch(self, config=None):
        self.config.return_value = config or {}
        ui.launch_ui()
        label = self.find(ui.ttk.Label, "Ad retries")
        self.retries = label.master.grid_slaves(row=3, column=1)[0]
        label = self.find(ui.ttk.Label, "Retry cooldown (s)")
        self.cooldown = label.master.grid_slaves(row=4, column=1)[0]

    def prepare_values(self, retries="4", cooldown="2.5"):
        self.retries.set(retries)
        self.cooldown.set(cooldown)

    def capture_run(self, action):
        captured = {}

        def fake_main():
            captured["args"] = list(ui.sys.argv[1:])
            if "--novel-links-file" in captured["args"]:
                source = captured["args"][captured["args"].index("--novel-links-file") + 1]
                if Path(source).is_file():
                    captured["entries"] = Path(source).read_text(encoding="utf-8")

        with (
            patch.object(ui.sys, "frozen", True, create=True),
            patch("main.main", side_effect=fake_main),
            patch.object(ui.threading, "Thread") as thread,
        ):
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            action()
        self.error.assert_not_called()
        args = captured["args"]
        self.assertEqual(args[args.index("--ad-retries") + 1], "4")
        self.assertEqual(args[args.index("--ad-retry-cooldown") + 1], "2.5")
        self.assertEqual(self.save.call_args.args[0]["ad_retries"], 4)
        self.assertEqual(self.save.call_args.args[0]["ad_retry_cooldown"], 2.5)
        return captured

    def test_legacy_config_defaults_and_save_settings_support_zero_retries(self):
        self.launch({"threads": 2})
        self.assertEqual(self.retries.get(), "10")
        self.assertEqual(float(self.cooldown.get()), 5.0)
        self.prepare_values("0", "1.25")
        self.find(ui.ttk.Button, "Save Settings").invoke()
        self.assertEqual(self.save.call_args.args[0]["ad_retries"], 0)
        self.assertEqual(self.save.call_args.args[0]["ad_retry_cooldown"], 1.25)
        self.error.assert_not_called()
        for spinbox in (self.retries, self.cooldown):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.assertTrue(spinbox.bind(sequence))

    def test_saved_settings_are_loaded_without_losing_disabled_retries(self):
        self.launch({"ad_retries": 0, "ad_retry_cooldown": 12.5})
        self.assertEqual(self.retries.get(), "0")
        self.assertEqual(float(self.cooldown.get()), 12.5)

    def test_invalid_saved_values_fall_back_without_crashing_ui(self):
        self.launch({"ad_retries": True, "ad_retry_cooldown": "nan"})
        self.assertEqual(self.retries.get(), "10")
        self.assertEqual(float(self.cooldown.get()), 5.0)

    def test_invalid_saved_count_preserves_valid_saved_cooldown(self):
        self.launch({"ad_retries": 1.5, "ad_retry_cooldown": 3.25})
        self.assertEqual(self.retries.get(), "10")
        self.assertEqual(float(self.cooldown.get()), 3.25)

    def test_invalid_typed_settings_are_not_saved_or_started(self):
        self.launch()
        for retries, cooldown in (("1.5", "5"), ("-1", "5"), ("true", "5"),
                                  ("10", "nan"), ("10", "inf"), ("10", "-1")):
            with self.subTest(retries=retries, cooldown=cooldown):
                self.prepare_values(retries, cooldown)
                self.error.reset_mock()
                with patch.object(ui.threading, "Thread") as thread:
                    self.find(ui.ttk.Button, "Save Settings").invoke()
                    self.find(ui.ttk.Button, "Run File Batch").invoke()
                self.assertEqual(self.error.call_args.args[0], "Ad retry settings")
                self.save.assert_not_called()
                thread.assert_not_called()

    def test_single_download_passes_retry_settings_to_frozen_cli(self):
        self.launch()
        self.prepare_values()
        label = self.find(ui.ttk.Label, "Novel ID or URL")
        entry = label.master.grid_slaves(row=0, column=1)[0]
        entry.insert(0, "123")
        captured = self.capture_run(self.find(ui.ttk.Button, "Download Novel").invoke)
        self.assertEqual(captured["args"][0], "123")

    def test_file_batch_passes_retry_settings_to_frozen_cli(self):
        self.launch()
        self.prepare_values()
        captured = self.capture_run(self.find(ui.ttk.Button, "Run File Batch").invoke)
        self.assertIn("--novel-links-file", captured["args"])

    def test_pasted_batch_passes_settings_and_keeps_its_novel_entries(self):
        self.launch()
        self.prepare_values()
        original_toplevel = ui.tk.Toplevel
        dialogs = []

        def hidden_dialog(*args, **kwargs):
            dialog = original_toplevel(*args, **kwargs)
            dialog.withdraw()
            dialogs.append(dialog)
            return dialog

        with patch.object(ui.tk, "Toplevel", side_effect=hidden_dialog):
            self.find(ui.ttk.Button, "Paste to Batch...").invoke()
        dialog = dialogs[0]
        text = next(widget for widget in self.widgets(dialog) if isinstance(widget, tk.Text))
        text.insert("1.0", "123\nhttps://global.novelpia.com/novel/456")
        captured = self.capture_run(self.find(ui.ttk.Button, "Run Batch", dialog).invoke)
        self.assertEqual(captured["entries"], "123\n456\n")


if __name__ == "__main__":
    unittest.main()
