import collections
import threading
import unittest
from unittest.mock import call, patch

import requests

from src.advertisements import AdvertisementError
from src.api import NovelpiaClient, cancel_event


class ChapterRetrySettingTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()
        self.addCleanup(cancel_event.clear)
        self.episode = {"episode_no": 42, "epi_title": "Example chapter"}

    def client(self, **settings):
        client = NovelpiaClient(throttle=0, email="test@example.invalid", password="test", **settings)
        client.tokens.login_at = "test-token"
        self.addCleanup(client.s.close)
        return client

    def test_default_allows_ten_recovery_attempts_after_the_initial_fetch(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                client = self.client()
                failure = {"error": "Temporary server failure", "idx": 1}
                with (
                    patch.object(client, "fetch_episode", return_value=failure) as fetch,
                    patch.object(cancel_event, "wait", return_value=False) as wait,
                    patch.object(client, "refresh") as refresh,
                    patch.object(client, "login") as login,
                    patch("builtins.print"),
                ):
                    results = client.fetch_episodes_parallel([self.episode], max_workers=workers)
                self.assertEqual(client.recover_attempts, 10)
                self.assertEqual((client.recover_cooldown_min, client.recover_cooldown_max), (5, 5))
                self.assertEqual(fetch.call_args_list, [call(self.episode, 1)] * 11)
                self.assertEqual(wait.call_args_list, [call(5.0)] * 10)
                self.assertEqual(refresh.call_count, 10)
                login.assert_not_called()
                self.assertIs(results[0], failure)

    def test_custom_retry_count_and_fixed_cooldown_apply_to_each_chapter_in_both_modes(self):
        episodes = [self.episode, {"episode_no": 43, "epi_title": "Second chapter"}]
        for workers in (1, 2):
            with self.subTest(workers=workers):
                client = self.client(ad_retries="3", ad_retry_cooldown="7.25")
                counts = collections.Counter()
                lock = threading.Lock()

                def fetch(episode, idx):
                    with lock:
                        counts[episode["episode_no"]] += 1
                    return {"error": f"Failure for {idx}", "epi_no": episode["episode_no"], "idx": idx}

                with (
                    patch.object(client, "fetch_episode", side_effect=fetch),
                    patch.object(cancel_event, "wait", return_value=False) as wait,
                    patch.object(client, "refresh"),
                    patch.object(client, "login") as login,
                    patch("builtins.print"),
                ):
                    results = client.fetch_episodes_parallel(episodes, max_workers=workers)
                self.assertEqual(counts, {42: 4, 43: 4})
                self.assertEqual(wait.call_args_list, [call(7.25)] * 6)
                self.assertEqual([result["idx"] for result in results], [1, 2])
                login.assert_not_called()

    def test_success_stops_recovery_before_the_configured_limit(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                client = self.client(ad_retries=3, ad_retry_cooldown=2.5)
                success = {"html": "<p>Received</p>", "epi_no": 42, "idx": 1}
                with (
                    patch.object(client, "fetch_episode", side_effect=[
                        {"error": "Initial failure"}, {"error": "Retry failure"}, success,
                    ]) as fetch,
                    patch.object(cancel_event, "wait", return_value=False) as wait,
                    patch.object(client, "refresh") as refresh,
                    patch.object(client, "login") as login,
                    patch("builtins.print"),
                ):
                    results = client.fetch_episodes_parallel([self.episode], max_workers=workers)
                self.assertIs(results[0], success)
                self.assertEqual(fetch.call_count, 3)
                self.assertEqual(wait.call_args_list, [call(2.5)] * 2)
                self.assertEqual(refresh.call_count, 2)
                login.assert_not_called()

    def test_zero_retries_preserves_the_initial_error_without_waiting_or_authentication(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                client = self.client(ad_retries=0, ad_retry_cooldown=600)
                initial = {"error": "Original detailed failure", "epi_no": 42, "idx": 1}
                with (
                    patch.object(client, "fetch_episode", return_value=initial) as fetch,
                    patch.object(client, "_recover_episode", wraps=client._recover_episode) as recover,
                    patch.object(cancel_event, "wait", return_value=False) as wait,
                    patch.object(client, "refresh") as refresh,
                    patch.object(client, "login") as login,
                    patch("builtins.print"),
                ):
                    results = client.fetch_episodes_parallel([self.episode], max_workers=workers)
                self.assertIs(results[0], initial)
                fetch.assert_called_once_with(self.episode, 1)
                recover.assert_not_called()
                wait.assert_not_called()
                refresh.assert_not_called()
                login.assert_not_called()

    def test_cancellation_interrupts_the_configured_cooldown_before_auth_or_another_fetch(self):
        for workers in (1, 2):
            with self.subTest(workers=workers):
                cancel_event.clear()
                client = self.client(ad_retries=10, ad_retry_cooldown=600)

                def cancel_during_wait(seconds):
                    self.assertEqual(seconds, 600)
                    cancel_event.set()
                    return True

                with (
                    patch.object(client, "fetch_episode", return_value={"error": "Initial failure"}) as fetch,
                    patch.object(cancel_event, "wait", side_effect=cancel_during_wait) as wait,
                    patch.object(client, "refresh") as refresh,
                    patch.object(client, "login") as login,
                    patch("builtins.print"),
                ):
                    if workers == 1:
                        results = client.fetch_episodes_parallel([self.episode], max_workers=workers)
                        self.assertEqual(results[0]["error"], "cancelled")
                    else:
                        with self.assertRaisesRegex(KeyboardInterrupt, "Cancelled"):
                            client.fetch_episodes_parallel([self.episode], max_workers=workers)
                fetch.assert_called_once_with(self.episode, 1)
                wait.assert_called_once_with(600.0)
                refresh.assert_not_called()
                login.assert_not_called()

    def test_terminal_advertisement_error_never_restarts_the_viewer_via_chapter_recovery(self):
        for workers in (1, 2):
            for initial_network_failure in (False, True):
                with self.subTest(workers=workers, initial_network_failure=initial_network_failure):
                    client = self.client(ad_retries=10, ad_retry_cooldown=2.5)
                    failures = [AdvertisementError("Viewer exhausted its configured retries")]
                    if initial_network_failure:
                        failures.insert(0, requests.ConnectionError("Temporary connection failure"))
                    with (
                        patch.object(client, "episode_ticket", side_effect=failures) as ticket,
                        patch.object(cancel_event, "wait", return_value=False) as wait,
                        patch.object(client, "refresh") as refresh,
                        patch.object(client, "login") as login,
                        patch("builtins.print"),
                    ):
                        results = client.fetch_episodes_parallel([self.episode], max_workers=workers)
                    self.assertFalse(results[0]["retryable"])
                    self.assertEqual(results[0]["error"], "Viewer exhausted its configured retries")
                    self.assertEqual(ticket.call_count, 1 + int(initial_network_failure))
                    self.assertEqual(wait.call_args_list, [call(2.5)] * int(initial_network_failure))
                    self.assertEqual(refresh.call_count, int(initial_network_failure))
                    login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
