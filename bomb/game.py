"""game.py -- the Bomberman Online game server (ps2bb-pkggame01, TCP 10020).

Framing (both directions, little-endian): u16 command, u16 payload length, payload. The
client's receive dispatcher is MAIN.BIN 0x3398e0 (base 0x300000); it reads the command at +0
and hands the payload (+4) to a per-command handler.

Known so far:
  C->S 0x0404 login (193 B): CommonInqID (zero-padded to 0x75), the console's IP (to 0x86),
       u16 0x0010, three u32 string lengths, then CommonInqID, ServiceID, EndDate (NUL-terminated),
       all as Feega returned them.
  S->C 0x0405 login result: s32. >= 0 is success (MAIN 0x337290); -7 / -14 / -15 open the
       nickname registration screen; other negative codes show messages (MAIN 0x336db8).

The match itself runs between the consoles (TCP 10080 to the host, see MATCH_MODE); this server
starts it, collects the round reports and decides rounds and the match.

Everything else is logged (hexdump) so the next command shows up.

Run:
  python -m bomb.game --bind 0.0.0.0 --port 10020
Logs: $BOMB_LOG_DIR (default ./data/logs): game.log and game.jsonl.
The boards' snapshot: $BOMB_DATA_DIR/board/live.json (live.py; BOMB_LIVE_FILE, BOMB_LIVE_EVERY).
"""
import argparse
import hashlib
import random
import json
import os
import socket
import struct
import sys
import threading
import time

from . import admin, adminport, feegalink, live

LOG_DIR = os.environ.get("BOMB_LOG_DIR", os.path.join(os.getcwd(), "data", "logs"))
_lock = threading.Lock()


def hexdump(b, width=16):
    return "\n".join(f"  {i:04x}  {b[i:i + width].hex(' '):<{width * 3}} "
                     + "".join(chr(c) if 32 <= c < 127 else "." for c in b[i:i + width])
                     for i in range(0, len(b), width))


def emit(peer, direction, cmd, payload, note=""):
    t = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{t} {peer} {direction} 0x{cmd:04x} len={len(payload)} {note}".rstrip()
    rec = {"t": t, "peer": peer, "dir": direction, "cmd": cmd, "len": len(payload),
           "hex": payload.hex(), "note": note}
    with _lock:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "game.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        with open(os.path.join(LOG_DIR, "game.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n" + hexdump(payload) + "\n")
        try:
            print(line + "\n" + hexdump(payload), flush=True)
        except UnicodeEncodeError:  # a non-UTF-8 console must not break the session
            print(ascii(line) + "\n" + hexdump(payload), flush=True)


LOBBY_COUNT = 10
GAME_PORT = int(os.environ.get("BOMB_GAME_PORT", "10020"))
# Address the client connects to for a lobby: a dotted IP string, copied from the entry's +0x22 into
# the game-host buffer (MAIN 0x3371a8). Left empty, the client looks up "".
GAME_ADDR = os.environ.get("BOMB_GAME_ADDR", "127.0.0.1")
# The address must be one the client can reach from ITS network, so pick it per source, first
# CIDR match wins, e.g. "192.168.0.0/16=192.168.0.10,0.0.0.0/0=203.0.113.5" (players at home get
# the server's LAN address, everyone else the public one). Empty = GAME_ADDR for all.
ADVERTISE_MAP = os.environ.get("BOMB_ADVERTISE_MAP", "")


def parse_advertise_map(spec):
    import ipaddress
    rules = []
    for item in spec.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        cidr, addr = item.split("=", 1)
        rules.append((ipaddress.ip_network(cidr.strip(), strict=False), addr.strip()))
    return rules


_ADVERTISE_RULES = parse_advertise_map(ADVERTISE_MAP)


def advertise_for(peer_ip, rules=None):
    """The lobby address to hand a client at peer_ip ("a.b.c.d" or "a.b.c.d:port")."""
    import ipaddress
    rules = _ADVERTISE_RULES if rules is None else rules
    try:
        ip = ipaddress.ip_address(peer_ip.rsplit(":", 1)[0] if peer_ip.count(":") == 1 else peer_ip)
    except ValueError:
        return GAME_ADDR
    for net, addr in rules:
        if ip.version == net.version and ip in net:
            return addr
    return GAME_ADDR


def lobby_entry(i, game_addr=None, population=1):
    """One S->C 0x07e1 lobby entry, 64 bytes, appended to the client's table at 0x66aed8
    (MAIN 0x339778, up to 20). Every field the client reads:
      +0x00 key string (<= 10 B; find-user 0x040b +0x12 is strcmp'd against it, 0x3646c8)
      +0x0c s16 which of the 10 lobby buttons (0x34a800; also auto lobby selection 0x337618)
      +0x0e lobby name (char[20], only shown by find-user, 0x364670)
      +0x22 NUL-terminated address string (<= 15 chars; copied by 0x3371a8 and connected to)
      +0x34 u32 port the lobby connection uses (0x337250 -> 0x369bc0(host=+0x22, port=+0x34))
      +0x38 u16 port the login-time connection uses (0x336f9c)
      +0x3c s16 population: the lobby counter (0x337258) and auto selection, which only picks an
            entry in 1..9998 (0x3375e0), so never 0.
    +0x04 / +0x0a / +0x2c are never read."""
    e = bytearray(0x40)
    key = f"L{i + 1}".encode("ascii")
    e[0:len(key)] = key
    struct.pack_into("<h", e, 0x0c, i)
    name = f"Lobby {i + 1}".encode("ascii")
    e[0x0e:0x0e + len(name)] = name
    addr = (game_addr or GAME_ADDR).encode("ascii")[:15]
    e[0x22:0x22 + len(addr)] = addr
    struct.pack_into("<I", e, 0x34, GAME_PORT)
    struct.pack_into("<H", e, 0x38, GAME_PORT)
    struct.pack_into("<h", e, 0x3c, max(1, min(9998, population)))
    return bytes(e)


def udp_logger(bind, port):
    """The client opens UDP from its port 10060 to our 10020 once in a lobby. The heartbeat's source
    port is that console's NAT-mapped 10060; slot record +0x48 hands it to the other members for
    the optional UDP mesh (probes to record +0x36 : +0x48)."""
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind((bind, port))
    beats = 0
    while True:
        data, addr = u.recvfrom(65536)
        peer = f"{addr[0]}:{addr[1]}"
        UDP_PORTS[addr[0]] = addr[1]
        cmd = struct.unpack_from("<H", data)[0] if len(data) >= 2 else 0
        if cmd == 0x0514:
            # Heartbeat (sender 0x337700): every 20 frames until S->C 0x0515 sets 0x4e0ed8
            # (0x337780), then every 600. 0x0515 is a TCP-dispatcher command, so it goes once
            # per session on the game connection. Never answer on UDP: in a match the console
            # reads its 10060 socket as mesh data (0x36aec0) and took our UDP 0x0515 for a
            # 0x515-byte record, overflowing a 512-byte stack buffer.
            with _slock:
                due = [t for t in SESSIONS if not t.beat_acked
                       and t.peer.rsplit(":", 1)[0] == addr[0]]
                for t in due:
                    t.beat_acked = True
            for t in due:
                try:
                    t.send(0x0515, b"", "udp heartbeat seen (0x0515 over TCP)")
                except OSError:
                    pass
            beats += 1
            if beats % 50 == 1:
                emit(peer, "UDP C->S", cmd, data, f"udp heartbeat #{beats}")
            continue
        emit(peer, "UDP C->S", cmd, data, "udp datagram")


def cstr(b):
    return b.split(b"\0", 1)[0].decode("latin1")


def cstr_jp(b):
    return b.split(b"\0", 1)[0].decode("cp932", "replace")


ROOMS = {}          # room id -> Room; guarded by _slock
LOBBY_ROWS = 6      # the lobby's room table 0x4e55d8 holds 6 rows of 0x3e bytes
ROOM_MAX = 8        # 8 slot records (0x4e5750, 8 x 0x5c)
# Profile blocks: load reply command -> (name, size, save command). "a" = player-menu settings
# (+0 lobby of the last find/jump, +4 / +6 toggles, +8 s16 character: 0..7 preset, -1 = custom
# look), "options" = button config, "chat" = the 8 quick-chat messages, "look" = 4 x RGB (0..31).
PROFILE_BLOCKS = {0x0439: ("a", 10, 0x043a), 0x043d: ("options", 10, 0x043e),
                  0x0441: ("chat", 0x70, 0x0442), 0x0445: ("look", 12, 0x0446)}
