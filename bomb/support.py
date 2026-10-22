"""support.py -- BOMBERMAN WEB > Support: player mail to the GMs, and their replies.

The game (MAIN 0x74a190 / 0x74a1e0) talks to pkg01 over HTTP on :80 (web.py), which serves these
two pages and News and FAQ (board_page):

  POST /bombWeb/send_mail.asp?id=<CommonInqID>&subject=<subject>
       body = the message itself, Shift-JIS, percent-encoded (not form fields)
  GET  /bombWeb/get_mail2.asp?id=<CommonInqID>&num=<n>
       the player's mail list; num counts down from 20 as the screen pages

Both answer with a hubbxml page (Shift-JIS; pagetheme layers 1-3 are required, or opening an
item fails with "Receive Error"). Each ticket is one subject: what the player wrote, then the
reply, or a note that none has come yet.

Tickets live in $BOMB_DATA_DIR/support.json. A new one is posted to a Discord channel when
BOMB_SUPPORT_WEBHOOK holds its webhook URL. GMs answer from the server (or an admin panel):

  python -m bomb.support list            open tickets (all with --all)
  python -m bomb.support show N
  python -m bomb.support reply N "text"  the player sees it under Support Replies
  python -m bomb.support close N
"""
import argparse
import contextlib
import json
import os
import sys
import threading
import time
import urllib.request
from urllib.parse import unquote_to_bytes
from xml.sax.saxutils import escape

DATA_DIR = os.environ.get("BOMB_DATA_DIR", os.path.join(os.getcwd(), "data"))
WEBHOOK = os.environ.get("BOMB_SUPPORT_WEBHOOK", "")
STORE = "support.json"
WIDTH = 30          # MESSAGE WINDOW line width, half-width characters
SUBJECT_MAX = 24
TITLE_MAX = 40      # News and FAQ titles (29 shows in full)
BODY_MAX = 2000
PER_PLAYER = 20     # tickets a player can see (the game asks for 20)

_lock = threading.Lock()

try:
    import fcntl
except ImportError:         # Windows: only for running the tests, one process at a time
    fcntl = None


# ---- storage ------------------------------------------------------------

def _path():
    return os.path.join(DATA_DIR, STORE)


@contextlib.contextmanager
def _locked():
    """Held around every read-change-write of support.json. Two processes write it: bomb-web
    (new tickets, a chat bot's replies) and bomb-game (the admin panel's replies, adminport.py),
    so the thread lock alone could lose one side's change; an flock on support.json.lock covers
    both."""
    with _lock:
        if fcntl is None:
            yield
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_path() + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"next": 1, "tickets": []}


def save(doc):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def player_name(inq):
    """The nickname, else the feega-ID, else the CommonInqID."""
    try:
        from . import game
        name = game.account_name(inq)
        if isinstance(name, bytes):
            name = name.decode("cp932", "replace")
        if name and name != "PLAYER":
            return name
    except Exception:
        pass
    from . import feegalink
    return feegalink.name(inq) or inq


def sjis_param(raw):
    """A percent-encoded Shift-JIS value from the game, as text."""
    if isinstance(raw, str):
        raw = raw.encode("latin-1", "replace")
    return unquote_to_bytes(raw.replace(b"+", b" ")).decode("cp932", "replace")


def new_ticket(inq, subject, body):
    subject = (subject.strip() or "(no subject)")[:SUBJECT_MAX]
    body = body.replace("\r\n", "\n").strip()[:BODY_MAX]
    with _locked():
        doc = load()
        t = {"id": doc["next"], "inq": inq, "name": player_name(inq),
             "subject": subject, "body": body,
             "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "replies": [], "closed": False}
        doc["next"] += 1
        doc["tickets"].append(t)
        save(doc)
    notify(t)
    return t


def reply(n, text, gm="Support"):
    with _locked():
        doc = load()
        for t in doc["tickets"]:
            if t["id"] == n:
                t["replies"].append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "gm": gm, "text": text.strip()})
                save(doc)
                return t
    raise SystemExit("no ticket %d" % n)


def close(n):
    with _locked():
        doc = load()
        for t in doc["tickets"]:
            if t["id"] == n:
                t["closed"] = True
                save(doc)
                return t
    raise SystemExit("no ticket %d" % n)


# ---- a chat bot's side (web.py /support/api/*) ---------------------------
#
# A chat bot (for example a Discord bot that posts each ticket and opens a thread for the GM who
# takes it) can list tickets, keep its own fields on them and send replies. It sends
# BOMB_SUPPORT_API_TOKEN in X-Support-Token; the API is off while that is unset. The tickets stay
# here, so the bot keeps no game data and a restart neither loses nor re-posts anything.

API_TOKEN = os.environ.get("BOMB_SUPPORT_API_TOKEN", "")
#: what the bot may record on a ticket
BOT_FIELDS = ("alert_channel", "alert_message", "thread", "last_msg", "knocked_by")


def api_tickets():
    return load()["tickets"]


def api_update(n, fields):
    with _locked():
        doc = load()
        for t in doc["tickets"]:
            if t["id"] == n:
                for k in BOT_FIELDS:
                    if k in fields:
                        t[k] = fields[k]
                save(doc)
                return t
    raise KeyError(n)


# ---- Discord ------------------------------------------------------------

