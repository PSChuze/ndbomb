"""Names from the Feega account API (bomb/feegalink.py)."""
import unittest
from unittest import mock

from bomb import feegalink, game
from bomb.fakefeega import FakeFeega, common_inq_id


class FeegaLinkTest(unittest.TestCase):
    def setUp(self):
        self.feega = FakeFeega()

    def tearDown(self):
        self.feega.srv.shutdown()
        feegalink.forget()

    def test_names_come_from_the_service(self):
        key = self.feega.remember("Ace")
        self.assertEqual(key, common_inq_id("Ace"))
        self.assertEqual(feegalink.name(key), "Ace")
        self.assertEqual(game.auth_name(key), "Ace")
        self.assertEqual(feegalink.name("0000000000"), "")

    def test_a_rename_shows_after_the_cache_time(self):
        key = self.feega.remember("Ace")
        feegalink.TTL = 60
        feegalink.forget()
        self.assertEqual(feegalink.name(key), "Ace")
        self.feega.accounts[key] = "Ace2"
        self.assertEqual(feegalink.name(key), "Ace")          # still cached
        feegalink.forget()
        self.assertEqual(feegalink.name(key), "Ace2")

    def test_an_unreachable_service_keeps_the_last_names_and_waits(self):
        key = self.feega.remember("Ace")
        feegalink.TTL = 0
        self.assertEqual(feegalink.name(key), "Ace")
        feegalink.URL = "http://127.0.0.1:9"                  # nothing listens there
        self.assertEqual(feegalink.name(key), "Ace")
        feegalink.TTL = 60
        with mock.patch.object(feegalink, "fetch", side_effect=AssertionError("asked again")):
            feegalink.name(key)                                # within the wait: no new ask

    def test_the_token_is_sent(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen.update(req.headers)
            raise OSError("no")

        with mock.patch.object(feegalink, "TOKEN", "abc"), \
                mock.patch.object(feegalink.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(feegalink.fetch())
        self.assertEqual(seen.get("X-feega-token"), "abc")


if __name__ == "__main__":
    unittest.main()