# New-player blocks. "options" +0..+6 are four s16 button indexes into {O, X, Tri, Sq, L1, L2, R1,
# R2} (0x747c40); the client's own Default (0x35fe18) writes 0, 1, 2, 3, while all zeros put every
# action on O. "chat" = 8 s16 balloon types, then 8 x 12-byte cp932 texts; the client's defaults
# are the strings at 0x748d18. "a" and "look" are valid as zeros (a +8 = 0 is preset character 0).
# English by default, the same lines an English client's Default button writes over
# 0x748d18; the server cannot tell an English client from a Japanese one, and a Japanese player's
# Default button still restores the originals.
DEFAULT_CHAT_EN = ["Mine!", "Yes!", "I won!", "Got me", "Lucky!", "Go go!", "Oops!", "Too bad"]
DEFAULT_CHAT_TEXTS = DEFAULT_CHAT_EN
# SHA-1 of the chat block holding the client's own (Japanese) defaults, which older servers handed
# out: a stored block equal to it was never edited, so it is not the player's choice.
OLD_CHAT_SHA1 = "994a0db1552c17402ecd86a879a49fdff7f03cb5"


def _chat_block(texts):
    return bytes(16) + b"".join(t.encode("cp932").ljust(12, b"\0") for t in texts)


PROFILE_DEFAULTS = {
    "options": bytes.fromhex("00000100020003000000"),
    "chat": _chat_block(DEFAULT_CHAT_TEXTS),
}
SESSIONS = set()
_slock = threading.RLock()
CHAT_NAME = b"PLAYER"  # fallback when the account has neither a nickname nor a Feega AuthID on file
NAME_MAX = 12          # nickname size everywhere (registration 0x4e5558; slot record +4 must end before +0x18)
CHAT_LINE_MAX = 52     # 0x336060 keeps 0x35 bytes of a chat line, NUL included
PLAIN_ACKS = {0x041D: 0x041E, 0x041F: 0x0420, 0x0402: 0x0403}
SAVE_BLOCKS = {save: name for name, _size, save in PROFILE_BLOCKS.values()}
DATA_DIR = os.environ.get("BOMB_DATA_DIR", os.path.join(os.getcwd(), "data"))
_plock = threading.RLock()
# The match runs between the consoles, not through this server: every console listens on TCP 10080
# and the members connect to the master (the host). Slot record +0x34 of the master picks the path
# (0x30f2ac: G+0x1b4 = (entry+0xcc == 0); 0x3369c4 copies record +0x34 to entry+0xcc): nonzero =
# members connect straight to the master's record +0x36 address on 10080; 0 = both sides go through
# the relay named by S->C 0x07f0 (+0x52 host, +0x64 master port, +0x68 member port). "direct" works
# on a LAN only (the host must accept inbound TCP 10080); "relay" uses bomb.relay, which this
# process starts on BOMB_RELAY_PORT (no open ports needed on any console). Relay mode with no relay
# would end every match in BBG01, so relay is the default only when a relay port is set.
RELAY_PORT = int(os.environ.get("BOMB_RELAY_PORT", "0"))
MATCH_MODE = os.environ.get("BOMB_MATCH_MODE", "relay" if RELAY_PORT else "direct")
# The relay address handed out in 0x07f0 (a dotted IP, <= 17 chars): empty = the same per-source
# address as the lobby entries (BOMB_ADVERTISE_MAP), since the relay runs on this host.
RELAY_HOST = os.environ.get("BOMB_RELAY_HOST", "")
# Retail made new accounts register a nickname: login 0x0405 -7 opens the registration screen
# (0x140001), which posts to pkg03 db_write.asp (HTTPS on 443) and then sends 0x0412. Off by
# default: it needs a TLS 1.0 front for pkg03 on 443 that the clients trust, and without one
# the post fails and the player is stuck. With it off the client never posts at all, and the
# Feega AuthID is the player's name.
REQUIRE_NICKNAME = os.environ.get("BOMB_REQUIRE_NICKNAME", "0") == "1"
ROUND_POINTS = 10      # ranking points per round won (the retail scoring is not known)
ROUND_WAIT = 3.0       # seconds to wait for the master's round report (clients give up after ~20 s)
UDP_PORTS = {}         # console IP -> source port of its UDP heartbeat (its NAT-mapped 10060)
# The optional UDP 10060 mesh between consoles (slot record +0x36 / +0x48 / +0x4c). Over the
# internet the consoles' NATs drop each other's unsolicited UDP, so a half-learned mesh sent the
# match state where it never arrived and every match froze at the start. Off by default in relay
# mode: the records then point every probe at the relay's address, where nothing answers, and
# everything goes through the relay. On by default in direct (LAN) mode.
UDP_MESH = os.environ.get("BOMB_UDP_MESH", "0" if MATCH_MODE == "relay" else "1") == "1"
MESH_SINK = os.environ.get("BOMB_MESH_SINK", "192.0.2.1")   # RFC 5737 TEST-NET-1: routed nowhere


def _cp932_width(b):
    return 2 if 0x81 <= b <= 0x9f or 0xe0 <= b <= 0xfc else 1


def cut_cp932(raw, n):
    """raw cut to at most n bytes without splitting a two-byte character."""
    i = 0
    while i < len(raw):
        w = _cp932_width(raw[i])
        if i + w > n:
            break
        i += w
    return raw[:i]


def split_cp932(raw, n):
    out = []
    while raw:
        part = cut_cp932(raw, n) or raw[:1]
        out.append(part)
        raw = raw[len(part):]
    return out


def _profiles_path():
    return os.path.join(DATA_DIR, "profiles.json")


def load_profile(inq_id):
    with _plock:
        try:
            with open(_profiles_path(), encoding="utf-8") as f:
                return json.load(f).get(inq_id or "", {})
        except (OSError, ValueError):
            return {}


def save_profile(inq_id, prof):
    with _plock:
        try:
            with open(_profiles_path(), encoding="utf-8") as f:
                allp = json.load(f)
        except (OSError, ValueError):
            allp = {}
        allp[inq_id or ""] = prof
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _profiles_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allp, f, indent=1)
        os.replace(tmp, _profiles_path())


def profile_block(inq_id, name):
    """One profile block as bytes: the stored copy, else the new-player default, else zeros."""
    size = next(s for n, s, _ in PROFILE_BLOCKS.values() if n == name)
    blk = bytes.fromhex(load_profile(inq_id).get(name, "")) or PROFILE_DEFAULTS.get(name, b"")
    if name == "chat" and hashlib.sha1(blk.ljust(size, b"\0")[:size]).hexdigest() == OLD_CHAT_SHA1:
        blk = PROFILE_DEFAULTS["chat"]      # never edited: the old Japanese default, not a choice
    return blk.ljust(size, b"\0")[:size]


def _players_path():
    return os.path.join(DATA_DIR, "players.json")


