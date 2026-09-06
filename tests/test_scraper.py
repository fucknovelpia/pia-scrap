import unittest

from src.scraper import extract_novel_entries, extract_novel_links


class ScraperImageTests(unittest.TestCase):
    def test_extracts_nested_and_sibling_listing_images(self):
        markup = """
        <article>
          <a href="/novel/101"><img data-src="//cdn.test/101.jpg" alt="First"></a>
          <a href="/novel/101">First novel</a>
        </article>
        <article>
          <img srcset="/202-small.png 300w, /202-large.png 900w" alt="Second">
          <a href="/novel/202?ref=list">Second novel</a>
        </article>
        """
        entries = extract_novel_entries(markup, "https://global.novelpia.com")
        self.assertEqual(
            [entry["url"] for entry in entries],
            [
                "https://global.novelpia.com/novel/101",
                "https://global.novelpia.com/novel/202",
            ],
        )
        self.assertEqual(entries[0]["image_url"], "https://cdn.test/101.jpg")
        self.assertEqual(
            entries[1]["image_url"],
            "https://global.novelpia.com/202-large.png",
        )
        self.assertEqual(extract_novel_links(markup), [entry["url"] for entry in entries])


if __name__ == "__main__":
    unittest.main()
