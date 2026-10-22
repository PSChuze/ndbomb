"""fakefeega.py -- a stand-in for the Feega service's account API, for the self-tests.

Serves GET /v1/accounts (and /v1/accounts/<key>) on 127.0.0.1 from a dict, the way the real
service answers feegalink.py, and points feegalink at itself with no caching.
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import feegalink


def common_inq_id(name):
    """The key a first sign-in with this feega-ID gets (the Feega service's rule)."""
    return "%010d" % (int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16) % 10 ** 10)


class FakeFeega:
    def __init__(self):
        self.accounts = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/v1/accounts":
                    code, doc = 200, {"accounts": dict(owner.accounts)}
                elif path.startswith("/v1/accounts/") and path[13:] in owner.accounts:
                    code, doc = 200, {"key": path[13:], "account": owner.accounts[path[13:]]}
                else:
                    code, doc = 404, {"error": "no_account"}
                out = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        feegalink.URL, feegalink.TTL = self.url, 0
        feegalink.forget()

    def remember(self, name, key=None):
        """An account as a Feega sign-in makes it; returns its key."""
        key = key or common_inq_id(name)
        self.accounts[key] = name
        return key