def _load_players():
    try:
        with open(_players_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_players(allp):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _players_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(allp, f, indent=1, ensure_ascii=False)
    os.replace(tmp, _players_path())


def player_info(inq_id, update=None):
    """The account's record in $BOMB_DATA_DIR/players.json: pid, nick, points, wins, rounds.
    Player ids are handed out in order and kept: the client sends them as s16 and compares them
    (lh against lw, 0x34bdec), so they must be unique and in 1..32767 (-1 = not logged in).
    update(rec, all_records) may change the record; it is saved afterwards."""
    with _plock:
        allp = _load_players()
        rec = allp.get(inq_id or "")
        if rec is None:
            used = {r.get("pid") for r in allp.values()}
            pid = next(i for i in range(1, 32768) if i not in used)
            rec = allp[inq_id or ""] = {"pid": pid, "nick": "", "points": 0, "wins": 0, "rounds": 0}
            _save_players(allp)
        if update:
            update(rec, allp)
            _save_players(allp)
        return dict(rec)


def auth_name(inq_id):
    """The Feega AuthID of this CommonInqID, as the Feega service has it (feegalink.py)."""
    return feegalink.name(inq_id)


def account_name(inq_id):
    """Display name for room slots, chat, ranking and find: the registered nickname (0x0412), else
    the Feega AuthID. At most NAME_MAX bytes: a longer name in slot record +4 runs into +0x18
    (dimmed name), +0x1a (class), +0x20 / +0x24 (stats) and +0x2c (map)."""
    name = player_info(inq_id).get("nick") or auth_name(inq_id)
    return cut_cp932(name.encode("cp932", "replace"), NAME_MAX) or CHAT_NAME


def find_account(name):
    """CommonInqID whose display name is `name` (bytes), or None."""
    for inq in _load_players():
        if account_name(inq) == name:
            return inq
    return None


def ranking():
    """[(inq_id, record)] with points > 0, best first; rank = index + 1."""
    rows = [(inq, rec) for inq, rec in _load_players().items() if rec.get("points", 0) > 0]
    return sorted(rows, key=lambda r: (-r[1]["points"], r[1]["pid"]))


class Room:
    """One room. The header is the 62-byte record the lobby rows (0x07d4 / 0x07dd, table 0x4e55d8,
    drawn by 0x34ece0) and the room scene (0x07e5, 0x4e5e40) share:
      +0x00 s16 room id (shown as 3 digits)   +0x02 s16 members   +0x04 u8 nonzero = in a game
      +0x05 name (cp932)   +0x1e password (nonzero first byte = lock icon)
      +0x2c s16 game mode (0x041f): 0 teams of equal size, 1 free team sizes, 2 free-for-all
            (create sends 2); modes 0 / 1 enable the team menu
      +0x2e s16 max players (create sends 8; members == max shows "full")
      +0x30 / +0x32 / +0x34 s16 rules (wins, time in s, third; +0x34 nonzero enables sub 0x46b)
      +0x36..+0x3d one byte per slot, drawn on empty slots; the host toggles it with 0x040e
            (nonzero is taken as closed: not confirmed on the client)."""

    def __init__(self, rid, req, host):
        self.id = rid
        self.hdr = bytearray(req[:0x3e].ljust(0x3e, b"\0"))
        self.password = self.hdr[0x1e:0x2c].split(b"\0", 1)[0]
        # Rules default to what the client sends on its own (1, 180, 1); left 0 the room showed
        # 0 wins / 0:00 and a start hung.
        struct.pack_into("<hhh", self.hdr, 0x30, 1, 180, 1)
        self.hdr[0x36:0x3e] = bytes(8)
        self.max = max(2, min(ROOM_MAX, struct.unpack_from("<h", self.hdr, 0x2e)[0] or ROOM_MAX))
        self.members = {0: host}     # slot -> Session
        self.host = host
        self.stage = 0               # host's map pick (0x0402 +4), slot record +0x2c
        self.in_game = False
        self.setup_done = set()      # pids whose sub 0x466 arrived for the current round
        self.setup_evt = threading.Event()
        self.round = 0               # rounds started in the current match
        self.reports = {}            # reporter pid -> {player pid: 1 out / 0 alive} (sub 0x453)
        self.ends = {}               # reporter pid -> round code (sub 0x476 +0x0a)
        self.round_timer = None
        self.wins = {}               # pid -> rounds won in the current match

    def mode(self):
        return struct.unpack_from("<h", self.hdr, 0x2c)[0]

    def wins_needed(self):
        return max(1, struct.unpack_from("<h", self.hdr, 0x30)[0])

    def header(self):
        struct.pack_into("<hh", self.hdr, 0, self.id, len(self.members))
        self.hdr[4] = 1 if self.in_game else 0
        return bytes(self.hdr)

    def lobby_row(self):
        # Same record; the password itself stays on the server, a marker keeps the lock icon.
        row = bytearray(self.header())
        row[0x1e:0x2c] = (b"*" if self.password else b"").ljust(0xe, b"\0")
        return bytes(row)

    def closed(self, slot):
        return self.hdr[0x36 + slot] != 0

    def free_slot(self):
        return next((i for i in range(self.max) if i not in self.members and not self.closed(i)), None)

    def slot_of(self, sess):
        return next(i for i, m in self.members.items() if m is sess)

    def record(self, sess):
        """The 0x5c-byte slot record (stored by 0x34b7a0; copied into the match entry by 0x336718):
          +0x00 s16 player id (-1 = empty)   +0x02 s16 slot   +0x04 name (must end before +0x18)
          +0x18 s16 nonzero dims the name and hides the stats (cleared for the host by 0x34bdcc)
          +0x1a s16 class 0..19 (badge; 0 and 1 show none)   +0x20 u32 / +0x24 u32 stats under
                the slot; +0x24 is also the in-match score base (entry+0x14)
          +0x2c s16 map (the host's is copied into all records, 0x34b9c4)   +0x2e s16 team 0..3
          +0x30 s16 master id: 0x336b14 makes the member whose +0x30 == +0 the match master
          +0x32 s16 stage variant (host)   +0x34 s16 nonzero = direct match link (see MATCH_MODE)
          +0x36 address the members connect to on TCP 10080 when this member is the master
          +0x48 u32 UDP port and +0x4c LAN address for the optional UDP 10060 mesh."""
        me = bytearray(0x5c)
        struct.pack_into("<hh", me, 0, sess.pid, self.slot_of(sess))
        me[4:4 + len(sess.name)] = sess.name
        struct.pack_into("<II", me, 0x20, sess.stats.get("wins", 0), sess.stats.get("points", 0))
        # +0x2e becomes match entry +8, the team the master's round-end check (0x36eb40) groups
        # the living by: one team left = a winner 60-120 frames later. Free-for-all needs every
        # player on a team of their own; left at 0 for all, every round ended ~1 s in with the
        # whole room "dead". Team modes keep the picked team.
        team = self.slot_of(sess) if self.mode() == 2 else sess.team
        struct.pack_into("<hh", me, 0x2c, self.stage, team)
        struct.pack_into("<h", me, 0x30, self.host.pid)
        struct.pack_into("<h", me, 0x34, 1 if MATCH_MODE == "direct" else 0)
        if UDP_MESH:
            ip = sess.peer.rsplit(":", 1)[0].encode("ascii")[:17]
            me[0x36:0x36 + len(ip)] = ip
            struct.pack_into("<I", me, 0x48, UDP_PORTS.get(sess.peer.rsplit(":", 1)[0], 10060))
            lan = (sess.lan_ip or sess.peer.rsplit(":", 1)[0]).encode("ascii", "replace")[:15]
            me[0x4c:0x4c + len(lan)] = lan
        else:
            # A real address that never answers (MESH_SINK, a blackhole): the probes go out and
            # nothing comes back, not even an ICMP error, so the mesh never learns a peer and
            # the state packets stay on the TCP link (the relay). 0.0.0.0 here hung the master's
            # network stack right after it accepted its members; the relay's own
            # address answered the LAN probes with ICMP port unreachable, and a PCSX2 master
            # crashed 4 s later ("Jump to unaligned address").
            sink = MESH_SINK.encode("ascii")[:15]
            me[0x36:0x36 + len(sink)] = sink
            struct.pack_into("<I", me, 0x48, 10060)
            me[0x4c:0x4c + len(sink)] = sink
        return bytes(me)

    def profile(self, sess):
        """S->C 0x07ee, 0x82 bytes (0x34bae8): the member's look and quick chat. Joining resets
        every record and profile id to -1 (0x34b078), and a slot is drawn only when its record and
        its profile are both present (0x34b8cc), so every member needs one after its record (a
        profile with no record is dropped). 0x044c sub 0x468 re-reads it into the match entry.
          +0x00 s16 player id   +0x02 s16 unknown (entry+0xdc)
          +0x04 s16 character: >= 0 preset sprite 0x3e4+n, < 0 the custom look below (block a +8)
          +0x06 12 B look block   +0x12 0x70 B quick-chat block (8 x s16 type, 8 x 12 B text)"""
        p = bytearray(0x82)
        struct.pack_into("<h", p, 0, sess.pid)
        p[4:6] = profile_block(sess.inq_id, "a")[8:10]
        p[6:0x12] = profile_block(sess.inq_id, "look")
        p[0x12:0x82] = profile_block(sess.inq_id, "chat")
        return bytes(p)

    def everyone(self):
        return list(self.members.values())


def lobby_sessions():
    with _slock:
        return [t for t in SESSIONS if t.in_lobby and t.room is None]


def to_lobby(cmd, payload, note):
    """Lobby-wide room table updates: 0x07d5 add (0x34b508, first free row), 0x07dd update by id
    (0x34b578), 0x07d6 remove by id (0x34b5e8, s32)."""
    for t in lobby_sessions():
        try:
            t.send(cmd, payload, note)
        except OSError:
            pass


def _tips_path():
    return os.path.join(DATA_DIR, "tips.json")


def load_tips():
    """Admin lobby tips ($BOMB_DATA_DIR/tips.json): {"index": "text"}, see admin.py."""
    try:
        with open(_tips_path(), encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items() if 0 <= int(k) < admin.TIP_COUNT}
    except (OSError, ValueError):
        return {}


def save_tips(tips):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _tips_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in sorted(tips.items())}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _tips_path())


