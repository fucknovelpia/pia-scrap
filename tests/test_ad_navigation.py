import unittest

from src.ad_navigation import (
    DEFAULT_AD_RETRIES, DEFAULT_AD_RETRY_COOLDOWN,
    ViewerNavigationWatchdog, validate_ad_retry_settings,
)


class ViewerNavigationWatchdogTests(unittest.TestCase):
    def test_neutral_and_error_cool_down_then_exhaust_the_retry_budget(self):
        watcher = ViewerNavigationWatchdog(now=0, max_retries=2)
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
        watcher = ViewerNavigationWatchdog(now=10, max_retries=2)
        self.assertIsNone(watcher.observe("loading", 44))
        self.assertEqual(watcher.observe("loading", 45), "retry")
        self.assertIsNone(watcher.observe("loading", 45.1))
        self.assertIsNone(watcher.observe("loading", 79))
        self.assertEqual(watcher.observe("loading", 80), "retry")
        self.assertIsNone(watcher.observe("loading", 114))
        self.assertEqual(watcher.observe("loading", 115), "failed")

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
        self.assertEqual(watcher.observe("loading", 35), "retry")
        self.assertIsNone(watcher.observe("ad", 36))
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
        self.assertIsNone(watcher.observe("loading", 635))
        self.assertEqual(watcher.observe("loading", 636), "retry")

    def test_manual_login_can_wait_then_resume_loading_without_refilling_retries(self):
        watcher = ViewerNavigationWatchdog(now=0, max_retries=2)
        self.assertEqual(watcher.observe("loading", 35), "retry")
        for now in (36, 100, 400, 900):
            self.assertIsNone(watcher.observe("interactive", now))
        self.assertEqual(watcher.attempts, 1)
        self.assertIsNone(watcher.observe("loading", 901))
        self.assertIsNone(watcher.observe("loading", 935))
        self.assertEqual(watcher.observe("loading", 936), "retry")
        self.assertEqual(watcher.attempts, 2)
        self.assertEqual(watcher.observe("loading", 971), "failed")

    def test_transient_faults_do_not_restart_the_original_loading_deadline(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertIsNone(watcher.observe("loading", 20))
        self.assertIsNone(watcher.observe("error", 21))
        self.assertIsNone(watcher.observe("loading", 24))
        self.assertIsNone(watcher.observe("loading", 34))
        self.assertEqual(watcher.observe("loading", 35), "retry")

    def test_default_allows_ten_retries_then_fails(self):
        watcher = ViewerNavigationWatchdog(now=0)
        self.assertEqual(DEFAULT_AD_RETRIES, 10)
        self.assertEqual(DEFAULT_AD_RETRY_COOLDOWN, 5.0)
        for attempt in range(1, 11):
            self.assertIsNone(watcher.observe("loading", 35 * attempt - 1))
            self.assertEqual(watcher.observe("loading", 35 * attempt), "retry")
            self.assertEqual(watcher.attempts, attempt)
        self.assertEqual(watcher.observe("loading", 385), "failed")
        self.assertEqual(watcher.attempts, 10)

    def test_custom_cooldown_applies_to_stalled_loading_and_explicit_errors(self):
        watcher = ViewerNavigationWatchdog(now=0, retry_delay=120, max_retries=1)
        self.assertIsNone(watcher.observe("loading", 30))
        self.assertIsNone(watcher.observe("loading", 149))
        self.assertEqual(watcher.observe("loading", 150), "retry")
        self.assertIsNone(watcher.observe("error", 151))
        self.assertIsNone(watcher.observe("error", 270))
        self.assertEqual(watcher.observe("error", 271), "failed")

    def test_zero_retries_reports_failure_without_reloading(self):
        watcher = ViewerNavigationWatchdog(now=0, max_retries=0)
        self.assertEqual(watcher.observe("loading", 35), "failed")
        self.assertEqual(watcher.attempts, 0)


class AdRetrySettingTests(unittest.TestCase):
    def test_normalizes_valid_gui_and_numeric_settings_without_truncation(self):
        cases = [
            ("10", "5.0", (10, 5.0)), (0, 0, (0, 0.0)),
            (3.0, "12.5", (3, 12.5)), (" 4.0 ", 1.25, (4, 1.25)),
            ("9007199254740993", 0, (9007199254740993, 0.0)),
        ]
        for retries, cooldown, expected in cases:
            with self.subTest(retries=retries, cooldown=cooldown):
                result = validate_ad_retry_settings(retries, cooldown)
                self.assertEqual(result, expected)
                self.assertIs(type(result[0]), int)
                self.assertIs(type(result[1]), float)

    def test_rejects_invalid_retries_instead_of_truncating(self):
        for value in (True, False, -1, "-1", 2.5, "2.5", "NaN", float("inf"), None, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ad_retry_settings(value, 5)

    def test_rejects_invalid_cooldowns(self):
        for value in (True, False, -0.1, "-1", float("nan"), "inf", None, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ad_retry_settings(10, value)


if __name__ == "__main__":
    unittest.main()
