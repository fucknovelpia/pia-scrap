import threading
import unittest
from unittest.mock import patch

import requests

from src.api import NovelpiaClient, cancel_event


class ActiveOperations:
    """Count real overlapping fetch/recovery calls, including their thread IDs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = []

    def enter(self, operation, idx):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((operation, idx, threading.get_ident()))

    def leave(self):
        with self.lock:
            self.active -= 1


class EpisodeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()
        self.addCleanup(cancel_event.clear)
        self.client = NovelpiaClient(throttle=0, threads=4)
        self.episodes = [
            {"episode_no": 1000 + idx, "epi_title": f"Chapter {idx}"}
            for idx in range(1, 9)
        ]

    @staticmethod
    def successful(episode, idx):
        return {
            "html": f"<p>Chapter {idx}</p>",
            "epi_no": episode["episode_no"],
            "epi_title": episode["epi_title"],
            "idx": idx,
        }

    def start_run(self, progress_cb=None):
        outcome = {"finished": threading.Event()}

        def coordinate():
            outcome["coordinator_thread"] = threading.get_ident()
            try:
                outcome["results"] = self.client.fetch_episodes_parallel(
                    self.episodes, max_workers=4, progress_cb=progress_cb,
                )
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                outcome["finished"].set()

        outcome["thread"] = threading.Thread(target=coordinate, daemon=True)
        outcome["thread"].start()
        return outcome

    def finish_run(self, outcome):
        self.assertTrue(outcome["finished"].wait(5), "Episode workers did not finish after release")
        outcome["thread"].join(timeout=1)

    def test_available_worker_starts_chapter_five_while_chapter_one_waits(self):
        first_four = threading.Barrier(4)
        release_first = threading.Event()
        fifth_started = threading.Event()
        operations = ActiveOperations()
        callbacks = []

        def fetch(episode, idx):
            operations.enter("fetch", idx)
            try:
                if idx <= 4:
                    first_four.wait(timeout=5)
                if idx == 1:
                    if not release_first.wait(5):
                        raise RuntimeError("Slow chapter was not released")
                elif idx == 5:
                    fifth_started.set()
                return self.successful(episode, idx)
            finally:
                operations.leave()

        def progress(idx, ok, result):
            callbacks.append((threading.get_ident(), idx, ok, result))

        with patch.object(self.client, "fetch_episode", side_effect=fetch):
            outcome = self.start_run(progress)
            try:
                self.assertTrue(
                    fifth_started.wait(3),
                    "Chapter 5 waited for blocked chapter 1 instead of using a free worker",
                )
                self.assertFalse(release_first.is_set())
            finally:
                release_first.set()
                self.finish_run(outcome)

        self.assertNotIn("error", outcome)
        self.assertEqual(operations.peak, 4)
        self.assertEqual(operations.active, 0)
        self.assertEqual([r["idx"] for r in outcome["results"]], list(range(1, 9)))
        self.assertEqual(
            [r["epi_no"] for r in outcome["results"]],
            [ep["episode_no"] for ep in self.episodes],
        )
        self.assertCountEqual([item[1] for item in callbacks], list(range(1, 9)))
        for thread_id, idx, ok, result in callbacks:
            self.assertEqual(thread_id, outcome["coordinator_thread"])
            self.assertTrue(ok)
            self.assertEqual(result["idx"], idx)
            self.assertEqual(result["epi_no"], self.episodes[idx - 1]["episode_no"])

    def test_slow_recovery_occupies_one_worker_without_stalling_other_chapters(self):
        recovering = threading.Event()
        release_recovery = threading.Event()
        fifth_started = threading.Event()
        operations = ActiveOperations()

        def fetch(episode, idx):
            operations.enter("fetch", idx)
            try:
                if idx == 1:
                    return {"error": "Temporary network failure", "idx": idx}
                if idx <= 4 and not recovering.wait(5):
                    raise RuntimeError("Recovery did not start")
                if idx == 5:
                    fifth_started.set()
                return self.successful(episode, idx)
            finally:
                operations.leave()

        def recover(episode, idx):
            operations.enter("recover", idx)
            try:
                recovering.set()
                if not release_recovery.wait(5):
                    raise RuntimeError("Recovery was not released")
                return self.successful(episode, idx)
            finally:
                operations.leave()

        with (
            patch.object(self.client, "fetch_episode", side_effect=fetch),
            patch.object(self.client, "_recover_episode", side_effect=recover),
        ):
            outcome = self.start_run()
            try:
                self.assertTrue(recovering.wait(3), "Failed chapter never entered recovery")
                self.assertTrue(
                    fifth_started.wait(3),
                    "A slow recovery blocked the coordinator from assigning chapter 5",
                )
                self.assertFalse(release_recovery.is_set())
            finally:
                release_recovery.set()
                self.finish_run(outcome)

        self.assertNotIn("error", outcome)
        self.assertLessEqual(operations.peak, 4)
        self.assertEqual(operations.active, 0)
        first_thread = next(thread for op, idx, thread in operations.calls if op == "fetch" and idx == 1)
        recovery_thread = next(thread for op, idx, thread in operations.calls if op == "recover" and idx == 1)
        self.assertEqual(recovery_thread, first_thread)
        self.assertNotEqual(recovery_thread, outcome["coordinator_thread"])
        self.assertEqual([r["idx"] for r in outcome["results"]], list(range(1, 9)))
        self.assertTrue(all("error" not in result for result in outcome["results"]))

    def test_cancel_does_not_start_unassigned_chapters(self):
        all_initial_started = threading.Event()
        release_initial = threading.Event()
        calls = []
        lock = threading.Lock()

        def fetch(episode, idx):
            with lock:
                calls.append(idx)
                if len(calls) == 4:
                    all_initial_started.set()
            if not release_initial.wait(5):
                raise RuntimeError("Initial workers were not released")
            return self.successful(episode, idx)

        with (
            patch.object(self.client, "fetch_episode", side_effect=fetch),
            patch.object(self.client, "_recover_episode") as recover,
        ):
            outcome = self.start_run()
            try:
                self.assertTrue(all_initial_started.wait(3), "Initial worker slots did not start")
                cancel_event.set()
            finally:
                release_initial.set()
                self.finish_run(outcome)

        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        self.assertCountEqual(calls, [1, 2, 3, 4])
        recover.assert_not_called()

    def test_failed_chapters_do_not_break_other_results_or_repeat_closed_ads(self):
        recovered = []
        callbacks = []

        def fetch(episode, idx):
            if idx == 1:
                return {"error": "Advertisement closed", "idx": idx, "retryable": False}
            if idx == 2:
                raise requests.ConnectionError("Temporary connection failure")
            if idx == 3:
                return {"error": "Temporary server failure", "idx": idx}
            return self.successful(episode, idx)

        def recover(episode, idx):
            recovered.append(idx)
            if idx == 3:
                raise RuntimeError("Recovery request failed")
            return self.successful(episode, idx)

        def progress(idx, ok, result):
            callbacks.append((threading.get_ident(), idx, ok, result))

        with (
            patch.object(self.client, "fetch_episode", side_effect=fetch),
            patch.object(self.client, "_recover_episode", side_effect=recover),
        ):
            outcome = self.start_run(progress)
            self.finish_run(outcome)

        self.assertNotIn("error", outcome)
        self.assertCountEqual(recovered, [2, 3])
        results = outcome["results"]
        self.assertEqual([result["idx"] for result in results], list(range(1, 9)))
        self.assertEqual(results[0]["error"], "Advertisement closed")
        self.assertFalse(results[0]["retryable"])
        self.assertIn("Recovery request failed", results[2]["error"])
        self.assertTrue(all("error" not in results[idx - 1] for idx in (2, 4, 5, 6, 7, 8)))
        self.assertCountEqual([item[1] for item in callbacks], list(range(1, 9)))
        for thread_id, idx, ok, result in callbacks:
            self.assertEqual(thread_id, outcome["coordinator_thread"])
            self.assertEqual(result["idx"], idx)
            self.assertEqual(ok, idx not in (1, 3))


if __name__ == "__main__":
    unittest.main()
