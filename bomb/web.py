"""web.py -- the HTTP side of the Bomberman Online server (ps2bb-pkg01 / pkg02 on :80).

Answers what we know and logs everything, so the next unknown request shows up in the log with
its headers and body.

Known (DATA0/PATCH.BIN, overlay base 0xac0000):
  GET /bombPatch/service/request_patchlist.asp?version=<installed>&df=<n>
  The client wants HTTP 200 and reads four integer headers (atoi):
    X-bombServerStatus  nonzero -> error S1004 (server not in service)
    X-bombClient        latest version; installed >= this -> "up to date" (returns 2)
    X-bombPatchCRC      \\ only used when a patch is offered
    X-bombPatchSize     /
  Anything but 200 -> error S1003.

Run:
  python -m bomb.web --bind 0.0.0.0 --port 80
Logs: $BOMB_LOG_DIR (default ./data/logs): web.log (one line per request) and web.jsonl (full
request records, bodies as hex).
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LATEST_CLIENT = int(os.environ.get("BOMB_LATEST_CLIENT", "10200"))
SERVER_STATUS = int(os.environ.get("BOMB_SERVER_STATUS", "0"))
LOG_DIR = os.environ.get("BOMB_LOG_DIR", os.path.join(os.getcwd(), "data", "logs"))


def mask_personal(body):
    """Keep personal data out of the logs (db_write.asp carries the player's EMAIL and names)."""
    import re
    return re.sub(rb"((?:EMAIL|Name_Kanji|Name_Kana)=)[^&]*", rb"\1***", body)


def log_record(rec):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "web.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    line = f"{rec['t']} {rec['peer']} {rec['method']} {rec['host']}{rec['path']} -> {rec['status']}"
    with open(os.path.join(LOG_DIR, "web.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


# The chat bot's support API (support.py) has a listener of its own, never the game's port 80:
# that one is reachable from the internet through the shared web server.
API_PORT = int(os.environ.get("BOMB_SUPPORT_API_PORT", "0") or 0)


class Handler(BaseHTTPRequestHandler):
    #: True only on the support API's own listener (ApiHandler)
    api = False

    def version_string(self):
        return "Microsoft-IIS/5.0"  # what an .asp host of the era would have said

    def log_message(self, *args):  # we log our own way
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urlsplit(self.path)
        status, headers, out = self.route(url.path, parse_qs(url.query), body)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)
        log_record({
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "peer": self.client_address[0],
            "method": self.command,
            "host": self.headers.get("Host", ""),
            "path": self.path,
            "headers": {k: ("***" if k.lower() == "x-support-token" else v)
                        for k, v in self.headers.items()},
            "body_hex": mask_personal(body).hex(),
            "status": status,
            "reply_headers": dict(headers),
        })

    do_GET = do_POST = do_HEAD = _serve

    def route(self, path, query, body):
        if path.lower() == "/bombpersonal/db_write.asp":
            # HTTPS via stunnel [pkg03]. The registration screen (MAIN 0x349158) posts
            # id=&Name_Kanji=&Name_Kana=&EMAIL= here before sending 0x0412. The reply parser
            # (0x348990) wants "1" (or "1,..."); "0,-N" are failures (N=3 "e-mail cannot be used"),
            # and any other first character is error -3002, so "OK" stopped the registration.
            return 200, [("Content-Type", "text/plain")], b"1"
        if path.lower() == "/bombpatch/service/request_patchlist.asp":
            return 200, [
                ("Content-Type", "application/octet-stream"),
                ("X-bombServerStatus", str(SERVER_STATUS)),
                ("X-bombClient", str(LATEST_CLIENT)),
                ("X-bombPatchCRC", "0"),
                ("X-bombPatchSize", "0"),
            ], b""
        if path.lower() in ("/bombweb/get_news.asp", "/bombweb/get_faq.asp"):
            from . import support
            kind = "news" if "news" in path.lower() else "faq"
            return 200, [("Content-Type", "text/xml")], support.board_page(kind)
        if path.lower() in ("/bombweb/send_mail.asp", "/bombweb/get_mail2.asp"):
            return self.support(path.lower(), query, body)
        if path.startswith("/support/api/") and self.api:
            return self.support_api(path, body)
        return 404, [("Content-Type", "text/plain")], b"not found\n"

    def support_api(self, path, body):
        # A chat bot's API (support.py), behind BOMB_SUPPORT_API_TOKEN; off when the token is
        # unset.
        from . import support
        import hmac
        js = [("Content-Type", "application/json")]
        token = self.headers.get("X-Support-Token", "")
        if not support.API_TOKEN or not hmac.compare_digest(token, support.API_TOKEN):
            return 403, js, b'{"error": "forbidden"}'
        try:
            req = json.loads(body or b"{}")
            if path == "/support/api/tickets":
                out = support.api_tickets()
            elif path == "/support/api/update":
                out = support.api_update(int(req["n"]), req.get("fields") or {})
            elif path == "/support/api/reply":
                out = support.reply(int(req["n"]), str(req["text"]), str(req.get("gm") or "Support"))
            elif path == "/support/api/close":
                out = support.close(int(req["n"]))
            else:
                return 404, js, b'{"error": "no such call"}'
        except (KeyError, ValueError, SystemExit) as exc:
            return 400, js, json.dumps({"error": str(exc)}).encode()
        return 200, js, json.dumps(out, ensure_ascii=False).encode("utf-8")

    def support(self, path, query, body):
        # BOMBERMAN WEB > Support (support.py). The query is read
        # raw: subject= is percent-encoded Shift-JIS, which parse_qs would decode as UTF-8.
        from . import support
        raw = dict(p.partition("=")[::2] for p in urlsplit(self.path).query.split("&") if p)
        inq = raw.get("id", "")
        xml = [("Content-Type", "text/xml")]
        if not inq.isdigit():
            return 400, [("Content-Type", "text/plain")], b"no id\n"
        if path.endswith("send_mail.asp"):
            support.new_ticket(inq, support.sjis_param(raw.get("subject", "")),
                               support.sjis_param(body))
            return 200, xml, support.sent_page()
        return 200, xml, support.mail_page(inq)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    a = ap.parse_args(argv)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"bomb.web on {a.bind}:{a.port}, latest client {LATEST_CLIENT}, logs {LOG_DIR}", flush=True)
    if API_PORT:
        api = ThreadingHTTPServer((a.bind, API_PORT), ApiHandler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        print(f"bomb.web support API on {a.bind}:{API_PORT}", flush=True)
    srv.serve_forever()


class ApiHandler(Handler):
    """The support API's listener: only /support/api/*, nothing of the game's pages."""
    api = True

    def route(self, path, query, body):
        if path.startswith("/support/api/"):
            return self.support_api(path, body)
        return 404, [("Content-Type", "text/plain")], b"not found\n"


if __name__ == "__main__":
    sys.exit(main())
