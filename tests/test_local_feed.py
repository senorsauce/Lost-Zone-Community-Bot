import unittest

from local_feed import google_news_url, parse_rss


class LocalFeedTests(unittest.TestCase):
    def test_parses_rss_items(self):
        payload = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <item><title>Town festival announced</title><link>https://example.test/festival</link>
        <description>Saturday downtown</description><pubDate>Tue, 08 Sep 2026 10:00:00 GMT</pubDate>
        <source>Example News</source></item></channel></rss>"""

        items = parse_rss(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Town festival announced")
        self.assertEqual(items[0].source, "Example News")

    def test_builds_location_encoded_search_url(self):
        url = google_news_url('events near "Madison, WI" within 25 miles')

        self.assertIn("news.google.com/rss/search", url)
        self.assertIn("Madison%2C+WI", url)
        self.assertIn("25+miles", url)


if __name__ == "__main__":
    unittest.main()