import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import main

from src.api import NovelpiaClient, cancel_event
from src.helper import save_config
from src.novel import fetch_novel_and_episodes
from src.ui import (
    cleanup_temporary_batch_file,
    lock_spinbox_mouse_wheel,
    parse_pasted_novel_entries,
    write_temporary_batch_entries,
)


class RequestIntervalTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()
        self.addCleanup(cancel_event.clear)

    def test_extracts_cloudfront_image_cookies_from_episode_ticket(self):
        signed = {
            "CloudFront-Policy": "policy",
            "CloudFront-Key-Pair-Id": "key-id",
            "CloudFront-Signature": "signature",
            "ignored": "value",
        }
        cookies = NovelpiaClient.signed_image_cookies(
            {"result": {"signed_key": signed}}
        )
        self.assertEqual(
            cookies,
            {
                "CloudFront-Policy": "policy",
                "CloudFront-Key-Pair-Id": "key-id",
                "CloudFront-Signature": "signature",
            },
        )

    def test_uses_random_value_between_configured_bounds(self):
        client = NovelpiaClient(min_interval=0.5, max_interval=2.0)
        with (
            patch("src.api.random.uniform", return_value=1.25) as uniform,
            patch.object(cancel_event, "wait", return_value=False) as sleep,
        ):
            delay = client._sleep_request_interval()

        self.assertEqual(delay, 1.25)
        uniform.assert_called_once_with(0.5, 2.0)
        sleep.assert_called_once_with(1.25)

    def test_legacy_throttle_remains_a_fixed_interval(self):
        client = NovelpiaClient(throttle=0.75, min_interval=0.5, max_interval=2.0)
        self.assertEqual(client.interval_min, 0.75)
        self.assertEqual(client.interval_max, 0.75)
        with patch("src.api.random.uniform") as uniform:
            self.assertEqual(client._next_request_interval(), 0.75)
        uniform.assert_not_called()

    def test_rate_limit_increases_both_bounds(self):
        client = NovelpiaClient(min_interval=0.5, max_interval=2.0)
        client._on_rate_limit()
        self.assertEqual(client.interval_min, 2.0)
        self.assertEqual(client.interval_max, 3.5)

    def test_concurrent_workers_each_use_the_configured_random_pause(self):
        client = NovelpiaClient(min_interval=0.5, max_interval=2.0, threads=2)

        def fetch_episode(episode, idx):
            client._sleep_request_interval()
            return {"html": "ok", "epi_title": str(idx), "epi_no": episode["episode_no"]}

        client.fetch_episode = fetch_episode
        episodes = [{"episode_no": number} for number in range(1, 4)]
        with (
            patch("src.api.random.uniform", return_value=1.1) as uniform,
            patch.object(cancel_event, "wait", return_value=False) as sleep,
        ):
            results = client._fetch_episodes_concurrent(episodes, max_workers=2)

        self.assertEqual(len(results), 3)
        self.assertEqual(uniform.call_count, 3)
        self.assertTrue(all(call.args == (0.5, 2.0) for call in uniform.call_args_list))
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(call.args == (1.1,) for call in sleep.call_args_list))

    def test_cancel_during_request_delay_does_not_send_the_ticket(self):
        client = NovelpiaClient(throttle=10)

        def cancel_during_wait(delay):
            cancel_event.set()
            return True

        with (
            patch.object(cancel_event, "wait", side_effect=cancel_during_wait),
            patch.object(client, "_episode_ticket_response") as request,
        ):
            with self.assertRaisesRegex(requests.RequestException, "Cancelled"):
                client.episode_ticket(42)
        request.assert_not_called()

    def test_overlapping_recovery_keeps_delays_local_and_preserves_rate_limit_backoff(self):
        client = NovelpiaClient(min_interval=0.5, max_interval=2)
        client.recover_attempts = 1
        client.recover_cooldown_min = client.recover_cooldown_max = 0
        client.rotate_session_on_failure = False
        ready = threading.Barrier(3)
        release = threading.Event()
        errors = []
        recovery_delays = []

        def fetch(episode, idx):
            recovery_delays.append(client._next_request_interval())
            ready.wait(3)
            if not release.wait(3):
                raise AssertionError("Recovery was not released")
            self.assertEqual(client._next_request_interval(), 5)
            return {"html": "ok", "idx": idx}

        def recover(idx):
            try:
                result = client._recover_episode({"episode_no": idx}, idx)
                self.assertEqual(result["html"], "ok")
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(client, "fetch_episode", side_effect=fetch),
            patch("src.api.random.uniform", side_effect=lambda lower, upper: upper),
        ):
            workers = [threading.Thread(target=recover, args=(idx,), daemon=True) for idx in (1, 2)]
            for worker in workers:
                worker.start()
            try:
                ready.wait(3)
                # Unrelated workers retain their configured delay during retry.
                self.assertEqual(client._next_request_interval(), 2)
                client._on_rate_limit()
                client._on_rate_limit()
            finally:
                release.set()
                for worker in workers:
                    worker.join(3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])
            self.assertEqual(recovery_delays, [3, 3])
            self.assertEqual(client._next_request_interval(), 5)
            self.assertEqual((client.interval_min, client.interval_max), (3.5, 5))

    def test_rejects_invalid_interval_range(self):
        with self.assertRaises(ValueError):
            NovelpiaClient(min_interval=2.0, max_interval=0.5)


