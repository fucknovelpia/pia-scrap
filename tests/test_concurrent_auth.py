import threading
import unittest
from unittest.mock import patch

from src.api import AuthenticationError, NovelpiaClient, cancel_event


class ObservedRLock:
    """Expose attempted lock entries so concurrency tests need no sleeps."""

    def __init__(self, expected_entries):
        self.lock = threading.RLock()
        self.counter_lock = threading.Lock()
        self.entries = 0
        self.expected_entries = expected_entries
        self.all_entered = threading.Event()

    def __enter__(self):
        with self.counter_lock:
            self.entries += 1
            if self.entries >= self.expected_entries:
                self.all_entered.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class ConcurrentAuthenticationTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()
        self.addCleanup(cancel_event.clear)
        self.client = NovelpiaClient(email="test@example.invalid", password="test-only", throttle=0)
        self.client.tokens.login_at = "expired"
        self.client.recover_attempts = 1
        self.episode = {"episode_no": 42, "epi_title": "Test chapter"}
        self.success = {"epi_no": 42, "idx": 1, "html": "<p>Recovered</p>"}

    @staticmethod
    def join_workers(workers):
        for worker in workers:
            worker.join(timeout=3)
        if any(worker.is_alive() for worker in workers):
            raise AssertionError("Authentication worker did not finish")

    def test_simultaneous_refresh_calls_share_one_rotation_and_result(self):
        worker_count = 4
        observed_lock = ObservedRLock(worker_count)
        self.client._auth_lock = observed_lock
        start = threading.Barrier(worker_count)
        release_refresh = threading.Event()
        results, errors = [], []

        def refresh_body():
            if not release_refresh.wait(3):
                raise AssertionError("Refresh was not released")
            self.client.tokens.login_at = "fresh-shared-token"
            return self.client.tokens.login_at

        def refresh():
            try:
                start.wait(timeout=3)
                results.append(self.client.refresh())
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=refresh, daemon=True) for _ in range(worker_count)]
        with patch.object(self.client, "_refresh", side_effect=refresh_body) as private_refresh:
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(observed_lock.all_entered.wait(3), "Refresh calls did not overlap")
            finally:
                release_refresh.set()
                self.join_workers(workers)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["fresh-shared-token"] * worker_count)
        private_refresh.assert_called_once_with()
        self.assertEqual(self.client._auth_generation, 1)

    def test_login_waits_for_in_progress_refresh_before_changing_session(self):
        observed_lock = ObservedRLock(2)
        self.client._auth_lock = observed_lock
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        operations, errors = [], []

        def refresh_body():
            operations.append("refresh-start")
            refresh_started.set()
            if not release_refresh.wait(3):
                raise AssertionError("Refresh was not released")
            self.client.tokens.login_at = "refreshed"
            operations.append("refresh-end")
            return "refreshed"

        def login_body():
            operations.append("login")
            self.client.tokens.login_at = "logged-in"
            return "logged-in"

        def invoke(operation):
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)

        refresh_worker = threading.Thread(target=invoke, args=(self.client.refresh,), daemon=True)
        login_worker = threading.Thread(target=invoke, args=(self.client.login,), daemon=True)
        workers = [refresh_worker, login_worker]
        with (
            patch.object(self.client, "_refresh", side_effect=refresh_body),
            patch.object(self.client, "_login", side_effect=login_body),
        ):
            try:
                refresh_worker.start()
                self.assertTrue(refresh_started.wait(3))
                login_worker.start()
                self.assertTrue(observed_lock.all_entered.wait(3))
                self.assertEqual(operations, ["refresh-start"])
            finally:
                release_refresh.set()
                self.join_workers([worker for worker in workers if worker.ident is not None])
        self.assertEqual(errors, [])
        self.assertEqual(operations, ["refresh-start", "refresh-end", "login"])
        self.assertEqual(self.client.tokens.login_at, "logged-in")
        self.assertEqual(self.client._auth_generation, 2)

    def test_recovery_keeps_successful_refresh_without_password_login(self):
        def refresh_body():
            self.client.tokens.login_at = "fresh"
            return "fresh"

        with (
            patch("src.api.random.uniform", return_value=0),
            patch.object(self.client, "_refresh", side_effect=refresh_body) as refresh,
            patch.object(self.client, "_login") as login,
            patch.object(self.client, "fetch_episode", return_value=self.success) as fetch,
        ):
            result = self.client._recover_episode(self.episode, 1)
        self.assertIs(result, self.success)
        refresh.assert_called_once_with()
        login.assert_not_called()
        fetch.assert_called_once_with(self.episode, 1)
        self.assertEqual(self.client._auth_generation, 1)

    def test_recovery_uses_rotation_completed_during_its_cooldown(self):
        def another_worker_rotated(_seconds):
            with self.client._auth_lock:
                self.client.tokens.login_at = "another-worker-token"
                self.client._auth_generation += 1
            return False

        with (
            patch("src.api.random.uniform", return_value=0),
            patch.object(cancel_event, "wait", side_effect=another_worker_rotated) as wait,
            patch.object(self.client, "_refresh") as refresh,
            patch.object(self.client, "_login") as login,
            patch.object(self.client, "fetch_episode", return_value=self.success),
        ):
            result = self.client._recover_episode(self.episode, 1)
        self.assertIs(result, self.success)
        wait.assert_called_once_with(0)
        refresh.assert_not_called()
        login.assert_not_called()
        self.assertEqual(self.client.tokens.login_at, "another-worker-token")
        self.assertEqual(self.client._auth_generation, 1)

    def test_failed_refresh_retains_password_fallback(self):
        def login_body():
            self.client.tokens.login_at = "logged-in"
            return "logged-in"

        with (
            patch("src.api.random.uniform", return_value=0),
            patch.object(self.client, "_refresh", side_effect=AuthenticationError("Session expired")) as refresh,
            patch.object(self.client, "_login", side_effect=login_body) as login,
            patch.object(self.client, "fetch_episode", return_value=self.success),
        ):
            result = self.client._recover_episode(self.episode, 1)
        self.assertIs(result, self.success)
        refresh.assert_called_once_with()
        login.assert_called_once_with()
        self.assertEqual(self.client._auth_generation, 1)


if __name__ == "__main__":
    unittest.main()
