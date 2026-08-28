import json
import os
import tempfile
import unittest
import zipfile

from src.epub import EpubBuilder
from test_images import FakeSession


class FakeClient:
    def __init__(self):
        self.s = FakeSession()
        self.timeout = 5
        self.default_max_workers = 1

    def fetch_episodes_parallel(self, episodes, max_workers=1, progress_cb=None):
        results = []
        for index, episode in enumerate(episodes, 1):
            result = {
                "html": '<p>Before</p><img data-src="/panel.png" alt="Panel"><p>After</p>',
                "epi_title": episode["epi_title"],
                "epi_no": episode["episode_no"],
            }
            results.append(result)
            if progress_cb:
                progress_cb(index, True, result)
        return results


class EpubImageIntegrationTests(unittest.TestCase):
    def test_epub_embeds_cover_and_chapter_image_and_writes_manifest(self):
        novel = {
            "result": {
                "novel": {
                    "novel_no": 7,
                    "novel_name": "Image Test",
                    "novel_full_img": "/cover.png",
                    "novel_story": "Test",
                    "flag_complete": 0,
                },
                "writer_list": [{"writer_name": "Tester"}],
                "info": {"epi_cnt": 1},
            }
        }
        episodes = [{"episode_no": 70, "epi_num": 1, "epi_title": "One"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            book_dir = os.path.join(temp_dir, "image-test")
            path, _, count = EpubBuilder(temp_dir).build(
                FakeClient(),
                novel,
                episodes,
                novel_id=7,
                book_dir=book_dir,
            )
            self.assertEqual(count, 1)
            self.assertTrue(os.path.isfile(path))

            with open(os.path.join(book_dir, "images.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(len(manifest["assets"]), 1)  # identical test bytes are deduplicated
            self.assertIn("cover", manifest["assets"][0]["roles"])
            self.assertIn("chapter-image", manifest["assets"][0]["roles"])

            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.startswith("EPUB/images/") for name in names))
                chapter_name = next(name for name in names if name.endswith("chap_0001.xhtml"))
                chapter = archive.read(chapter_name).decode("utf-8")
                self.assertIn("images/", chapter)
                self.assertNotIn("data-src", chapter)


if __name__ == "__main__":
    unittest.main()
