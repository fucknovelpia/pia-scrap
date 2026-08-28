import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.builder import build_txt
from test_epub_images import FakeClient


class TxtImageIntegrationTests(unittest.TestCase):
    def test_txt_saves_assets_and_inserts_local_image_marker(self):
        novel = {
            "result": {
                "novel": {
                    "novel_no": 9,
                    "novel_name": "TXT Image Test",
                    "novel_full_img": "/cover.png",
                    "novel_story": "Test",
                    "flag_complete": 0,
                },
                "writer_list": [{"writer_name": "Tester"}],
                "info": {"epi_cnt": 1},
            }
        }
        episodes = [{"episode_no": 90, "epi_num": 1, "epi_title": "One"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "src.builder.fetch_novel_and_episodes",
                return_value=(novel, episodes, "TXT Image Test"),
            ):
                book_dir, _, count = build_txt(FakeClient(), 9, temp_dir)

            self.assertEqual(count, 1)
            txt_path = os.path.join(book_dir, "1_One.txt")
            with open(txt_path, encoding="utf-8") as handle:
                chapter_text = handle.read()
            self.assertIn("[Image: Panel (images/", chapter_text)

            with open(os.path.join(book_dir, "metadata.json"), encoding="utf-8") as handle:
                metadata = json.load(handle)
            self.assertEqual(metadata["images"]["image_count"], 1)
            self.assertTrue(os.path.isfile(os.path.join(book_dir, "images.json")))


if __name__ == "__main__":
    unittest.main()