def notify(t):
    if not WEBHOOK:
        return
    text = ("**Bomberman support #%d** from **%s** (`%s`)\n**%s**\n%s\n\n"
            "Reply: `docker exec bomb-web-1 python -m bomb.support reply %d \"...\"`"
            % (t["id"], t["name"], t["inq"], t["subject"], t["body"][:1500], t["id"]))
    payload = json.dumps({"username": "Bomberman support", "content": text,
                          "allowed_mentions": {"parse": []}}).encode("utf-8")

    def post():
        try:
            req = urllib.request.Request(WEBHOOK, data=payload, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "bomb-support"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            print("support: Discord post for #%d failed: %s" % (t["id"], exc), flush=True)

    threading.Thread(target=post, daemon=True).start()


# ---- the pages ----------------------------------------------------------

def _width(s):
    return sum(1 if ord(c) < 0x80 or 0xff61 <= ord(c) <= 0xff9f else 2 for c in s)


def wrap(text, width=WIDTH):
    """Lines no wider than `width` half-width columns (full-width counts 2)."""
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = word if not line else line + " " + word
            if _width(cand) <= width:
                line = cand
                continue
            if line:
                out.append(line)
            line = ""
            while _width(word) > width:          # a word wider than a line
                cut = 0
                while _width(word[:cut + 1]) <= width:
                    cut += 1
                out.append(word[:cut])
                word = word[cut:]
            line = word
        out.append(line)
    return out


def page(items, now=None):
    """hubbxml for [(subject name, [lines])]."""
    now = int(now or time.time())
    out = ['<?xml version="1.0" encoding="Shift-JIS"?>', "<hubbxml>",
           "<servertime>%d</servertime>" % now, "<datatime>%d</datatime>" % now]
    out += ['<pagetheme layer="%d" type="1" lines="8"></pagetheme>' % n for n in (1, 2, 3)]
    for i, (name, lines) in enumerate(items, 1):
        out.append('<subject id="%d" type="1" name="%s" status="0">'
                   % (i, escape(name, {'"': "&quot;"})))
        out += ["<message>%s</message>" % escape(l) for l in lines]
        out.append("</subject>")
    out.append("</hubbxml>")
    return ("\r\n".join(out) + "\r\n").encode("cp932", "replace")


def mail_page(inq):
    mine = [t for t in load()["tickets"] if t["inq"] == inq][-PER_PLAYER:]
    if not mine:
        return page([("No mail yet", wrap("Send a question with Contact Support."))])
    items = []
    for t in reversed(mine):
        lines = ["You wrote:"] + wrap(t["body"]) + [""]
        if t["replies"]:
            for r in t["replies"]:
                lines += ["%s:" % (r.get("gm") or "Support")] + wrap(r["text"]) + [""]
        else:
            lines += ["No reply yet."]
        name = ("Re: " if t["replies"] else "") + t["subject"]
        items.append((name, lines[:100]))
    return page(items)


# BOMBERMAN WEB's News and FAQ: $BOMB_DATA_DIR/news.json and faq.json, read on every request
# (edit them live). Each is a list of {"title": "...", "body": "..."}, newest first; the body is
# wrapped to the message window, and a blank line starts a new paragraph.
BOARD_DEFAULTS = {
    "news": [{"title": "Welcome", "body": "Bomberman Online is running on this server. Have fun."}],
    "faq": [{"title": "Cannot connect to the network",
             "body": "Check that the console's DNS server is this server's address."},
            {"title": "Support mail",
             "body": "Send a question with Contact Support. Replies appear under Support Replies."}],
}


def board_items(kind):
    try:
        with open(os.path.join(DATA_DIR, kind + ".json"), encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = BOARD_DEFAULTS[kind]
    if not isinstance(items, list):
        items = BOARD_DEFAULTS[kind]
    return [(str(i.get("title") or "")[:TITLE_MAX] or "-", wrap(str(i.get("body") or ""))[:100])
            for i in items if isinstance(i, dict)]


def board_page(kind):
    return page(board_items(kind) or [("Nothing here yet", [])])


def sent_page():
    return page([("Sent", wrap("Your message was sent. Replies appear under "
                               "Support Replies."))])


# ---- the command line ---------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    for name in ("show", "close"):
        sub.add_parser(name).add_argument("n", type=int)
    rp = sub.add_parser("reply")
    rp.add_argument("n", type=int)
    rp.add_argument("text")
    rp.add_argument("--gm", default="Support")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for t in load()["tickets"]:
            if t["closed"] and not a.all:
                continue
            print("#%d %s %s (%s) %s%s" % (t["id"], t["t"], t["name"], t["inq"], t["subject"],
                                           "  [replied]" if t["replies"] else ""))
    elif a.cmd == "show":
        t = [t for t in load()["tickets"] if t["id"] == a.n]
        if not t:
            raise SystemExit("no ticket %d" % a.n)
        print(json.dumps(t[0], ensure_ascii=False, indent=1))
    elif a.cmd == "reply":
        t = reply(a.n, a.text, a.gm)
        print("replied to #%d (%s); they see it under Support Replies" % (t["id"], t["name"]))
    elif a.cmd == "close":
        close(a.n)
        print("closed #%d" % a.n)


if __name__ == "__main__":
    sys.exit(main())