class ChapterRangeTests(unittest.TestCase):
    def test_chapter_range_is_inclusive(self):
        class FakeClient:
            tokens = SimpleNamespace(login_at=None)

            @staticmethod
            def novel(_novel_id):
                return {
                    "result": {
                        "novel": {
                            "novel_no": 12,
                            "novel_name": "Range Test",
                            "count_epi": 5,
                            "flag_complete": 0,
                        },
                        "writer_list": [],
                        "info": {"epi_cnt": 5},
                    }
                }

            @staticmethod
            def episode_list(_novel_id, rows):
                return {
                    "result": {
                        "list": [
                            {"episode_no": 100 + number, "epi_num": number}
                            for number in range(1, rows + 1)
                        ]
                    }
                }

        _, episodes, _ = fetch_novel_and_episodes(
            FakeClient(),
            12,
            start_chapter=2,
            end_chapter=4,
        )
        self.assertEqual([episode["epi_num"] for episode in episodes], [2, 3, 4])


class MouseWheelLockTests(unittest.TestCase):
    def test_spinbox_mouse_wheel_events_are_blocked(self):
        class FakeSpinbox:
            def __init__(self):
                self.bindings = {}

            def bind(self, sequence, callback):
                self.bindings[sequence] = callback

        spinbox = FakeSpinbox()
        lock_spinbox_mouse_wheel(spinbox)
        self.assertEqual(
            set(spinbox.bindings),
            {"<MouseWheel>", "<Button-4>", "<Button-5>"},
        )
        for callback in spinbox.bindings.values():
            self.assertEqual(callback(None), "break")


class SettingsPersistenceTests(unittest.TestCase):
    def test_partial_token_update_preserves_download_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".api.json"
            with patch("src.helper.CONFIG_PATH", config_path):
                save_config({"min_interval": 0.5, "max_interval": 2.0,
                             "ad_retries": 14, "ad_retry_cooldown": 2.5})
                save_config({"login_at": "new-token"})

            with open(config_path, encoding="utf-8") as handle:
                config = json.load(handle)
            self.assertEqual(config["min_interval"], 0.5)
            self.assertEqual(config["max_interval"], 2.0)
            self.assertEqual(config["login_at"], "new-token")
            self.assertEqual(config["ad_retries"], 14)
            self.assertEqual(config["ad_retry_cooldown"], 2.5)


