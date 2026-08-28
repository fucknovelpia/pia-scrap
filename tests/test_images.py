import json
import os
import tempfile
import unittest

import requests
from bs4 import BeautifulSoup

from src.images import (
    ImageManager,
    choose_image_source,
    detect_image_format,
    normalize_image_url,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-png-payload"


class FakeResponse:
    def __init__(self, payload=PNG_BYTES, content_type="image/png", status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(payload)),
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)

    def iter_content(self, chunk_size=65536):
        yield self.payload

    def close(self):
        pass


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ImageSourceTests(unittest.TestCase):
    def test_normalizes_protocol_relative_and_relative_urls(self):
        self.assertEqual(
            normalize_image_url("//cdn.example/cover.jpg"),
            "https://cdn.example/cover.jpg",
        )
        self.assertEqual(
            normalize_image_url("../image.png", "https://example.test/books/12/"),
            "https://example.test/books/image.png",
        )

    def test_prefers_highest_quality_responsive_picture_source(self):
        soup = BeautifulSoup(
            """
            <picture>
              <source data-srcset="/small.webp 400w, /large.webp 1600w">
              <img src="/placeholder.gif" data-src="/medium.jpg" alt="Art">
            </picture>
            """,
            "html.parser",
        )
        self.assertEqual(
            choose_image_source(soup.find("img"), "https://example.test/chapter/"),
            "https://example.test/large.webp",
        )

    def test_detects_payload_type_instead_of_trusting_extension(self):
        self.assertEqual(
            detect_image_format(PNG_BYTES, "application/octet-stream", "https://x.test/no-extension"),
            ("image/png", ".png"),
        )
        with self.assertRaises(ValueError):
            detect_image_format(b"<html>access denied</html>", "image/jpeg", "https://x.test/image.jpg")


class ImageManagerTests(unittest.TestCase):
    def test_localizes_deduplicates_and_reuses_persistent_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = FakeSession()
            manager = ImageManager(session, temp_dir)
            localized, assets = manager.localize_html(
                """
                <div style="background-image: url('/same.png')">
                  <img data-src="/same.png" alt="Panel">
                  <img srcset="/small.png 300w, /same.png 1200w">
                </div>
                """,
                episode_no=42,
                context="episode:42",
                base_url="https://example.test/viewer/42",
            )

            self.assertEqual(len(session.calls), 1)
            self.assertEqual(len(assets), 1)
            self.assertIn(assets[0].relative_path, localized)
            self.assertNotIn("data-src", localized)
            self.assertNotIn("srcset", localized)
            self.assertTrue(os.path.isfile(assets[0].absolute_path))

            manager.save_manifest()
            with open(os.path.join(temp_dir, "images.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(len(manifest["assets"]), 1)
            self.assertEqual(manifest["assets"][0]["episode_numbers"], [42])
            self.assertIn("chapter-background", manifest["assets"][0]["roles"])
            self.assertIn("chapter-image", manifest["assets"][0]["roles"])

            second_session = FakeSession()
            second_manager = ImageManager(second_session, temp_dir)
            reused = second_manager.download("https://example.test/same.png")
            self.assertIsNotNone(reused)
            self.assertEqual(second_session.calls, [])

    def test_rejects_oversized_content_length_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            response = FakeResponse()
            response.headers["content-length"] = str(1024)
            manager = ImageManager(FakeSession(response), temp_dir, max_image_bytes=100)
            asset = manager.download("https://example.test/large.png")
            self.assertIsNone(asset)
            self.assertEqual(manager.summary()["image_count"], 0)
            self.assertEqual(manager.summary()["image_failures"], 1)


if __name__ == "__main__":
    unittest.main()