def tip_payload(i, text):
    """S->C 0x07f2: u32 index, then the text (54 bytes kept, 0x340318)."""
    return struct.pack("<I", i) + cut_cp932(text.encode("cp932", "replace"), admin.TIP_MAX) + b"\0"


def reachable_sessions():
    """Everyone in the lobby or a room, but not in a running match (admin pushes)."""
    with _slock:
        return [t for t in SESSIONS if (t.in_lobby or t.room is not None)
                and not (t.room is not None and t.room.in_game)]


def admin_request(req):
    """The admin's commands from admin.py's socket."""
    op = req.get("op")
    if op == "who":
        with _slock:
            rows = [{"name": t.name.decode("cp932", "replace"), "peer": t.peer,
                     "where": ("match" if t.room is not None and t.room.in_game else
                               f"room {t.room.id}" if t.room is not None else
                               "lobby" if t.in_lobby else "login")} for t in SESSIONS]
        return {"ok": True, "players": rows}
    if op in ("banner", "chat"):
        raw = req.get("text", "").encode("cp932", "replace")
        lead = b"" if req.get("plain") else admin.SYSTEM_COLOUR
        if op == "banner":
            frames = [struct.pack("<h", 1) + lead + cut_cp932(raw, admin.BANNER_MAX - len(lead)) + b"\0"]
        else:
            frames = [struct.pack("<h", 2) + lead + part
                      for part in split_cp932(raw, CHAT_LINE_MAX - len(lead))]
            frames = [f + b"\0" for f in frames]
        to = reachable_sessions()
        for t in to:
            for f in frames:
                try:
                    t.send(0x07E6, f, f"announcement ({op})")
                except OSError:
                    pass
        return {"ok": True, "sent_to": len(to), "frames": len(frames)}
    if op == "warn":
        want = req.get("name", "").casefold()
        with _slock:
            hits = [t for t in SESSIONS if t.name.decode("cp932", "replace").casefold() == want]
        for t in hits:
            try:
                t.send(0x07EF, b"", "conduct warning")
            except OSError:
                pass
        return {"ok": bool(hits), "sent_to": len(hits)} if hits else {"ok": False, "error": "no such player online"}
    if op == "tip":
        i = int(req.get("index", -1))
        if not 0 <= i < admin.TIP_COUNT:
            return {"ok": False, "error": f"index must be 0..{admin.TIP_COUNT - 1}"}
        with _slock:
            tips = load_tips()
            if req.get("clear"):
                tips.pop(i, None)
            elif req.get("text"):
                tips[i] = req["text"]
            else:
                return {"ok": False, "error": "give a text or --clear"}
            save_tips(tips)
        if req.get("clear"):
            return {"ok": True, "cleared": i, "note": "clients show the stock tip again after a restart"}
        to = [t for t in lobby_sessions()]
        for t in to:
            try:
                t.send(0x07F2, tip_payload(i, tips[i]), f"lobby tip {i}")
            except OSError:
                pass
        return {"ok": True, "sent_to": len(to)}
    if op == "tips":
        return {"ok": True, "tips": load_tips()}
    return {"ok": False, "error": f"unknown op {op!r}"}


def chat_lines(name, text):
    """"name\uff1etext" in chunks the client keeps whole (0x336060 copies 0x35 bytes)."""
    return [part + b"\0" for part in split_cp932(name + "\uff1e".encode("cp932") + text, CHAT_LINE_MAX)]


