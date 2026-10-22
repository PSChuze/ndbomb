"""The HTTP side: patch check, BOMBERMAN WEB news, FAQ and support mail (bomb/web.py)."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from bomb import game, support, web


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bomb-web-")
        self.old = support.DATA_DIR, web.LOG_DIR, game.DATA_DIR
        support.DATA_DIR, web.LOG_DIR, game.DATA_DIR = self.dir, os.path.join(self.dir, "logs"), self.dir
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        support.DATA_DIR, web.LOG_DIR, game.DATA_DIR = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def get(self, path, data=None):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data), timeout=5) as r:
            return r.status, r.headers, r.read()

    def test_patch_check(self):
        st, headers, _ = self.get("/bombPatch/service/request_patchlist.asp?version=10200&df=0")
        self.assertEqual((st, headers["X-bombClient"], headers["X-bombServerStatus"]), (200, "10200", "0"))

    def test_news_default_and_from_the_file(self):
        st, _, body = self.get("/bombWeb/get_news.asp")
        text = body.decode("cp932")
        self.assertIn("<hubbxml>", text)
        for layer in (1, 2, 3):
            self.assertIn('<pagetheme layer="%d" type="1" lines="8"></pagetheme>' % layer, text)
        self.assertIn('name="Welcome"', text)
        with open(os.path.join(self.dir, "news.json"), "w", encoding="utf-8") as f:
            json.dump([{"title": "Tournament", "body": "Saturday at 20:00 in lobby 1. Bring friends & rivals."}], f)
        text = self.get("/bombWeb/get_news.asp")[2].decode("cp932")
        self.assertIn('name="Tournament"', text)
        self.assertIn("&amp; rivals.", text)
        self.assertTrue(all(len(m) <= support.WIDTH for m in
                            [l.split(">", 1)[1].split("<", 1)[0] for l in text.splitlines() if "<message>" in l]))

    def test_faq_default(self):
        text = self.get("/bombWeb/get_faq.asp")[2].decode("cp932")
        self.assertIn('name="Cannot connect to the network"', text)

    def test_support_mail_round_trip(self):
        self.get("/bombWeb/send_mail.asp?id=1234567890&subject=Help", data=b"The%20room%20is%20full")
        text = self.get("/bombWeb/get_mail2.asp?id=1234567890&num=20")[2].decode("cp932")
        self.assertIn('name="Help"', text)
        self.assertIn("The room is full", text)

    def code(self, base, path, token=None):
        req = urllib.request.Request(base + path, data=b"{}",
                                     headers={"X-Support-Token": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_the_bot_api_is_never_on_the_game_port(self):
        old, support.API_TOKEN = support.API_TOKEN, "bot-token"
        api = ThreadingHTTPServer(("127.0.0.1", 0), web.ApiHandler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        api_base = "http://127.0.0.1:%d" % api.server_address[1]
        try:
            # port 80 (what the shared web server forwards to): not there, even with the token
            self.assertEqual(self.code(self.base, "/support/api/tickets", "bot-token")[0], 404)
            self.assertEqual(self.code(self.base, "/support/api/reply", "bot-token")[0], 404)
            # its own listener: the token is needed, and the game's pages are not there
            st, body = self.code(api_base, "/support/api/tickets", "bot-token")
            self.assertEqual((st, json.loads(body)), (200, []))
            self.assertEqual(self.code(api_base, "/support/api/tickets", "wrong")[0], 403)
            self.assertEqual(self.code(api_base, "/support/api/tickets")[0], 403)
            self.assertEqual(self.code(api_base, "/bombWeb/get_news.asp")[0], 404)
        finally:
            support.API_TOKEN = old
            api.shutdown()
            api.server_close()

    def test_the_bot_api_is_off_without_a_token(self):
        api = ThreadingHTTPServer(("127.0.0.1", 0), web.ApiHandler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % api.server_address[1]
            self.assertEqual(self.code(base, "/support/api/tickets", "anything")[0], 403)
        finally:
            api.shutdown()
            api.server_close()


if __name__ == "__main__":
    unittest.main()