class AdRetryCliSettingsTests(unittest.TestCase):
    def test_defaults_saved_values_and_explicit_overrides_reach_client_creation(self):
        cases = (
            ({}, [], (10, 5.0)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5}, [], (14, 2.5)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5},
             ["--retries", "0", "--retry-cooldown", "0"], (0, 0.0)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5}, ["--retries", "3"], (3, 2.5)),
            ({"ad_retries": "7", "ad_retry_cooldown": "4.5"},
             ["--retry-cooldown", "8"], (7, 8.0)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5},
             ["--ad-retries", "0", "--ad-retry-cooldown", "0"], (0, 0.0)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5}, ["--ad-retries", "3"], (3, 2.5)),
            ({"ad_retries": "7", "ad_retry_cooldown": "4.5"},
             ["--ad-retry-cooldown", "8"], (7, 8.0)),
            ({"ad_retries": 14, "ad_retry_cooldown": 2.5},
             ["--ad-retries", "4", "--retries", "6", "--retry-cooldown", "3",
              "--ad-retry-cooldown", "1"], (6, 1.0)),
        )
        for config, flags, expected in cases:
            with (
                self.subTest(config=config, flags=flags),
                patch("main.sys.argv", ["main.py", "123", *flags]),
                patch("main.load_config", return_value=config),
                patch("main.create_authenticated_client") as create_client,
                patch("main.run_single_build", return_value=("output", "Title", 1)),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                main.main()
                args, saved = create_client.call_args.args
                self.assertEqual((args.ad_retries, args.ad_retry_cooldown), expected)
                self.assertIs(saved, config)

    def test_invalid_cli_or_saved_retry_values_fail_before_authentication(self):
        cases = (
            ({}, ["--retries", "1.5"]),
            ({}, ["--retries", "-1"]),
            ({}, ["--retry-cooldown", "nan"]),
            ({}, ["--retry-cooldown", "inf"]),
            ({}, ["--retry-cooldown", "-1"]),
            ({}, ["--ad-retries", "1.5"]),
            ({}, ["--ad-retries", "-1"]),
            ({}, ["--ad-retry-cooldown", "nan"]),
            ({}, ["--ad-retry-cooldown", "inf"]),
            ({}, ["--ad-retry-cooldown", "-1"]),
            ({"ad_retries": True}, []),
            ({"ad_retries": 1.5}, []),
            ({"ad_retry_cooldown": float("nan")}, []),
            ({"ad_retry_cooldown": -1}, []),
        )
        for config, flags in cases:
            with (
                self.subTest(config=config, flags=flags),
                patch("main.sys.argv", ["main.py", "123", *flags]),
                patch("main.load_config", return_value=config),
                patch("main.create_authenticated_client") as create_client,
                patch("main.scrape_novel_links") as scrape,
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit) as failed:
                    main.main()
                self.assertEqual(failed.exception.code, 2)
                create_client.assert_not_called()
                scrape.assert_not_called()

    def test_old_argument_namespace_and_config_still_get_default_client_settings(self):
        args = SimpleNamespace(
            login_at=None, userkey=None, tkey=None, chrome_profile=None,
            email=None, password=None, proxy=None, throttle=None,
            min_interval=0.5, max_interval=2.0, threads=1, save_session=False,
        )
        with (
            patch("main.NovelpiaClient") as factory,
            patch("main.dotenv_values", return_value={}),
            patch.dict("main.os.environ", {}, clear=True),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            main.create_authenticated_client(args, {})
        self.assertEqual(factory.call_args.kwargs["ad_retries"], 10)
        self.assertEqual(factory.call_args.kwargs["ad_retry_cooldown"], 5.0)

    def test_direct_client_setup_rejects_invalid_settings_before_chrome_import(self):
        args = SimpleNamespace(chrome_profile="Default", ad_retries=1.5, ad_retry_cooldown=5)
        with (
            patch("main.load_chrome_novelpia_session") as chrome,
            patch("main.NovelpiaClient") as factory,
            patch("main.dotenv_values") as environment,
        ):
            with self.assertRaises(ValueError):
                main.create_authenticated_client(args, {})
        chrome.assert_not_called()
        factory.assert_not_called()
        environment.assert_not_called()


class PastedBatchTests(unittest.TestCase):
    def test_parses_urls_ids_mixed_separators_and_removes_duplicates(self):
        pasted = """
        https://global.novelpia.com/novel/4770
        123, 4770; https://global.novelpia.com/novel/456?ref=batch
        invalid https://example.com/not-a-novel
        """
        self.assertEqual(
            parse_pasted_novel_entries(pasted),
            ["4770", "123", "456"],
        )

    def test_temporary_paste_file_is_backend_compatible_and_cleaned_up(self):
        path = write_temporary_batch_entries(["4770", "123"])
        try:
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "4770\n123\n")
        finally:
            cleanup_temporary_batch_file(path)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