class Session:
    def __init__(self, conn, peer):
        self.conn, self.peer = conn, peer
        self.inq_id = None
        self.pid = 1
        self.name = CHAT_NAME
        self.lan_ip = ""
        self.team = 0
        self.stats = {}
        self.room = None
        self.in_lobby = False
        self.beat_acked = False      # 0x0515 sent for this session's UDP heartbeat
        self._send_lock = threading.Lock()

    def send(self, cmd, payload=b"", note=""):
        # Other sessions' threads send to us too (room and lobby pushes): one frame at a time.
        with self._send_lock:
            self.conn.sendall(struct.pack("<HH", cmd, len(payload)) + payload)
        emit(self.peer, "S->C", cmd, payload, note)

    def send_all(self, sessions, cmd, payload, note):
        for t in sessions:
            try:
                t.send(cmd, payload, note)
            except OSError:
                pass

    def on_login(self, p):
        self.inq_id = cstr(p[0:0x75])
        ip = cstr(p[0x75:0x86])
        nick = player_info(self.inq_id).get("nick")
        code = -7 if REQUIRE_NICKNAME and not nick else 0
        note = f"login inq={self.inq_id} ip={ip}" + (" (no nickname: registration)" if code else "")

        def reply():
            # The 0x0405 handler (MAIN 0x337290) closes the connection first, so everything the
            # lobby-select screen needs must arrive before it. That screen (0x34ae60) shows
            # Failed to load the lobby information (message 0x171) when no lobby entries
            # arrived. After a registration the client goes straight to lobby select with the
            # list it already has, so the entries go out on the -7 path too.
            addr = advertise_for(self.peer)
            with _slock:
                pop = len(SESSIONS)
            for i in range(LOBBY_COUNT):
                self.send(0x07e1, lobby_entry(i, addr, pop), f"lobby entry {i} -> {addr}")
            self.send(0x0405, struct.pack("<i", code),
                      "login result -7 (register a nickname)" if code else "login result 0 (ok)")
        return note, reply

    def on_register(self, p):
        # C->S 0x0412 (0x337368), on a fresh connection with no 0x0404 first, after the
        # registration screen's db_write.asp post answered "1": +0 char[13] nickname (the client
        # rejects spaces, '/' and full-width spaces), +0x0d CommonInqID. S->C 0x0413 s32
        # (0x337450): 1 = registered (the client closes and goes to lobby select), -2 "nickname
        # already in use", -3 "inappropriate words"; anything else is retried 4 times, then -5000.
        nick_raw = cut_cp932(p[0:0x0d].split(b"\0", 1)[0], NAME_MAX)
        inq = cstr(p[0x0d:])
        nick = nick_raw.decode("cp932", "replace")
        result = {}

        def claim(rec, allp):
            if any(r.get("nick") == nick for k, r in allp.items() if k != inq):
                result["code"] = -2
            else:
                rec["nick"] = nick
                result["code"] = 1
        if not nick.strip() or not inq:
            result["code"] = -3
        else:
            player_info(inq, claim)
        code = result["code"]
        self.inq_id = inq
        return f"register nickname {nick!r} for inq={inq} -> {code}", \
            lambda: self.send(0x0413, struct.pack("<i", code), f"nickname registration {code}")

    def on_lobby_login(self, p):
        # C->S 0x03ea when a lobby is picked: same layout as 0x0404. S->C 0x03eb s32: >= 0 is the
        # own player id (0x34b440 -> 0x66b43c) and the client enters the lobby (0x337790);
        # < 0 closes and shows the login failure messages (0x336db8).
        self.inq_id = cstr(p[0:0x75])
        self.lan_ip = cstr(p[0x75:0x86])
        info = player_info(self.inq_id)
        pid = self.pid = info["pid"]
        self.stats = info
        self.name = account_name(self.inq_id)
        note = f"lobby login inq={self.inq_id} name={self.name.decode('cp932', 'replace')} -> player id {pid}"
        return note, lambda: self.send(0x03eb, struct.pack("<i", pid), f"lobby login ok, id {pid}")

    def on_lobby_info(self, p):
        # C->S 0x0438 (sent after 0x03eb, MAIN 0x337790): load the player's profile. The client
        # counts four replies (0x4e0b74) and then sends 0x03ee (0x337d08); one per block:
        #   0x0439 -> 0x66b440 (10 B)   0x043d -> 0x66b450 (10 B, player options)
        #   0x0441 -> 0x66b460 (0x70 B, quick chat)   0x0445 -> 0x66b4d0 (12 B, appearance)
        def reply():
            for cmd, (name, size, _save) in PROFILE_BLOCKS.items():
                self.send(cmd, profile_block(self.inq_id, name), f"profile {name} ({size} B)")
        return "profile load request", reply

    def on_enter(self, p):
        # C->S 0x03ee after the four 0x0439. S->C 0x03ef s32 (handler 0x337d38): nonzero switches
        # to the scene at *0x66b3e8 (the lobby), 0 just returns. Then:
        #   0x07f0 (0x6c B -> 0x66ae60): +0x43 own nickname (the ranking's "find me", 0x3666c0),
        #          +0x50 lobby group, +0x52 / +0x64 / +0x68 match relay host and ports.
        #   0x07d2 own stats (0x3390f0, ignored unless +0 is the own id): +0x10 s16 class,
        #          +0x14 u32 and +0x18 u32 drawn in the lobby (meaning inferred: points, wins).
        #   0x07f1 lobby counters (0x33aa10): s32 lobby users, s32 second counter, s32 maintenance.
        def reply():
            self.in_lobby = True
            self.send(0x03ef, struct.pack("<i", 1), "enter ok (1)")
            self.send_own_header()
            st = bytearray(0x1c)
            struct.pack_into("<h", st, 0, self.pid)
            struct.pack_into("<II", st, 0x14, self.stats.get("points", 0), self.stats.get("wins", 0))
            self.send(0x07d2, bytes(st), "own stats")
            with _slock:
                counts = (len(lobby_sessions()), len(SESSIONS), 0)
            self.send(0x07f1, struct.pack("<iii", *counts), "lobby counters")
            for i, text in sorted(load_tips().items()):
                self.send(0x07F2, tip_payload(i, text), f"lobby tip {i}")
        return "enter request", reply

    def send_own_header(self):
        """S->C 0x07f0, 0x6c bytes (0x3396a0 -> 0x66ae60): +0x43 own nickname, +0x50 lobby group,
        +0x52 match relay address, +0x64 relay port for the master, +0x68 for members (read by
        0x327580 when the match links through the relay)."""
        hdr = bytearray(0x6c)
        hdr[0x43:0x43 + len(self.name)] = self.name
        if RELAY_PORT:
            relay = (RELAY_HOST or advertise_for(self.peer)).encode("ascii")[:17]
            hdr[0x52:0x52 + len(relay)] = relay
            struct.pack_into("<II", hdr, 0x64, RELAY_PORT, RELAY_PORT)
        self.send(0x07f0, bytes(hdr), "own header (nickname" + (f", relay {hdr[0x52:0x64].rstrip(bytes(1)).decode()}:{RELAY_PORT})" if RELAY_PORT else ")"))

    def on_leave_lobby(self, p):
        # C->S 0x03f2 (u32 0) from the lobby's exit button. S->C 0x03f3 s32 >= 0: the client
        # closes the connection and returns to lobby select (the lobby list stays in memory).
        def reply():
            self.in_lobby = False
            self.send(0x03F3, struct.pack("<i", 0), "left the lobby")
        return "leave lobby", reply

    def on_heartbeat(self, p):
        # C->S 0x0514 (0x337700). S->C 0x0515 (0x337780) sets 0x4e0ed8 = 1, which stretches the
        # heartbeat interval from 20 to 600 frames.
        return "heartbeat", lambda: self.send(0x0515, b"", "heartbeat ack")

    def on_mode(self, p):
        # C->S 0x040c (0x337e40): the lobby's room list request. Sent with 1 on lobby entry
        # (0x34f400) and with 1 or 2 when the list toggle (flag bit 6 of 0x4e5f70, labels 0x37 /
        # 0x38) flips (0x34c6d4 / 0x34c854); what 2 filters is not known, both get every room.
        # S->C 0x040d s32 (0x337ea8) clears the pending 0x040c; 1 or 2 empties the six rows at
        # 0x4e55d8 (0x34aff8). The rows are then filled by 0x07d4 (0x34b508, first free row).
        v = struct.unpack_from("<H", p)[0] if len(p) >= 2 else 1

        def reply():
            self.send(0x040d, struct.pack("<i", v), f"room list {v}")
            with _slock:
                rooms = sorted(ROOMS.values(), key=lambda r: (r.in_game, r.id))[:LOBBY_ROWS]
                rows = [(r.id, r.lobby_row()) for r in rooms]
            for rid, row in rows:
                self.send(0x07d4, row, f"room list row, room {rid}")
        return f"room list request {v}", reply

    def send_room_state(self, room, note):
        """Everything the room scene needs, in the order the client accepts it: the header, every
        slot record, every member's 0x07ee profile (after its record), then 0x07df host, which
        clears token 0x2712, redraws the host slot and opens the host menu (0x34bcf0)."""
        with _slock:
            hdr, host = room.header(), room.host.pid
            recs = [(m.pid, room.record(m), room.profile(m)) for m in room.everyone()]
        self.send(0x07e5, hdr, f"room {room.id} header ({note})")
        for pid, rec, _prof in recs:
            self.send(0x07d7, rec, f"slot record, player {pid}")
        for pid, _rec, prof in recs:
            self.send(0x07ee, prof, f"profile, player {pid}")
        self.send(0x07df, struct.pack("<i", host), f"host = player {host}")

    def on_create_room(self, p):
        # C->S 0x03f4 (0x338078), 62 B: +0x05 room name (cp932; empty -> default at 0x747d20), +0x1e
        # password (checked by 0x337f18), +0x2c u16 mode 2, +0x2e u16 max players 8. S->C 0x03f5 s32
        # (0x338170): >= 0 switches to the scene at *0x66b3ec (the room) and waits on 0x2712; -3 / -2
        # / other negative show messages (0x377, 0x269 / 0x217 / 0x239). Unanswered, the client drops
        # the connection after ~15 s.
        name = cstr_jp(p[0x05:0x1e])
        extra = cstr_jp(p[0x1e:0x2c])
        a, b = struct.unpack_from("<HH", p, 0x2c) if len(p) >= 0x30 else (0, 0)
        with _slock:
            # the lobby shows the id as 3 digits: the lowest free 1..999
            rid = next((i for i in range(1, 1000) if i not in ROOMS), -1)
            room = Room(rid, p, self) if rid > 0 else None
            if room is not None:
                ROOMS[rid] = room
                self.room = room
                self.team = 0
        if room is None:
            # -2 = 0x377:0x217 "Could not create the room." (-3 bad password, other bad name)
            return "create room: no free room id", lambda: self.send(0x03f5, struct.pack("<i", -2), "room not created (-2)")
        note = f"create room name={name!r} password={'set' if extra else 'none'} mode {a} max {b} -> room {room.id}"

        def reply():
            self.send(0x03f5, struct.pack("<i", room.id), f"room created, id {room.id}")
            time.sleep(0.5)  # let the scene switch run first
            self.send_room_state(room, "created")
            to_lobby(0x07d5, room.lobby_row(), f"lobby: room {room.id} added")
        return note, reply

    def on_join_room(self, p):
        # C->S 0x03f6 (0x338210), 62 B: +0 s16 room id, +0x1e password (strcpy). S->C 0x03f7 s32
        # (0x3382c0): >= 0 resets the room tables (0x34b078), enters the room scene and waits on
        # 0x2712 like create. Negative shows (group 0x377): -1 0x1bd "room is being deleted",
        # -2 0x30 "in a game", -3 0x42 "room full", -4 0x1e5 "incorrect password".
        # Members already in the room get 0x07d8 (0x3391f0): the slot record, plus
        # "<name> joined the room." (0x377:0x1ff) when it is not their own id; then 0x07ee.
        rid = struct.unpack_from("<h", p)[0] if len(p) >= 2 else -1
        password = p[0x1e:0x2c].split(b"\0", 1)[0] if len(p) >= 0x1f else b""
        with _slock:
            room = ROOMS.get(rid)
            if room is None or self.room is not None:
                code = -1
            elif room.in_game:
                code = -2
            elif room.free_slot() is None:
                code = -3
            elif room.password and password != room.password:
                code = -4
            else:
                code = rid
                room.members[room.free_slot()] = self
                self.room = room
                self.team = 0
                others = [m for m in room.everyone() if m is not self]
        note = f"join room {rid} -> {code}"

        def reply():
            self.send(0x03f7, struct.pack("<i", code), f"join room {rid}: {code}")
            if code < 0:
                return
            time.sleep(0.5)  # let the scene switch run first, as after create
            self.send_room_state(room, "joined")
            with _slock:
                mine, prof, n = room.record(self), room.profile(self), len(room.members)
            self.send_all(others, 0x07d8, mine, f"player {self.pid} joined room {rid}")
            self.send_all(others, 0x07ee, prof, f"profile, player {self.pid}")
            to_lobby(0x07dd, room.lobby_row(), f"lobby: room {rid} now {n} members")
        return note, reply

    def leave_room(self, why):
        """Drop this session from its room (0x03f8 or a closed connection) and tell the rest:
        0x07da {s32 id, s8 reason} first when a match is running (0x36a818 takes the player out of
        the match), then 0x07d9 s32 player id (0x3392b8) shows "<name> left the room." (0x377:0xf5)
        and frees the slot (0x34ba60). A new host gets fresh slot records (+0x30 = master id) and
        0x07df. An empty room is removed from the lobby rows (0x07d6)."""
        with _slock:
            room = self.room
            if room is None:
                return
            self.room = None
            del room.members[room.slot_of(self)]
            rest = room.everyone()
            in_game = room.in_game
            new_host = bool(rest) and room.host is self
            if not rest:
                ROOMS.pop(room.id, None)
            elif new_host:
                room.host = rest[0]
            if len(rest) < 2:
                room.in_game = False  # the clients return to the room on their own
            host = room.host
            recs = [(m.pid, room.record(m)) for m in rest]
            row = room.lobby_row()
            room.setup_evt.set()  # a start waiting on this player's 0x466 stops waiting
        if not rest:
            to_lobby(0x07d6, struct.pack("<i", room.id), f"lobby: room {room.id} removed ({why})")
            return
        if in_game:
            # reason: 1 seen; 2 for the master raises net error 0x1f5 (0x30fca0). Others unknown.
            self.send_all(rest, 0x07da, struct.pack("<ib", self.pid, 1), f"player {self.pid} out of the match")
        self.send_all(rest, 0x07d9, struct.pack("<i", self.pid), f"player {self.pid} left room {room.id} ({why})")
        if new_host:
            for pid, rec in recs:
                self.send_all(rest, 0x07d7, rec, f"slot record, player {pid} (host {host.pid})")
            self.send_all(rest, 0x07df, struct.pack("<i", host.pid), f"host = player {host.pid}")
        to_lobby(0x07dd, row, f"lobby: room {room.id} now {len(rest)} members")

    def on_save_block(self, cmd, p):
        # C->S 0x043a / 0x043e / 0x0442 / 0x0446 save one profile block (see PROFILE_BLOCKS). The
        # caller (e.g. 0x35e768, 0x361d0c) sets 0x66b6fc = -1, sends, and loops until 0x66b6fc
        # equals its own command; the ack (cmd + 1, 0x337948 / 0x3379d0 / 0x337a58 / 0x337ae0)
        # writes it only when nonzero (0x34beb0). So the ack carries the request's command: a 0
        # reply left the client waiting until the 15 s timeout.
        name = SAVE_BLOCKS[cmd]
        prof = load_profile(self.inq_id)
        prof[name] = bytes(p).hex()
        save_profile(self.inq_id, prof)
        note = f"save {name} {p[:16].hex(' ')}" + (f" text={cstr_jp(p[0x10:0x30])!r}" if name == "chat" else "")
        return note, lambda: self.send(cmd + 1, struct.pack("<i", cmd), f"{name} saved (0x{cmd:04x})")

    def on_record(self, p):
        # C->S 0x0419 (0x337b20) u32: the first rank of the page (1 on open, +-10 when paging).
        # S->C 0x041a (0x337b70 -> 0x34bed0): unless its first u32 is -1 ("past the end", nothing
        # is copied), 0xf0 bytes go to 0x66b5f0: 10 rows of 0x18 B, drawn by 0x367340:
        #   +0x00 s32 rank (row hidden unless > 0)   +0x04 name (char[14])
        #   +0x12 s16 icon (sprite 0x25 - value; meaning unknown, 0 sent)   +0x14 s32 points
        start = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 1
        start = max(1, start)
        rows = ranking()
        page = bytearray(0xF0)
        if rows and start > len(rows):
            struct.pack_into("<i", page, 0, -1)
            note = f"ranking from {start}: past the end"
        else:
            for k, (inq, rec) in enumerate(rows[start - 1:start + 9]):
                o = k * 0x18
                struct.pack_into("<i", page, o, start + k)
                name = account_name(inq)[:13]
                page[o + 4:o + 4 + len(name)] = name
                struct.pack_into("<i", page, o + 0x14, rec["points"])
            note = f"ranking from {start}: {max(0, min(10, len(rows) - start + 1))} rows"
        return f"ranking request {start}", lambda: self.send(0x041A, bytes(page), note)

    def on_rank_search(self, p):
        # C->S 0x041b (0x337ba0): a NUL-terminated nickname (own one from 0x07f0 +0x43, or typed).
        # S->C 0x041c s32 (0x337c18 -> 0x34bf18): the rank (the client then asks for its page);
        # <= 0 shows "no such user / 0-point users are not ranked". -2 is the client's own
        # "still waiting" value, never sent.
        name = p.split(b"\0", 1)[0]
        rank = next((i + 1 for i, (inq, _r) in enumerate(ranking()) if account_name(inq) == name), 0)
        return f"ranking search {cstr_jp(name)!r}", lambda: self.send(0x041C, struct.pack("<i", rank), f"rank {rank}")

    def on_find(self, p):
        # C->S 0x040a (0x337c50): a NUL-terminated nickname from the find panel (0x363b3c).
        # S->C 0x040b, 0x1c bytes -> 0x66b6e0 (0x337cc8):
        #   +0x00 s16 status: -9 "not a registered nickname", other < 0 "%s is not connected",
        #         0..9998 "in room %03d" (+0x10 >= 0) or "in the lobby" (+0x10 < 0), >= 9999 looks up
        #         the lobby whose key (entry +0x00) equals +0x12 and offers a jump to it (not used)
        #   +0x02 name echo   +0x0c s16 lobby index   +0x10 s16 room id   +0x12 lobby key
        name = p.split(b"\0", 1)[0]
        inq = find_account(name)
        with _slock:
            who = next((t for t in SESSIONS if inq and t.inq_id == inq and (t.in_lobby or t.room)), None)
            room = who.room.id if who and who.room else -1
        out = bytearray(0x1c)
        out[2:2 + len(cut_cp932(name, 9))] = cut_cp932(name, 9)
        struct.pack_into("<h", out, 0x10, room)
        status = -9 if inq is None else -1 if who is None else 0
        struct.pack_into("<h", out, 0, status)
        return f"find {cstr_jp(name)!r}", lambda: self.send(0x040B, bytes(out), f"find: status {status} room {room}")

    def on_slot_toggle(self, p):
        # C->S 0x040e (0x338778 pending; sent by 0x345c68) u32 slot, only from the host and only on
        # an empty slot. S->C 0x040f s32 only plays a sound (>= 0 ok, < 0 error beep). What the
        # slot shows comes from header byte +0x36+slot, so flip it and push the header.
        slot = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 99
        with _slock:
            room = self.room
            ok = (room is not None and room.host is self and 0 <= slot < ROOM_MAX
                  and slot not in room.members and not room.in_game)
            if ok:
                room.hdr[0x36 + slot] ^= 1
                members, hdr, row = room.everyone(), room.header(), room.lobby_row()

        def reply():
            self.send(0x040F, struct.pack("<i", 0 if ok else -1), "slot toggle " + ("ok" if ok else "refused"))
            if ok:
                self.send_all(members, 0x07e5, hdr, f"room header (slot {slot} {'closed' if hdr[0x36 + slot] else 'open'})")
                to_lobby(0x07dd, row, f"lobby: room {room.id} updated")
        return f"slot toggle {slot}", reply

    def on_plain_ack(self, cmd, p):
        # Requests whose reply handler only clears the pending request and closes the waiting
        # dialog (0x33e2d0) without reading a payload: 0x041d -> 0x041e (0x3385f8),
        # 0x041f -> 0x0420 (0x338660), 0x0402 -> 0x0403 (0x338558). The change itself reaches the
        # room (the sender too) as a server update:
        #   0x041d s16 x3 (0x338578, from the rules 0x4e3100: wins, time in s, third) -> header
        #          +0x30 / +0x32 / +0x34, header 0x07e5 to every member.
        #   0x041f u32 (0x338618, menu 0x340c50 sends 0 / 1 / 2) = game mode -> header +0x2c (the
        #          lobby row draws the same three mode icons from it; create sends 2), 0x07e5.
        #   0x0402 (own id, 0, map, team) "set my slot": the team menu (0x340da8) sends (current
        #          map, 0..3), the map menus (map, own team). Map -> record +0x2c, from the host
        #          only (0x34b9c4 copies the host's into all records); team -> the sender's +0x2e.
        #          The sender's record goes to every member.
        def reply():
            self.send(PLAIN_ACKS[cmd], struct.pack("<i", 0), "ack")
            with _slock:
                room = self.room
                if room is None:
                    return
                if cmd == 0x0402 and len(p) >= 8:
                    stage, team = struct.unpack_from("<hh", p, 4)
                    if room.host is self:
                        room.stage = stage
                    self.team = team if 0 <= team <= 3 else 0
                    upd = (0x07d7, room.record(self), f"slot update, map {room.stage} team {self.team}")
                elif cmd == 0x041D and len(p) >= 6:
                    room.hdr[0x30:0x36] = p[:6]
                    upd = (0x07e5, room.header(), "room header (rules %d/%d/%d)" % struct.unpack_from("<hhh", p))
                elif cmd == 0x041F and len(p) >= 4:
                    struct.pack_into("<h", room.hdr, 0x2c, struct.unpack_from("<i", p)[0])
                    upd = (0x07e5, room.header(), f"room header (mode {room.hdr[0x2c]})")
                else:
                    return
                members, row = room.everyone(), room.lobby_row()
            self.send_all(members, *upd)
            if cmd != 0x0402:
                to_lobby(0x07dd, row, f"lobby: room {room.id} updated")
        return f"request {p.hex(' ')}", reply

    def on_chat(self, p):
        # Room chat: C->S 0x03fe (0x3389b0, room builder 0x346160) -> S->C 0x03ff (0x338a00, drawn
        # by the room log 0x347468). Lobby chat: C->S 0x03f0 (0x3388e8, lobby builder 0x34d524) ->
        # S->C 0x03f1 (0x338938, lobby log 0x34f000). Text is cp932, NUL-terminated, no pending
        # wait; CR/LF are stripped (0x338838); a leading 0x01 byte draws the line as a system line
        # (colour 0xb). The client adds no speaker name, so the server does, and lines longer than
        # 0x35 bytes are cut by the client (0x336060), so long ones go out in pieces.
        text = p.split(bytes(1), 1)[0]

        def reply():
            with _slock:
                if self.room:
                    cmd, targets = 0x03FF, self.room.everyone()
                else:
                    cmd, targets = 0x03F1, lobby_sessions()
            for line in chat_lines(self.name, text):
                self.send_all(targets, cmd, line, "chat relay")
        return f"chat {text.decode('cp932', 'replace')!r}", reply

    def on_start(self, p):
        # C->S 0x03fa (0x338680) when start is pressed: s16 own id, 0, two s16 (seen 0, 0).
        # S->C 0x03fb (0x3386f0): clears the pending 0x03fa; only the host then waits on token
        # 0x7db, cleared by one of
        #   0x07e4 (0x339448) s32 -1..-4: start refused, message group 0x377: -1 0x15b "can't start
        #          alone", -2 0x18a "can't start with one team", -3 0xbb "team sizes differ",
        #          -4 0x6c "not everyone is READY".
        #   0x07dc (0x339418) to every member: go -- builds the match tables from the slot records
        #          (0x336718), answers C->S 0x044c sub 0x466 ("setup done"), waits on token 0x7dc
        #          (15 s).
        # then S->C 0x044c sub 0x467 to every member (same seed at +0x10) clears 0x7dc and the
        # clients switch to the stage themselves (0x30ef88 needs >= 2 entries in 0x6521a8; with 1
        # it sends the end-of-round report sub 0x476 and stays in the room).
        # A guest's 0x03fa is probably the READY toggle (its field is not identified): it gets
        # 0x03fb only. The team checks follow the mode texts; READY (-4) is not enforced.
        with _slock:
            room = self.room
            members = room.everyone() if room else []
            host = room is not None and room.host is self
            code = 0
            if host:
                teams = {}
                for m in members:
                    teams[m.team] = teams.get(m.team, 0) + 1
                if len(members) < 2:
                    code = -1
                elif room.mode() in (0, 1) and len(teams) < 2:
                    code = -2
                elif room.mode() == 0 and len(set(teams.values())) > 1:
                    code = -3
                elif room.in_game:
                    code = -1
            if host and code == 0:
                room.in_game = True
                room.wins = {}
                room.round = 0

        def reply():
            self.send(0x03FB, struct.pack("<i", 0), "start ack")
            if not host:
                return
            if code:
                self.send(0x07E4, struct.pack("<i", code), f"start refused ({len(members)} in room, {code})")
                return
            to_lobby(0x07dd, room.lobby_row(), f"lobby: room {room.id} in a game")
            self.begin_round(room, members, first=True)
        return f"start {p.hex(' ')} ({len(members)} in room{', guest' if not host else ''})", reply

    def begin_round(self, room, members, first):
        """0x07dc (first round only) and then, once every member's sub 0x466 is in, sub 0x467 with
        one shared seed. Later rounds start from 0x469: the clients show the results for ~8 s,
        then send 0x466 again."""
        with _slock:
            room.setup_done = set()
            room.setup_evt = threading.Event()
        if first:
            for m in members:  # the relay address must be in place before 0x467 (sent at lobby entry too)
                try:
                    m.send_own_header()
                except OSError:
                    pass
            self.send_all(members, 0x07DC, struct.pack("<i", 0), "start go")
        # The members' sub 0x466 arrive on their own session threads (this one included), so
        # the wait for them runs on its own thread.
        threading.Thread(target=self.start_match, args=(room, members), daemon=True).start()

    def start_match(self, room, members):
        done = room.setup_evt.wait(10)  # the clients give up on token 0x7dc after 15 s
        with _slock:
            members = [m for m in members if m.room is room]
            missing = [m.pid for m in members if m.pid not in room.setup_done]
            room.round += 1
            room.reports, room.ends = {}, {}
            if room.round_timer:
                room.round_timer.cancel()
            room.round_timer = None
            limit = struct.unpack_from("<h", room.hdr, 0x32)[0] or 180
        # Sub 0x474 (0x339080 -> 0x389e58): +0x0a s16 round time in seconds. Online it is the only
        # source of the limit (offline sets 120 at 0x30fb44); the round setup copies it at
        # 0x389510. Never sent, every round began at 0:00: HURRY UP at once, then time up.
        tl = bytearray(0x20)
        struct.pack_into("<h", tl, 0, 0x474)
        struct.pack_into("<h", tl, 0x0a, limit)
        self.send_all(members, 0x044C, bytes(tl), f"round time {limit} s (sub 0x474)")
        seed = random.getrandbits(31)
        go = bytearray(0x20)
        struct.pack_into("<h", go, 0, 0x467)
        struct.pack_into("<I", go, 0x10, seed)
        note = f"round {room.round} start (sub 0x467)" + ("" if done and not missing else f", no setup report from {missing}")
        self.send_all(members, 0x044C, bytes(go), note)

    def finish_round(self, room):
        """Decide the round from the master's reports and answer every member: one sub 0x468 per
        player, then sub 0x469 (next round) or 0x46a (match over). The client waits ~20 s for
        this in its result states (0x4b1 frames, then 0x3c1:0x0280 and back to login).
          0x468 (0x338c88): +4 s16 player id, +0x0a won this round (-> entry+0xd4), +0x0c ?
                (-> +0xd0), +0x0e points gained (-> +0xd8), +0x10 u32 points total (-> +0x14);
                the result row shows +0x0e and +0x10 - +0x0e (0x377e00). Sets entry+0x1a4: rows
                without a 0x468 are not shown.
          0x469 (0x338f48): +0x0a 0, +0x0c map, +0x0e variant -> next round.
          0x46a (0x338fe0): match over; ~16 s later every client sends 0x0400 and returns to
                the room on its own (0x3777c4 -> 0x30fa10)."""
        with _slock:
            if room.round_timer:
                room.round_timer.cancel()
            room.round_timer = None
            if not room.in_game or (not room.reports and not room.ends):
                return
            members = [m for m in room.everyone() if m.room is room]
            pids = {m.pid for m in members}
            master = room.host.pid
            reports = room.reports.get(master) or next((r for r in room.reports.values() if r), {})
            code = room.ends.get(master, 0)
            # 0x453 names only the players who are out (0x373250: entry state 7 -> +0x0a 0, 8 -> 1,
            # not present -> 1); the living are never reported. Read as "0 = standing", a win with
            # two players out came out a draw.
            alive = [pid for pid in pids if pid not in reports]
            room.reports, room.ends = {}, {}
            # 0x476 +0x0a from the master (0x36eb40): 1 = a winner, 2 = everyone down, 3 = time up.
            if code == 1 or (code == 0 and len(alive) == 1):
                if room.mode() in (0, 1):
                    teams = {m.team for m in members if m.pid in alive}
                    winners = [m.pid for m in members if m.team in teams] if len(teams) == 1 else []
                else:
                    winners = alive if len(alive) == 1 else []
            else:
                winners = []
            for pid in winners:
                room.wins[pid] = room.wins.get(pid, 0) + 1
            over = any(w >= room.wins_needed() for w in room.wins.values())
            if over:
                room.in_game = False
                live.record_match(room, members, room.round)
            stage = room.stage
        results = []
        for m in members:
            won = m.pid in winners

            def score(rec, _allp, won=won):
                rec["rounds"] = rec.get("rounds", 0) + 1
                if won:
                    rec["wins"] = rec.get("wins", 0) + 1
                    rec["points"] = rec.get("points", 0) + ROUND_POINTS
            m.stats = player_info(m.inq_id, score)
            row = bytearray(0x20)
            struct.pack_into("<h", row, 0, 0x468)
            struct.pack_into("<h", row, 4, m.pid)
            struct.pack_into("<hhhI", row, 0x0a, 1 if won else 0, 0, ROUND_POINTS if won else 0,
                             m.stats.get("points", 0))
            results.append(bytes(row))
        for row in results:
            self.send_all(members, 0x044C, row, f"round result, player {struct.unpack_from('<h', row, 4)[0]}")
        nxt = bytearray(0x20)
        if over:
            struct.pack_into("<h", nxt, 0, 0x46a)
            self.send_all(members, 0x044C, bytes(nxt), f"match over (round {room.round}, winners {winners})")
            with _slock:
                row = room.lobby_row()
            to_lobby(0x07dd, row, f"lobby: room {room.id} back from a game")
        else:
            struct.pack_into("<hxxxxxxxxhh", nxt, 0, 0x469, 0, stage)
            self.send_all(members, 0x044C, bytes(nxt), f"next round (round {room.round}, winners {winners})")
            self.begin_round(room, members, first=False)

    def on_leave_room(self, p):
        # C->S 0x03f8 (0x3383f8, u32: 3 from the room, 1 when quitting a match) when leaving a
        # room; the client empties its lobby rows at once (0x34aff8). S->C 0x03f9 s32 (0x338448):
        # clears the pending 0x03f8; >= 0 (and 0x4e0edc set) returns to the lobby scene *0x66b3e8,
        # which asks for the room list again (0x040c). Unanswered it timed out.
        rid = self.room.id if self.room else None

        def reply():
            self.leave_room("left")
            self.send(0x03F9, struct.pack("<i", 0), "left room")
        return f"leave room {rid} ({p.hex(' ')})", reply

    def on_back_to_room(self, p):
        # C->S 0x0400, empty, no reply awaited: sent from the post-match menu (0x30f3e8) or at the
        # end of the result screens (0x3767b0); the client switches to the room scene itself.
        # S->C 0x0401 (0x3384d0) only tears the match down (0x30f970): never send it mid-match.
        # Refresh the room for the returning player.
        room = self.room

        def reply():
            if room is not None:
                self.send_room_state(room, "back from the match")
        return "back to the room", reply

    def on_match(self, p):
        # C->S 0x044c in a match, 20 B: s16 sub at +0, s16 player id at +4. Subs:
        #   0x466 setup done (end of 0x336718, after every 0x07dc and every 0x469).
        #   0x475 the master's round went live (state 6 -> 7, 0x30f740): no reply.
        #   0x453 (0x338ad4, from 0x373250 at round end): +4 a player who is OUT; +0x0a 0 = entry
        #         state 7, 1 = state 8 or not present (0x30fc04). The living are not reported.
        #   0x476 (0x338b44, after the 0x453s): +4 own id, +0x0a the master's round code
        #         (0x36eb40: 1 winner, 2 all down, 3 time up); non-masters send 0.
        # 0x466 / 0x475 send no payload beyond +4; +8.. are stale buffer bytes (0x338bc0).
        sub = struct.unpack_from("<h", p)[0] if len(p) >= 2 else -1
        about = struct.unpack_from("<h", p, 4)[0] if len(p) >= 6 else -1
        flag = struct.unpack_from("<h", p, 0x0a)[0] if len(p) >= 0x0c else 0
        decide = False
        with _slock:
            room = self.room
            if room is not None and sub == 0x466:
                room.setup_done.add(self.pid)
                if all(m.pid in room.setup_done for m in room.everyone()):
                    room.setup_evt.set()
            elif room is not None and room.in_game and sub == 0x453:
                room.reports.setdefault(self.pid, {})[about] = flag
            elif room is not None and room.in_game and sub == 0x476:
                room.ends[self.pid] = flag
                if self is room.host or all(m.pid in room.ends for m in room.everyone()):
                    decide = True
                elif room.round_timer is None:
                    # the master's report may never come (it dropped): decide on what is there
                    room.round_timer = threading.Timer(ROUND_WAIT, self.finish_round, args=(room,))
                    room.round_timer.daemon = True
                    room.round_timer.start()
        reply = (lambda: self.finish_round(room)) if decide else (lambda: None)
        return f"match sub 0x{sub & 0xffff:03x} {p[4:].hex(' ')}", reply

    def on_keepalive(self, p):
        # C->S 0x05dd, empty, every 601 frames while connected (0x33a258); no reply handler.
        return "keepalive", lambda: None

    def handle(self, cmd, payload):
        handler = {0x0404: self.on_login, 0x0412: self.on_register, 0x03ea: self.on_lobby_login,
                   0x0438: self.on_lobby_info, 0x03ee: self.on_enter, 0x03f2: self.on_leave_lobby,
                   0x0514: self.on_heartbeat, 0x040c: self.on_mode, 0x0419: self.on_record,
                   0x041b: self.on_rank_search, 0x040a: self.on_find, 0x040e: self.on_slot_toggle,
                   0x03f0: self.on_chat, 0x03fe: self.on_chat, 0x03fa: self.on_start,
                   0x03f8: self.on_leave_room, 0x0400: self.on_back_to_room, 0x044c: self.on_match,
                   0x03f4: self.on_create_room, 0x03f6: self.on_join_room, 0x05dd: self.on_keepalive,
                   **{c: (lambda p, c=c: self.on_save_block(c, p)) for c in SAVE_BLOCKS},
                   **{c: (lambda p, c=c: self.on_plain_ack(c, p)) for c in PLAIN_ACKS}}.get(cmd)
        if handler is None:
            emit(self.peer, "C->S", cmd, payload, "(unhandled)")
            return
        note, reply = handler(payload)
        emit(self.peer, "C->S", cmd, payload, note)
        reply()

    def run(self):
        buf = b""
        emit(self.peer, "---", 0, b"", "open")
        with _slock:
            SESSIONS.add(self)
        try:
            while True:
                chunk = self.conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    cmd, n = struct.unpack_from("<HH", buf)
                    if len(buf) < 4 + n:
                        break
                    payload, buf = buf[4:4 + n], buf[4 + n:]
                    self.handle(cmd, payload)
        except OSError as ex:
            emit(self.peer, "---", 0, b"", f"error {ex}")
        finally:
            self.leave_room("disconnected")
            with _slock:
                SESSIONS.discard(self)
            if buf:
                emit(self.peer, "C->S", 0, buf, "(trailing bytes, incomplete frame)")
            self.conn.close()
            emit(self.peer, "---", 0, b"", "close")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=10020)
    a = ap.parse_args(argv)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.bind, a.port))
    s.listen(16)
    threading.Thread(target=udp_logger, args=(a.bind, a.port), daemon=True).start()
    live.start(sys.modules[__name__])   # the boards' snapshot (live.py); this module, not a copy
    admin.serve(admin_request, log=lambda msg: print(msg, flush=True))  # admin pushes (admin.py)
    adminport.start(sys.modules[__name__], log=lambda msg: print(msg, flush=True))  # an OpenLobby panel
    if RELAY_PORT:
        from . import relay
        threading.Thread(target=relay.serve, args=(a.bind, RELAY_PORT,
                                                    lambda msg: emit("relay", "---", 0, b"", msg)),
                         daemon=True).start()
    print(f"bomb.game on {a.bind}:{a.port} (tcp + udp log), match mode {MATCH_MODE}"
          f"{f' via relay :{RELAY_PORT}' if RELAY_PORT else ''}, "
          f"nickname registration {'on' if REQUIRE_NICKNAME else 'off'}, logs {LOG_DIR}", flush=True)
    while True:
        conn, addr = s.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=Session(conn, f"{addr[0]}:{addr[1]}").run, daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
