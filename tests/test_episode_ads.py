import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from src.advertisements import AdvertisementError, AdvertisementResult
from src.api import NovelpiaClient, _has_episode_ticket, _is_ad_required, cancel_event, request_with_retries


def response(status=500, code="0010", result=None):
    resp = requests.Response()
    resp.status_code = status
    resp.url = "https://api-global.novelpia.com/v1/novel/episode"
    resp._content = json.dumps({
        "statusCode": status,
        "code": code,
        "result": result if result is not None else {"name": "NOVEL_ERROR", "message": "This episode has a basic advertisement."},
    }).encode()
    return resp


class EpisodeAdvertisementTests(unittest.TestCase):
    def setUp(self):
        cancel_event.clear()

    def test_ad_gate_bypasses_server_retries_throttle_and_login_refresh(self):
        session = Mock()
        session.request.return_value = response()
        refresh, login, rate_limit = Mock(), Mock(), Mock()
        with patch("src.api.time.sleep") as sleep:
            result = request_with_retries(
                session, "GET", "https://api-global.novelpia.com/v1/novel/episode",
                max_retries=4, allow_refresh=True, refresh_fn=refresh,
                login_fn=login, on_rate_limit=rate_limit,
            )
        self.assertTrue(_is_ad_required(result))
        session.request.assert_called_once()
        refresh.assert_not_called()
        login.assert_not_called()
        rate_limit.assert_not_called()
        sleep.assert_not_called()

    def test_ticket_opens_ad_and_resumes_only_with_server_ticket(self):
        client = NovelpiaClient(throttle=0)
        unlocked = response(200, "", {"_t": "episode-ticket"})

        def watch(episode_no, *, probe, cancelled, is_unlocked):
            self.assertEqual(episode_no, 670403)
            self.assertIs(cancelled, cancel_event)
            self.assertFalse(is_unlocked(response()))
            self.assertTrue(is_unlocked(unlocked))
            return probe()

        with patch.object(client, "_episode_ticket_response", side_effect=[response(), unlocked]) as get, patch("src.api.watch_episode_ad", side_effect=watch) as browser:
            data = client.episode_ticket(670403)
        self.assertEqual(data["result"]["_t"], "episode-ticket")
        browser.assert_called_once()
        self.assertEqual(get.call_args.kwargs, {"max_retries": 1})

    def test_unlocked_ticket_does_not_open_browser(self):
        client = NovelpiaClient(throttle=0)
        unlocked = response(200, "", {"_t": "episode-ticket"})
        with patch.object(client, "_episode_ticket_response", return_value=unlocked), patch("src.api.watch_episode_ad") as browser:
            client.episode_ticket(670403)
        browser.assert_not_called()

    def test_ad_logs_explain_auto_continue_and_confirm_received_chapter(self):
        client = NovelpiaClient(throttle=0)
        with (
            patch.object(client, "_episode_ticket_response", return_value=response()),
            patch("src.api.watch_episode_ad", return_value=self._captured_episode(675194)),
            patch("builtins.print") as output,
        ):
            client.episode_ticket(675194)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("Continue will be auto-clicked when the ad finishes.", messages[0])
        self.assertNotIn("If prompted", messages[0])
        self.assertEqual(messages[1], "[ad] Episode 675194: Advertisement complete. Chapter received; resuming download.")
        self.assertTrue(all(call.kwargs.get("flush") for call in output.call_args_list))

    @staticmethod
    def _captured_episode(episode_no):
        """The viewer already used this ticket while loading the real chapter."""
        ticket = {
            "statusCode": 200,
            "result": {
                "data": {"episode_no": episode_no},
                "_t": f"spent-ticket-{episode_no}",
                "signed_key": {
                    name: f"{name}-{episode_no}"
                    for name in NovelpiaClient.IMAGE_COOKIE_NAMES
                },
            },
        }
        content = {
            "statusCode": 200,
            "result": {
                "data": {
                    "epi_content": f"<p>Beginning {episode_no}</p>",
                    "epi_content2": f"<p>Middle {episode_no}</p>",
                    "epi_content3": f'<p><img src="https://pv-gn.novelpia.com/{episode_no}.png"></p>',
                    "epi_content4": f"<p>Ending {episode_no}</p>",
                },
            },
        }
        return AdvertisementResult(ticket=ticket, content=content)

    def test_viewer_consumed_unlock_downloads_captured_content_without_second_request(self):
        episode_no = 675194
        client = NovelpiaClient(throttle=0)
        captured = self._captured_episode(episode_no)
        with (
            # Once the browser consumes the reward, every separate ticket
            # request again returns 0010. The loaded browser response must win.
            patch.object(client, "_episode_ticket_response", return_value=response()) as ticket,
            patch("src.api.watch_episode_ad", return_value=captured) as browser,
            patch.object(client, "episode_content", side_effect=AssertionError("Ticket was already used")) as content,
            patch.object(client.s, "get", side_effect=AssertionError("Viewer content was already fetched")) as direct,
        ):
            result = client.fetch_episode({"episode_no": episode_no, "epi_title": "Ad chapter"}, 3)

        self.assertNotIn("error", result)
        ticket.assert_called_once_with(episode_no)
        browser.assert_called_once()
        content.assert_not_called()
        direct.assert_not_called()
        self.assertEqual(result["epi_no"], episode_no)
        self.assertEqual(result["idx"], 3)
        self.assertIn(f"Beginning {episode_no}", result["html"])
        self.assertIn(f"Middle {episode_no}", result["html"])
        self.assertIn(f"/{episode_no}.png", result["html"])
        self.assertIn(f"Ending {episode_no}", result["html"])
        self.assertEqual(result["_image_cookies"], captured.ticket["result"]["signed_key"])
        self.assertNotIn("_viewer_content", result)
        self.assertNotIn("spent-ticket", result["html"])

    def test_viewer_handoff_does_not_modify_the_original_ticket_response(self):
        client = NovelpiaClient(throttle=0)
        captured = self._captured_episode(675194)
        original_ticket = json.loads(json.dumps(captured.ticket))
        with (
            patch.object(client, "_episode_ticket_response", return_value=response()),
            patch("src.api.watch_episode_ad", return_value=captured),
        ):
            ticket = client.episode_ticket(675194)
        self.assertIsNot(ticket, captured.ticket)
        self.assertEqual(ticket["_viewer_content"], captured.content)
        self.assertEqual(captured.ticket, original_ticket)
        self.assertEqual(client.signed_image_cookies(ticket), captured.ticket["result"]["signed_key"])

    def test_four_viewer_handoffs_keep_chapter_content_and_signed_cookies_separate(self):
        workers = 4
        client = NovelpiaClient(throttle=0, threads=workers)
        episodes = [
            {"episode_no": number, "epi_title": f"Chapter {number}"}
            for number in (675194, 676405, 677072, 678021)
        ]
        barrier = threading.Barrier(workers)

        def watch(episode_no, **kwargs):
            try:
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError as exc:
                raise AdvertisementError("Download workers serialized their ads") from exc
            return self._captured_episode(episode_no)

        with (
            patch.object(client, "_episode_ticket_response", return_value=response()) as ticket,
            patch("src.api.watch_episode_ad", side_effect=watch) as browser,
            patch.object(client, "episode_content", side_effect=AssertionError("Viewer already fetched content")) as content,
            patch.object(client, "_recover_episode") as recover,
        ):
            results = client.fetch_episodes_parallel(episodes, max_workers=workers)

        self.assertEqual(ticket.call_count, workers)
        self.assertEqual(browser.call_count, workers)
        content.assert_not_called()
        recover.assert_not_called()
        for index, (episode, result) in enumerate(zip(episodes, results), 1):
            episode_no = episode["episode_no"]
            self.assertNotIn("error", result)
            self.assertEqual(result["epi_no"], episode_no)
            self.assertEqual(result["idx"], index)
            self.assertEqual(result["epi_title"], episode["epi_title"])
            self.assertIn(f"Ending {episode_no}", result["html"])
            self.assertEqual(
                result["_image_cookies"],
                self._captured_episode(episode_no).ticket["result"]["signed_key"],
            )
            for other in episodes:
                if other["episode_no"] != episode_no:
                    self.assertNotIn(str(other["episode_no"]), result["html"])

    def test_http_200_without_episode_token_does_not_confirm_ad_completion(self):
        self.assertFalse(_has_episode_ticket(response(200, "", {"login": {"mem_no": 1}})))
        self.assertFalse(_has_episode_ticket(response(200)))
        self.assertFalse(_has_episode_ticket(response(500, "", {"_t": "invalid"})))
        self.assertTrue(_has_episode_ticket(response(200, "", {"_t": "real-ticket"})))

    def test_closed_ad_is_not_retried_by_session_recovery(self):
        episode = {"episode_no": 670403, "epi_title": "Ad chapter"}
        for threads in (1, 2):
            with self.subTest(threads=threads):
                client = NovelpiaClient(throttle=0, threads=threads)
                with patch.object(client, "episode_ticket", side_effect=AdvertisementError("Advertisement window closed")), patch.object(client, "_recover_episode") as recover:
                    results = client.fetch_episodes_parallel([episode], max_workers=threads)
                recover.assert_not_called()
                self.assertFalse(results[0]["retryable"])
                self.assertIn("Advertisement window closed", results[0]["error"])

    def test_network_error_retains_normal_recovery(self):
        client = NovelpiaClient(throttle=0)
        episode = {"episode_no": 670403}
        with patch.object(client, "episode_ticket", side_effect=requests.ConnectionError("Offline")), patch.object(client, "_recover_episode", return_value={"html": "recovered"}) as recover:
            result = client.fetch_episodes_parallel([episode])
        recover.assert_called_once()
        self.assertEqual(result[0]["html"], "recovered")

    def _assert_worker_ad_concurrency(self, workers):
        client = NovelpiaClient(throttle=0, threads=workers)
        episodes = [
            {"episode_no": number, "epi_title": f"Chapter {number}"}
            for number in range(670401, 670409)
        ]
        barrier = threading.Barrier(workers)
        state_lock = threading.Lock()
        active = peak_active = 0
        requests_seen = []

        def ticket_response(episode_no, *, max_retries=4):
            with state_lock:
                requests_seen.append((episode_no, max_retries))
            if max_retries == 1:
                return response(200, "", {"_t": f"ticket-{episode_no}"})
            return response()

        def watch(episode_no, *, probe, cancelled, is_unlocked):
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                # Every worker must reach its own ad before any can complete.
                # A timeout makes accidental serialization fail without hanging.
                try:
                    barrier.wait(timeout=3)
                except threading.BrokenBarrierError as exc:
                    raise AdvertisementError("Download workers serialized their ads") from exc
                result = probe()
                self.assertIs(cancelled, cancel_event)
                self.assertTrue(is_unlocked(result))
                self.assertEqual(result.json()["result"]["_t"], f"ticket-{episode_no}")
                return result
            finally:
                with state_lock:
                    active -= 1

        def content(token):
            return {"result": {"data": {"epi_content": f"<p>{token}</p>"}}}

        with (
            patch.object(client, "_episode_ticket_response", side_effect=ticket_response),
            patch("src.api.watch_episode_ad", side_effect=watch) as browser,
            patch.object(client, "episode_content", side_effect=content),
            patch.object(client, "_recover_episode") as recover,
        ):
            results = client.fetch_episodes_parallel(episodes, max_workers=workers)

        self.assertEqual(peak_active, workers)
        self.assertEqual(active, 0)
        self.assertEqual(browser.call_count, len(episodes))
        recover.assert_not_called()
        self.assertCountEqual(
            requests_seen,
            [(ep["episode_no"], retries) for ep in episodes for retries in (4, 1)],
        )
        self.assertEqual([result["epi_no"] for result in results], [ep["episode_no"] for ep in episodes])
        for index, (episode, result) in enumerate(zip(episodes, results), 1):
            self.assertNotIn("error", result)
            self.assertEqual(result["idx"], index)
            self.assertEqual(result["epi_title"], episode["epi_title"])
            self.assertIn(f"ticket-{episode['episode_no']}", result["html"])

    def test_four_workers_watch_ads_independently_and_keep_episode_tickets_separate(self):
        self._assert_worker_ad_concurrency(4)

    def test_one_worker_never_watches_multiple_ads_concurrently(self):
        self._assert_worker_ad_concurrency(1)


if __name__ == "__main__":
    unittest.main()
