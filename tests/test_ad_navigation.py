import unittest

from src.ad_navigation import ViewerNavigationWatchdog


class ViewerNavigationWatchdogTests(unittest.TestCase):
    def test_neutral_and_error_cool_down_then_exhaust_the_retry_budget(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertIsNone(watcher.observe("neutral", 0))
        self.assertIsNone(watcher.observe("error", 4))
        self.assertEqual(watcher.observe("error", 5), "retry")
        self.assertEqual(watcher.attempts, 1)
        self.assertIsNone(watcher.observe("loading", 6))
        self.assertIsNone(watcher.observe("error", 7))
        self.assertIsNone(watcher.observe("neutral", 11))
        self.assertEqual(watcher.observe("neutral", 12), "retry")
        self.assertEqual(watcher.attempts, 2)
        self.assertIsNone(watcher.observe("error", 13))
        self.assertEqual(watcher.observe("error", 18), "failed")
        self.assertEqual(watcher.observe("neutral", 100), "failed")
        self.assertEqual(watcher.attempts, 2)

    def test_a_healthy_ad_can_wait_longer_than_five_minutes(self):
        watcher = ViewerNavigationWatchdog(now=0)
        for now in (0, 30, 301, 600, 3600):
            self.assertIsNone(watcher.observe("ad", now))
        self.assertEqual(watcher.attempts, 0)

    def test_loading_retries_have_independent_full_load_deadlines(self):
        watcher = ViewerNavigationWatchdog(now=10)
        self.assertIsNone(watcher.observe("loading", 39))
        self.assertEqual(watcher.observe("loading", 40), "retry")
        self.assertIsNone(watcher.observe("loading", 40.1))
        self.assertIsNone(watcher.observe("loading", 69))
        self.assertEqual(watcher.observe("loading", 70), "retry")
        self.assertIsNone(watcher.observe("loading", 99))
        self.assertEqual(watcher.observe("loading", 100), "failed")

    def test_ticket_received_before_cooldown_cancels_the_scheduled_retry(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertIsNone(watcher.observe("neutral", 2))
        self.assertIsNone(watcher.observe("ticket", 6))
        self.assertIsNone(watcher.observe("ticket", 100))
        self.assertEqual(watcher.attempts, 0)
        self.assertIsNone(watcher.observe("error", 101))
        self.assertIsNone(watcher.observe("error", 105))
        self.assertEqual(watcher.observe("error", 106), "retry")

    def test_healthy_recovery_does_not_replenish_lifetime_retries(self):
        watcher = ViewerNavigationWatchdog(now=0, max_retries=1)
        self.assertEqual(watcher.observe("loading", 30), "retry")
        self.assertIsNone(watcher.observe("ad", 31))
        self.assertIsNone(watcher.observe("ticket", 100))
        self.assertIsNone(watcher.observe("error", 101))
        self.assertEqual(watcher.observe("error", 106), "failed")
        self.assertEqual(watcher.attempts, 1)

    def test_loading_after_a_healthy_ad_starts_a_new_deadline(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertIsNone(watcher.observe("loading", 20))
        self.assertIsNone(watcher.observe("ad", 25))
        self.assertIsNone(watcher.observe("ad", 600))
        self.assertIsNone(watcher.observe("loading", 601))
        self.assertIsNone(watcher.observe("loading", 630))
        self.assertEqual(watcher.observe("loading", 631), "retry")

    def test_manual_login_can_wait_then_resume_loading_without_refilling_retries(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertEqual(watcher.observe("loading", 30), "retry")
        for now in (31, 100, 400, 900):
            self.assertIsNone(watcher.observe("interactive", now))
        self.assertEqual(watcher.attempts, 1)
        self.assertIsNone(watcher.observe("loading", 901))
        self.assertIsNone(watcher.observe("loading", 930))
        self.assertEqual(watcher.observe("loading", 931), "retry")
        self.assertEqual(watcher.attempts, 2)
        self.assertEqual(watcher.observe("loading", 961), "failed")

    def test_transient_faults_do_not_restart_the_original_loading_deadline(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertIsNone(watcher.observe("loading", 20))
        self.assertIsNone(watcher.observe("error", 21))
        self.assertIsNone(watcher.observe("loading", 24))
        self.assertIsNone(watcher.observe("loading", 29))
        self.assertEqual(watcher.observe("loading", 30), "retry")


if __name__ == "__main__":
    unittest.main()
