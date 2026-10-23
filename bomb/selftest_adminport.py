"""selftest_adminport.py -- offline check of adminport.py, this server's side of an OpenLobby admin
panel (the protocol in olport.py).

Starts the game server on 127.0.0.1 (temp data and log dirs), puts a scripted player in the lobby,
and drives the admin port the way the panel does: the description, who is online, and the admin's
commands, each of which must reach the player as the same message `python -m bomb.admin` sends;
then the help desk over support.py (a reply must show on the player's Support Replies page), and
the Rankings, Recent matches and Lobby tips pages. No console, no panel.

  python -m bomb.selftest_adminport
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TMP = tempfile.mkdtemp(prefix="bomb-adminport-")
os.environ["BOMB_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["BOMB_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["BOMB_ADMIN_TOKEN"] = TOKEN = "selftest-token-0123"
os.environ["BOMB_UPDATE_REPO"] = "off"      # offline: the update notice says why it is unknown

from . import adminport, game, live, support  # noqa: E402  (read the env at import)
from .fakefeega import FakeFeega  # noqa: E402
from .fakeplayer import Player  # noqa: E402

FAILS = []


def check(what, cond):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def serve():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def loop():
        while True:
            c, addr = s.accept()
            threading.Thread(target=game.Session(c, f"{addr[0]}:{addr[1]}").run, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1]


def main():
    feega = FakeFeega()
    gport = serve()
    p = Player("127.0.0.1", gport, "ALICE", quiet=True)
    p.enter_lobby()
    port = adminport.build(game)
    host, n = port.start()
    url = f"http://127.0.0.1:{n}"

    def call(path, body=None, token=TOKEN):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Devtool-Token": token, "X-Devtool-Actor": "admin"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def tool(path, form=None):
        """A tool page as the panel forwards it: GET, or a form post. -> (status, html)."""
        data = None if form is None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url + path, data=data, method="GET" if form is None else "POST",
                                     headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "admin",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, ""

    check("nothing answers without the token", call("/openlobby/module.json", token="nope")[0] == 403)
    st, desc = call("/openlobby/module.json")
    check("it describes itself as bomb, with the admin's commands",
          st == 200 and desc["key"] == "bomb" and desc.get("title") == "Bomberman Online"
          and [a["id"] for a in desc["actions"]]
          == ["announce", "chat", "warn", "tip"])
    check("Announce is the panel's announce", [a["kind"] for a in desc["actions"]][0] == "announce")
    check("it has a help desk, three tool pages and a player lookup",
          desc.get("help") is True and desc.get("player") is True
          and [(t["id"], t["path"]) for t in desc["tools"]]
          == [("rankings", "rankings"), ("matches", "matches"), ("tips", "tips")])
    st, now = call("/openlobby/status.json")
    # with nickname registration off the server names a console PLAYER; take the name it uses
    name = game.admin_request({"op": "who"})["players"][0]["name"]
    check("the player in the lobby is online", now.get("online") == 1
          and now["players"] == [{"name": name, "where": "lobby"}])
    check("status.json has the update notice", (now.get("update") or {}).get("update") == "unknown"
          and "off" in now["update"]["why"] and "running" in now["update"])
    check("its logins are feega logins, and it has a player picker",
          desc.get("accounts") == "feega" and desc.get("players") is True)
    st, found = call("/openlobby/players.json?q=" + urllib.parse.quote(name[:3].lower()) + "&limit=5")
    check("the picker finds the player online by part of the name", st == 200
          and found["players"][:1] == [{"id": name, "label": name, "detail": "", "online": True}])
    check("the picker finds nobody for a name no one has",
          call("/openlobby/players.json?q=zzzz&limit=5") == (200, {"players": []}))

    m = p.mark()
    st, out = call("/openlobby/actions/announce", {"text": "Maintenance at 10:00 UTC"})
    p.wait(0x07e6, since=m)
    time.sleep(0.3)
    got = p.got(0x07e6, m)
    check("Announce reaches the player as a banner and a chat line",
          out.get("ok") is True and sorted(int.from_bytes(f[:2], "little") for f in got) == [1, 2])
    m = p.mark()
    st, out = call("/openlobby/actions/warn", {"name": name.lower()})
    p.wait(0x07ef, since=m)
    check("Warn reaches that player (the name in any case)", out.get("ok") is True)
    check("Warn names nobody who is not online",
          call("/openlobby/actions/warn", {"name": "Nobody"})[1] == {"ok": False, "msg": "no such player online"})
    m = p.mark()
    st, out = call("/openlobby/actions/tip", {"index": "3", "text": "Hold O to throw a bomb"})
    p.wait(0x07f2, since=m)
    check("a lobby tip is sent and kept", out.get("ok") is True and game.load_tips().get(3) == "Hold O to throw a bomb")
    st, out = call("/openlobby/actions/tip", {"index": "3", "text": ""})
    check("an empty tip clears it", out.get("ok") is True and 3 not in game.load_tips())
    check("a tip number out of range is refused",
          call("/openlobby/actions/tip", {"index": "13", "text": "x"})[1].get("ok") is False)
    check("an empty announcement is refused",
          call("/openlobby/actions/announce", {"text": "  "})[1] == {"ok": False, "msg": "Nothing to announce."})

    # ---- Game help: support.py's tickets -------------------------------------------------
    check("no tickets, an empty help list", call("/openlobby/help.json") == (200, {"threads": []}))
    t1 = support.new_ticket(p.inq, "Cannot join", "The room says full.")
    t2 = support.new_ticket("0000000001", "Old one", "Never mind.")
    support.close(t2["id"])
    st, hl = call("/openlobby/help.json?open=1")
    check("the open list holds the open ticket, waiting for us",
          st == 200 and [(r["id"], r["subject"], r["waiting"], r["count"], r["closed"]) for r in hl["threads"]]
          == [(str(t1["id"]), "Cannot join", True, 1, False)] and isinstance(hl["threads"][0]["at"], int))
    st, hl = call("/openlobby/help.json")
    check("the whole list has the closed one too, newest first",
          [r["id"] for r in hl["threads"]] == [str(t2["id"]), str(t1["id"])])
    st, th = call("/openlobby/help/thread.json?id=%d" % t1["id"])
    check("a thread is the player's message, and the player is online",
          st == 200 and th["who"] == {"name": t1["name"], "online": True}
          and [(m["from"], m["text"]) for m in th["messages"]] == [("player", "The room says full.")])
    st, th = call("/openlobby/help/thread.json?id=%d" % t2["id"])
    check("the other ticket's player is not online", th["who"]["online"] is False and th["closed"] is True)
    check("an unknown thread is a 404", call("/openlobby/help/thread.json?id=99")[0] == 404)
    check("a reply to an unknown ticket is refused, not a crash",
          call("/openlobby/help/reply", {"id": "99", "text": "hi"})[1].get("ok") is False)
    check("closing an unknown ticket is refused",
          call("/openlobby/help/close", {"id": "nope"})[1].get("ok") is False)
    check("an empty reply is refused",
          call("/openlobby/help/reply", {"id": str(t1["id"]), "text": "  "})[1].get("ok") is False)
    st, out = call("/openlobby/help/reply", {"id": str(t1["id"]), "text": "Try room 2, it has space."})
    check("a reply is taken", st == 200 and out.get("ok") is True)
    mail = support.mail_page(p.inq).decode("cp932")
    check("the player sees it under Support Replies, signed Support",
          "Re: Cannot join" in mail and "Support:" in mail and "Try room 2, it has space." in mail)
    st, th = call("/openlobby/help/thread.json?id=%d" % t1["id"])
    check("the thread now ends with our answer, and no longer waits",
          [m["from"] for m in th["messages"]] == ["player", "staff"]
          and call("/openlobby/help.json?open=1")[1]["threads"][0]["waiting"] is False)
    st, out = call("/openlobby/help/reply", {"id": str(t1["id"]), "text": "Closing this.", "close": True})
    check("reply and close", out.get("ok") is True and support.load()["tickets"][0]["closed"] is True
          and call("/openlobby/help.json?open=1")[1]["threads"] == [])
    t3 = support.new_ticket(p.inq, "Third", "x")
    st, out = call("/openlobby/help/close", {"id": str(t3["id"])})
    check("close alone", out.get("ok") is True and support.load()["tickets"][-1]["closed"] is True)

    # ---- Rankings ------------------------------------------------------------------------
    st, html = tool("/rankings")
    check("Rankings with nobody ranked says so", st == 200 and "Nobody is ranked" in html)
    for inq, nick, pts, wins, rounds in (("1111111111", "AceBomber", 120, 12, 20),
                                         ("2222222222", "Kaboom", 30, 3, 5)):
        game.player_info(inq, lambda r, _a, n=nick, p_=pts, w=wins, ro=rounds:
                         r.update(nick=n, points=p_, wins=w, rounds=ro))
    st, html = tool("/rankings")
    check("Rankings lists the players by points, best first",
          st == 200 and html.find("AceBomber") < html.find("Kaboom") and "Kaboom" in html
          and "<script" not in html)
    st, html = tool("/rankings?by=rate")
    check("Win rate holds only players with enough rounds", "60.0%" in html and "Kaboom" not in html)

    # ---- Player lookup: the account's linked feega logins (links["feega"], key = CommonInqID) --
    def lookup(links=None, handles=(), polid="TEST0001"):
        q = {"polid": polid, "handles": ",".join(handles), "members": "alice"}
        if links is not None:
            q["links"] = links if isinstance(links, str) else json.dumps(links)
        return call("/openlobby/player.json?" + urllib.parse.urlencode(q))

    feega.accounts.update({p.inq: "ALICE", "1111111111": "ace_feega", "3333333333": "otheronly"})
    st, who = lookup({"feega": [{"key": "1111111111", "name": "ace_feega"},
                                {"key": p.inq, "name": "ALICE"}]})
    chars = who.get("characters") or []
    check("a linked login is its Bomberman, with its ranking numbers and look",
          st == 200 and len(chars) == 2 and chars[0] == {
              "name": "AceBomber", "detail": "", "online": False,
              "facts": [{"label": "feega-ID", "value": "ace_feega"},
                        {"label": "Points", "value": 120},
                        {"label": "Rounds won", "value": "12 of 20"},
                        {"label": "Bomberman", "value": "white"}]})
    check("a linked login that is on says where",
          len(chars) == 2 and chars[1]["online"] is True and chars[1]["detail"] == "in the lobby"
          and chars[1]["name"] == "ALICE")
    print("     example: " + json.dumps(chars[:1]), flush=True)
    blob = json.dumps(who)
    check("the answer holds no key, player id, password hash or address",
          "1111111111" not in blob and p.inq not in blob and "pid" not in blob
          and "pbkdf2" not in blob and "192.0.2" not in blob and "TEST0001" not in blob)
    check("an unknown key is nobody", lookup({"feega": [{"key": "9999999999", "name": "x"}]}) == (200, {}))
    check("looking up an unknown key does not make it a player", "9999999999" not in game._load_players())
    check("a feega login that never played Bomberman (another game only) is nobody",
          lookup({"feega": [{"key": "3333333333", "name": "otheronly"}]}) == (200, {}))
    check("no links is nobody, even with a handle named like a player",
          lookup(handles=["AceBomber", "ace_feega"]) == (200, {}))
    check("another game's links are not feega logins",
          lookup({"popn": [{"key": "1111111111", "name": "ace"}]}) == (200, {}))
    check("links that are not JSON are no links", lookup("{nope") == (200, {}))

    # ---- Recent matches -------------------------------------------------------------------
    check("Recent matches with none says so", "No match has ended" in tool("/matches")[1])

    class Room:
        hdr = bytes(5) + b"Friday<fun>".ljust(0x19, b"\0") + bytes(0x20)
        wins = {1: 2, 2: 0}

        def mode(self):
            return 2

    class Member:
        def __init__(self, pid, name):
            self.pid, self.name, self.inq_id = pid, name, None
    with game._slock:
        live.record_match(Room(), [Member(1, b"AceBomber"), Member(2, b"Kaboom")], 2)
    st, html = tool("/matches")
    check("Recent matches shows the match, its winner, and escapes the room name",
          st == 200 and "Friday&lt;fun&gt;" in html and "Free-for-all" in html
          and "AceBomber 2, Kaboom 0" in html)

    # ---- Lobby tips ----------------------------------------------------------------------
    STOCK = '<span class="muted">the stock tip</span>'
    st, html = tool("/tips")
    check("Lobby tips lists all 13, stock", st == 200 and html.count(STOCK) == 13)
    m = p.mark()
    st, html = tool("/tips", {"index": "5", "text": "Kick bombs <with> B"})
    p.wait(0x07f2, since=m)
    check("a tip set from the page is kept, sent, and listed",
          game.load_tips().get(5) == "Kick bombs <with> B" and "Tip 5 set and sent to 1 player" in html
          and "Kick bombs &lt;with&gt; B" in html and html.count(STOCK) == 12)
    st, html = tool("/tips", {"index": "5", "clear": "1"})
    check("Clear on the page clears it", 5 not in game.load_tips() and html.count(STOCK) == 13
          and "Tip 5 cleared" in html)
    st, html = tool("/tips", {"index": "40", "text": "x"})
    check("a tip number out of range is refused on the page", "The tip number is 0 to 12." in html)

    port.stop()
    p.sock.close()
    print("selftest_adminport: " + ("PASS" if not FAILS else f"{len(FAILS)} FAILED"), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
