"""selftest_rooms.py -- offline check of the room flow with scripted players (no console).

Starts the game server on 127.0.0.1 (temp data and log dirs) and drives fake players through
nickname registration, list, create, password, join, chat, start, a two-round match, ranking,
find, leave and disconnect. Proves the server's side of the protocol is consistent; it does not
prove the PS2 client accepts it (that needs a console or PCSX2).

  python -m bomb.selftest_rooms
"""
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="bomb-selftest-")
os.environ["BOMB_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["BOMB_DATA_DIR"] = os.path.join(TMP, "data")

from . import admin, game, live, relay  # noqa: E402  (read the env at import)
from .fakefeega import FakeFeega, common_inq_id  # noqa: E402
from .fakeplayer import Player, login_payload  # noqa: E402

FAILS = []


def check(what, cond):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        FAILS.append(what)


def serve():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    port = s.getsockname()[1]

    def loop():
        while True:
            c, addr = s.accept()
            threading.Thread(target=game.Session(c, f"{addr[0]}:{addr[1]}").run, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return port


def s32(p):
    return struct.unpack_from("<i", p)[0]


def s16(p, o=0):
    return struct.unpack_from("<h", p, o)[0]


def one_shot(port, cmd, payload, want):
    """A bare connection (the login / registration connections are not lobby sessions)."""
    p = Player("127.0.0.1", port, "X", quiet=True)
    m = p.mark()
    p.send(cmd, payload)
    _, reply = p.wait(want, since=m)
    got = p.got(0x07e1, m)
    p.sock.close()
    return reply, got


def subs(player, since, sub):
    return [p for p in player.got(0x044c, since) if s16(p) == sub]


def wait_sub(player, since, sub, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        found = subs(player, since, sub)
        if found:
            return found
        time.sleep(0.05)
    raise TimeoutError(f"{player.name}: no sub 0x{sub:x}")


class RelayClient:
    """A console's side of the match relay (0x327580 / 0x327360)."""

    def __init__(self, port, key):
        self.key = key
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def hello_master(self):
        self.sock.sendall(relay.frame(1, self.key))

    def hello_member(self, master_key):
        self.sock.sendall(relay.frame(3, self.key, master_key))

    def data(self, to_key, payload):
        self.sock.sendall(relay.frame(0x10, self.key, to_key, 0, payload))

    def recv(self):
        while True:
            if len(self.buf) >= relay.HDR.size:
                n, ftype, a, b, flag = relay.HDR.unpack_from(self.buf)
                if len(self.buf) >= n:
                    payload, self.buf = self.buf[relay.HDR.size:n], self.buf[n:]
                    return ftype, a, b, flag, payload
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("relay closed")
            self.buf += chunk


def relay_checks():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    threading.Thread(target=relay.serve, args=("127.0.0.1", port, lambda m: None), daemon=True).start()
    time.sleep(0.3)
    seed = 0x1234
    mk, k1, k2 = (seed << 16) + 7, (seed << 16) + 8, (seed << 16) + 9
    m1 = RelayClient(port, k1)
    m1.hello_member(mk)            # a member before its master: it waits
    time.sleep(0.2)
    master = RelayClient(port, mk)
    master.hello_master()
    t, a, b, _f, _p = master.recv()
    check("relay: a waiting member is announced to the master (type 4, B = member key)", t == 4 and b == k1)
    master.sock.sendall(relay.frame(5, mk, k1))
    t, _a, _b, flag, _p = m1.recv()
    check("relay: the master's type 5 links the member (type 6, flag 1)", t == 6 and flag == 1)
    m2 = RelayClient(port, k2)
    m2.hello_member(mk)
    t, a, b, _f, _p = master.recv()
    master.sock.sendall(relay.frame(5, mk, b))
    t2, *_ = m2.recv()
    check("relay: a member after its master links too", t == 4 and b == k2 and t2 == 6)
    m2.hello_member(mk)
    t, *_ = m2.recv()
    check("relay: a repeated hello is answered with type 6 again", t == 6)
    master.data(k2, b"star frame")
    t, a, b, _f, p = m2.recv()
    check("relay: master data reaches the member it names (B)", t == 0x10 and a == mk and p == b"star frame")
    m1.data(0, b"up")              # members leave B unset: routed by connection
    t, a, _b, _f, p = master.recv()
    check("relay: member data reaches its master with the member key in A", t == 0x10 and a == k1 and p == b"up")
    m1.sock.close()
    t, a, *_ = master.recv()
    check("relay: a member leaving is reported to the master (type 7, A = its key)", t == 7 and a == k1)
    master.sock.close()
    t, a, *_ = m2.recv()
    check("relay: the master leaving is reported to its members (type 7)", t == 7 and a == mk)


def admin_checks(a, port):
    """Admin pushes (admin.py): a sits in a room, not in a match."""
    who = game.admin_request({"op": "who"})
    check("admin: who lists ALICE", any(r["name"] == "ALICE" for r in who["players"]))
    ma = a.mark()
    out = game.admin_request({"op": "banner", "text": "Restart in 10 min"})
    _, p = a.wait(0x07e6, since=ma)
    check("admin: a banner reaches a player in a room (0x07e6 type 1, system colour)",
          out["ok"] and s16(p) == 1 and p[2:] == b"\x01Restart in 10 min\0")
    ma = a.mark()
    out = game.admin_request({"op": "chat", "text": "x" * 80})
    a.wait(0x07e6, since=ma)
    time.sleep(0.3)
    lines = a.got(0x07e6, ma)
    check("admin: a long chat announcement is split into lines the client keeps (<= 0x35 B)",
          out["frames"] == 2 and len(lines) == 2 and all(s16(p) == 2 and len(p) - 2 <= 0x35 for p in lines))
    ma = a.mark()
    out = game.admin_request({"op": "warn", "name": "alice"})
    a.wait(0x07ef, since=ma)
    check("admin: warn finds the player by name, any case (0x07ef)", out["ok"] and out["sent_to"] == 1)
    check("admin: warn for nobody online fails", not game.admin_request({"op": "warn", "name": "NOBODY"})["ok"])
    check("admin: tip index out of range is refused",
          not game.admin_request({"op": "tip", "index": 13, "text": "x"})["ok"])
    check("admin: tip stored", game.admin_request({"op": "tip", "index": 4, "text": "Hold O to throw"})["ok"]
          and game.load_tips() == {4: "Hold O to throw"})
    e = Player("127.0.0.1", port, "ERIN", quiet=True)
    me = e.mark()
    e.enter_lobby()
    _, p = e.wait(0x07f2, since=me)
    check("admin: a stored tip is sent at lobby entry (0x07f2 index, text)",
          struct.unpack_from("<I", p)[0] == 4 and p[4:] == b"Hold O to throw\0")
    game.admin_request({"op": "tip", "index": 4, "clear": True})
    check("admin: tip --clear removes it", game.load_tips() == {})
    check("admin: an unknown op is refused", not game.admin_request({"op": "nuke"})["ok"])
    if hasattr(socket, "AF_UNIX"):
        path = os.path.join(TMP, "admin-test.sock")
        admin.serve(game.admin_request, log=lambda m: None, path=path)
        check("admin: the unix socket answers who", admin.request({"op": "who"}, path=path)["ok"])
        check("admin: the socket is 0600", (os.stat(path).st_mode & 0o777) == 0o600)
        check("admin: a malformed request is answered, not fatal",
              admin.request({"op": "tip", "index": "x"}, path=path)["ok"] is False)
    else:
        print("skip admin socket checks (no AF_UNIX here)")
    e.sock.close()


def _profile_keys():
    with open(game._profiles_path(), encoding="utf-8") as f:
        return sorted(json.load(f))


def live_checks(rid):
    """The boards' snapshot (live.py) after the two-round match above: ALICE won both rounds."""
    before = _profile_keys()
    snap = live.snapshot()
    m = snap["matches"][0] if snap["matches"] else {}
    check("live: the match is recorded with ALICE as the winner, 2 rounds",
          m.get("winners") == ["ALICE"] and m.get("rounds") == 2 and m.get("room") == "room one")
    check("live: ALICE leads the points standings with 20",
          snap["standings"]["points"][:1] == [{"place": 1, "name": "ALICE", "value": 2 * game.ROUND_POINTS,
                                               "wins": 2, "rounds": 2, "char": 3}])
    by = {p["name"]: p for p in m.get("players") or []}
    check("live: the match players carry their Bomberman (ALICE preset 3, BOB his custom look)",
          by.get("ALICE", {}).get("char") == 3 and "look" not in by.get("ALICE", {})
          and by.get("BOB", {}).get("char") == -1
          and by.get("BOB", {}).get("look") == [[31, 0, 0], [0, 0, 31], [16, 21, 0], [31, 31, 31]])
    online = {p["name"]: p for p in snap["players"]}
    check("live: an online player who never saved a character is preset 0",
          online.get("CAROL", {}).get("char") == 0 and online.get("ALICE", {}).get("char") == 3)
    bob = next((r for r in snap["standings"]["rounds"] if r["name"] == "BOB"), {})
    check("live: a standings row of a custom look carries the look", bob.get("char") == -1
          and len(bob.get("look") or []) == 4)
    check("live: reading the characters creates no profile (only ALICE and BOB saved one)",
          _profile_keys() == before and len(before) == 2)
    room = next((r for r in snap["rooms"] if r["id"] == rid), None)
    check("live: the room is listed, locked, not in a game, with its two players",
          room is not None and room["locked"] and not room["in_game"]
          and sorted(room["players"]) == ["ALICE", "BOB"])
    check("live: everyone in the lobby or a room is online", snap["online"] == len(snap["players"]) >= 3)
    text = json.dumps(snap, ensure_ascii=False)
    check("live: no password, account id or player id is published",
          '"pw"' not in text and "inq" not in text and '"pid"' not in text)
    live.write()
    with open(live.path(), encoding="utf-8") as f:
        check("live: board/live.json is written", json.load(f)["game"] == "bomb")


def main():
    game.emit = lambda *a, **k: None  # keep the output to the checks
    rules = game.parse_advertise_map(
        "192.168.0.0/16=192.168.0.10,100.64.0.0/10=100.64.0.20,0.0.0.0/0=198.51.100.5")
    check("advertise map: LAN peer gets the LAN address",
          game.advertise_for("192.168.0.50:1234", rules) == "192.168.0.10")
    check("advertise map: VPN peer gets the VPN address",
          game.advertise_for("100.64.3.4:1234", rules) == "100.64.0.20")
    check("advertise map: internet peer gets the public address",
          game.advertise_for("203.0.113.7:1234", rules) == "198.51.100.5")
    check("advertise map: empty map falls back to BOMB_GAME_ADDR",
          game.advertise_for("203.0.113.7:1234", []) == game.GAME_ADDR)
    entry = game.lobby_entry(0, "198.51.100.125")
    check("lobby entry carries the advertised address at +0x22",
          entry[0x22:0x22 + 15].rstrip(b"\0") == b"198.51.100.125")
    check("lobby entry has a key, a name and a population in 1..9998",
          entry[0:2] == b"L1" and entry[0x0e:0x15] == b"Lobby 1" and 1 <= s16(entry, 0x3c) <= 9998)
    check("cp932 cut never splits a two-byte character",
          game.cut_cp932("\u3042\u3044\u3046".encode("cp932"), 5) == "\u3042\u3044".encode("cp932"))
    port = serve()

    # Nickname registration (opt-in, BOMB_REQUIRE_NICKNAME=1): no nickname -> 0x0405 -7 (after the
    # lobby entries); 0x0412 -> 1; the same nickname for another account -> -2; then login is 0.
    game.REQUIRE_NICKNAME = True
    feega = FakeFeega()
    inq = common_inq_id("NEWBIE")
    res, entries = one_shot(port, 0x0404, login_payload(inq), 0x0405)
    check("login without a nickname answers -7 (registration) after the lobby entries",
          s32(res) == -7 and len(entries) == game.LOBBY_COUNT)
    res, _ = one_shot(port, 0x0412, b"Bomber".ljust(13, b"\0") + inq.encode() + b"\0", 0x0413)
    check("0x0412 registers the nickname (1)", s32(res) == 1)
    other = common_inq_id("OTHER")
    res, _ = one_shot(port, 0x0412, b"Bomber".ljust(13, b"\0") + other.encode() + b"\0", 0x0413)
    check("a taken nickname is refused (-2)", s32(res) == -2)
    res, _ = one_shot(port, 0x0404, login_payload(inq), 0x0405)
    check("login with a nickname answers 0", s32(res) == 0)
    check("the display name is the nickname", game.account_name(inq) == b"Bomber")
    game.REQUIRE_NICKNAME = False

    for name in ("ALICE", "BOB", "CAROL", "DAVE"):  # what a Feega login records
        feega.remember(name)
    a = Player("127.0.0.1", port, "ALICE", quiet=True)
    b = Player("127.0.0.1", port, "BOB", quiet=True)
    c = Player("127.0.0.1", port, "CAROL", quiet=True)
    check("empty lobby lists no rooms", a.enter_lobby() == [])
    b.enter_lobby()
    c.enter_lobby()
    check("player ids are distinct", len({a.pid, b.pid, c.pid}) == 3)
    # the Bombermen the boards draw: ALICE picks preset 3 (blue), BOB a custom look, CAROL saves
    # nothing (preset 0). Block "a" +8 s16 is the character; "look" is 4 x RGB 0..31.
    m = a.mark()
    a.send(0x043a, struct.pack("<8xh", 3))
    a.wait(0x043b, since=m)
    m = b.mark()
    b.send(0x043a, struct.pack("<8xh", -1))
    b.wait(0x043b, since=m)
    m = b.mark()
    b.send(0x0446, bytes([31, 0, 0, 0, 0, 31, 16, 21, 0, 31, 31, 31]))
    b.wait(0x0447, since=m)
    opts = a.got(0x043d)[-1]
    check("new player's options are the client default 0,1,2,3", opts == bytes.fromhex("00000100020003000000"))
    check("new player's quick chat holds the default texts (English)",
          a.got(0x0441)[-1] == game.PROFILE_DEFAULTS["chat"])
    old = game._chat_block(["old"] * 8)
    game.save_profile("0000000001", {"chat": old.hex()})
    saved, game.OLD_CHAT_SHA1 = game.OLD_CHAT_SHA1, hashlib.sha1(old).hexdigest()
    check("a stored chat block equal to the old default is served as today's default",
          game.profile_block("0000000001", "chat") == game.PROFILE_DEFAULTS["chat"])
    game.OLD_CHAT_SHA1 = saved
    check("any other stored chat block is the player's own",
          game.profile_block("0000000001", "chat") == old)
    with open(game._profiles_path(), encoding="utf-8") as f:
        allp = json.load(f)
    allp.pop("0000000001", None)
    with open(game._profiles_path(), "w", encoding="utf-8") as f:
        json.dump(allp, f)
    check("lobby entry sends the own header (0x07f0) with the name at +0x43",
          a.got(0x07f0)[-1][0x43:0x48] == b"ALICE")

    ma, mb, mc = a.mark(), b.mark(), c.mark()
    a.lobby_chat("hi lobby")
    c.wait(0x03f1, since=mc)
    check("lobby chat (0x03f0) goes out as 0x03f1 with the speaker's name",
          c.got(0x03f1, mc)[0].startswith(b"ALICE"))
    check("lobby chat is not sent as room chat (0x03ff)", c.got(0x03ff, mc) == [])

    mb = b.mark()
    rid = a.create("room one", b"pw")
    check("create returns a room id >= 1", rid >= 1)
    _, row = b.wait(0x07d5, since=mb)
    check("lobby gets the new room (0x07d5) with 1 member and a lock marker",
          s32(row) & 0xffff == rid and s16(row, 2) == 1 and row[0x1e] == ord("*"))
    check("the lobby row does not carry the password", b"pw" not in row)
    check("room list shows it", b.list_rooms() == [rid])
    own = a.got(0x07d7)[-1]
    check("host record: direct match link (+0x34 1) and its address at +0x36",
          s16(own, 0x34) == 1 and own[0x36:0x36 + 9] == b"127.0.0.1")
    check("host gets its own 0x07ee profile after its record", s16(a.got(0x07ee)[-1]) == a.pid)

    check("wrong password is refused (-4)", b.join(rid, b"nope") == -4)
    check("unknown room is refused (-1)", b.join(999) == -1)
    ma, mb, mc = a.mark(), b.mark(), c.mark()
    check("right password joins", b.join(rid, b"pw") == rid)
    _, joined = a.wait(0x07d8, since=ma)
    check("host gets 0x07d8 for the joiner", s16(joined) == b.pid and s16(joined, 2) == 1 and joined[4:7] == b"BOB")
    _, prof = a.wait(0x07ee, since=ma)
    check("host gets the joiner's 0x07ee profile", s16(prof) == b.pid and len(prof) == 0x82)
    recs = b.got(0x07d7, mb)
    check("joiner gets both slot records, host id in +0x30",
          sorted(s16(r) for r in recs) == sorted([a.pid, b.pid]) and all(s16(r, 0x30) == a.pid for r in recs))
    check("joiner gets both profiles", sorted(s16(r) for r in b.got(0x07ee, mb)) == sorted([a.pid, b.pid]))
    _, upd = c.wait(0x07dd, since=mc)
    check("lobby row updated to 2 members", s16(upd, 2) == 2)

    ma, mb, mc = a.mark(), b.mark(), c.mark()
    b.chat("hello")
    a.wait(0x03ff, since=ma)
    b.wait(0x03ff, since=mb)
    time.sleep(0.3)
    check("room chat reaches both members with the speaker's name", a.got(0x03ff, ma)[0].startswith(b"BOB"))
    check("room chat does not reach the lobby", c.got(0x03ff, mc) == [] and c.got(0x03f1, mc) == [])
    ma = a.mark()
    b.chat("x" * 120)
    time.sleep(0.3)
    lines = a.got(0x03ff, ma)
    check("a long chat line is split into pieces of <= 53 bytes",
          len(lines) == 3 and all(len(x) <= 53 for x in lines))

    room = game.ROOMS[rid]
    check("free-for-all: every record carries its own slot as the team (+0x2e)",
          all(s16(room.record(m), 0x2e) == room.slot_of(m) for m in room.everyone()))
    struct.pack_into("<h", room.hdr, 0x2c, 1)   # team mode: the team menu's pick counts
    ma, mb = a.mark(), b.mark()
    b.send(0x0402, struct.pack("<hhhh", b.pid, 0, 5, 2))
    _, rec = a.wait(0x07d7, since=ma)
    check("a guest's 0x0402 sets its team (+0x2e) but not the map", s16(rec) == b.pid and s16(rec, 0x2e) == 2
          and s16(rec, 0x2c) == 0)
    struct.pack_into("<h", room.hdr, 0x2c, 2)

    ma = a.mark()
    a.send(0x040e, struct.pack("<I", 5))
    _, ok = a.wait(0x040f, since=ma)
    _, hdr = a.wait(0x07e5, since=ma)
    check("host closes empty slot 5 (0x040e -> 0x040f 0, header +0x3b set)", s32(ok) == 0 and hdr[0x36 + 5] == 1)

    mb = b.mark()
    b.start()
    b.wait(0x03fb, since=mb)
    time.sleep(0.3)
    check("a guest's start gets 0x03fb only (no refusal, no go)", b.got(0x07e4, mb) == [] and b.got(0x07dc, mb) == [])

    # A two-round match: rules +0x30 = 2 wins.
    a.send(0x041d, struct.pack("<hhh", 2, 180, 1))
    time.sleep(0.3)
    ma, mb, mc = a.mark(), b.mark(), c.mark()
    a.start()
    a.wait(0x07dc, since=ma)
    b.wait(0x07dc, since=mb)
    ga, gb = wait_sub(a, ma, 0x467)[0], wait_sub(b, mb, 0x467)[0]
    check("both members get sub 0x467 with the same seed", ga == gb)
    _, ing = c.wait(0x07dd, since=mc)
    check("lobby row shows the room in a game", ing[4] == 1)
    check("a join while in a game is refused (-2)", c.join(rid) == -2)

    ma, mb = a.mark(), b.mark()
    b.report_round({a.pid: 0, b.pid: 1}, 0)
    a.report_round({a.pid: 0, b.pid: 1}, 1)
    rows = wait_sub(b, mb, 0x468)
    time.sleep(0.3)
    rows = {s16(r, 4): r for r in subs(b, mb, 0x468)}
    check("round 1: a 0x468 row per player, the winner marked",
          set(rows) == {a.pid, b.pid} and s16(rows[a.pid], 0x0a) == 1 and s16(rows[b.pid], 0x0a) == 0
          and s32(rows[a.pid][0x10:]) == game.ROUND_POINTS)
    check("round 1: not the last round (0x469, no 0x46a)", subs(b, mb, 0x469) and not subs(b, mb, 0x46a))
    second = wait_sub(a, ma, 0x467)
    check("round 2 starts once the 0x466s are back (new 0x467)", len(second) == 1)

    ma, mb, mc = a.mark(), b.mark(), c.mark()
    b.report_round({a.pid: 0, b.pid: 1}, 0)
    a.report_round({a.pid: 0, b.pid: 1}, 1)
    wait_sub(b, mb, 0x46a)
    check("round 2: two wins end the match (0x46a)", bool(subs(a, ma, 0x46a)))
    _, back = c.wait(0x07dd, since=mc)
    check("match over frees the room in the lobby", back[4] == 0)
    ma = a.mark()
    a.send(0x0400)
    a.wait(0x07df, since=ma)
    check("0x0400 (back to the room) refreshes the room state", bool(a.got(0x07e5, ma)))

    ma = a.mark()
    a.send(0x0419, struct.pack("<I", 1))
    _, page = a.wait(0x041a, since=ma)
    check("ranking page 1 lists ALICE first with 20 points",
          s32(page) == 1 and page[4:9] == b"ALICE" and s32(page[0x14:]) == 2 * game.ROUND_POINTS)
    ma = a.mark()
    a.send(0x041b, b"ALICE\0")
    _, rank = a.wait(0x041c, since=ma)
    check("ranking search finds ALICE at rank 1", s32(rank) == 1)
    live_checks(rid)
    mc = c.mark()
    c.send(0x040a, b"BOB\0")
    _, found = c.wait(0x040b, since=mc)
    check("find BOB: in room", s16(found) == 0 and s16(found, 0x10) == rid)
    mc = c.mark()
    c.send(0x040a, b"NOBODY\0")
    _, found = c.wait(0x040b, since=mc)
    check("find an unknown name: -9", s16(found) == -9)

    mb = b.mark()
    a.leave()
    _, left = b.wait(0x07d9, since=mb)
    check("remaining member gets 0x07d9 with the leaver's id", s32(left) == a.pid)
    _, host = b.wait(0x07df, since=mb)
    check("host passes to the remaining member (0x07df)", s32(host) == b.pid)
    check("new slot records name the new host", all(s16(r, 0x30) == b.pid for r in b.got(0x07d7, mb)))

    ma = a.mark()
    # A dropped connection leaves the room like 0x03f8. shutdown() first: on Linux a bare close()
    # while the reader thread sits in recv() sends no FIN, so the server never sees the drop.
    b.sock.shutdown(socket.SHUT_RDWR)
    b.sock.close()
    _, gone = a.wait(0x07d6, since=ma)
    check("an emptied room is removed from the lobby (0x07d6)", s32(gone) == rid)
    check("room list is empty again", a.list_rooms() == [])

    rid2 = c.create("full", b"")
    struct.pack_into("<h", game.ROOMS[rid2].hdr, 0x2e, 2)
    game.ROOMS[rid2].max = 2
    check("second room joins without a password", a.join(rid2) == rid2)
    d = Player("127.0.0.1", port, "DAVE", quiet=True)
    d.enter_lobby()
    check("a full room is refused (-3)", d.join(rid2) == -3)
    md = d.mark()
    d.send(0x03f2, struct.pack("<I", 0))
    _, out = d.wait(0x03f3, since=md)
    check("leaving the lobby (0x03f2) answers 0x03f3 0", s32(out) == 0)

    admin_checks(a, port)
    relay_checks()

    print(f"\n{'ALL OK' if not FAILS else f'{len(FAILS)} FAILED'} (logs/data in {TMP})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
