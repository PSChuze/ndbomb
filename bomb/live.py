"""live.py -- the game server's public snapshot, for a status board or bot (and the admin port).

The board runs in its own container with ONLY $BOMB_DATA_DIR/board/ mounted read-only: it never
connects to the game server (a board bug can never touch a match) and never sees players.json,
profiles.json or accounts.json beside it. So everything the board draws is published here, and
nothing else: display names with their public ranking numbers, who is in the lobby, the rooms, and
the last few matches.

Shape (version 1), written atomically to BOMB_LIVE_FILE (default $BOMB_DATA_DIR/board/live.json)
every BOMB_LIVE_EVERY seconds (10; 0 = off):
  {"v": 1, "game": "bomb", "updated": unix, "online": N,
   "players": [{"name", "where": "lobby" | "room" | "match", "char"(, "look")}],
   "rooms": [{"id", "name", "players": [names], "max", "mode", "locked", "in_game", "wins"}],
   "matches": [{"n", "at", "room", "mode", "rounds", "winners": [names],
                "players": [{"name", "wins", "char"(, "look")}]}],  # newest first
   "standings": {"points" | "wins" | "rounds" | "rate": [{"place", "name", "value", "wins",
                                                          "rounds", "char"(, "look")}]},
                                                                        # top STANDINGS_ROWS
   "totals": {"players", "rounds"}}
  mode: "teams" (0 / 1) or "free" (2), as the lobby icon shows it. rate is the round win %
  (1 decimal) of players with at least RATE_MIN_ROUNDS rounds. n counts matches since the
  server started (two can share a second).
  char: the player's Bomberman, profile block "a" +8 (s16): 0..7 = the preset (sprite 0x3e4 + n:
  white, black, red, blue, green, yellow, aqua, pink), -1 = a custom look, then also "look": the
  "look" block as 4 [r, g, b] (0..31), in the customize screen's part order (head, body, arms,
  hands and feet; the order is read off the manual, not traced in code). A player who never
  saved block "a" is preset 0, as the client treats the zero default. Added. For a match, the
  look at the time it ended.
"""
import json
import os
import struct
import threading
import time

VERSION = 1
MATCHES_KEPT = 20
STANDINGS_ROWS = 25
RATE_MIN_ROUNDS = 10
STANDINGS = ("points", "wins", "rounds", "rate")

_matches = []           # newest first; guarded by _mlock
_seq = [0]
_mlock = threading.Lock()


def path():
    from . import game
    return os.environ.get("BOMB_LIVE_FILE") or os.path.join(game.DATA_DIR, "board", "live.json")


def _text(raw):
    return raw.split(b"\0", 1)[0].decode("cp932", "replace") or "?"


def _names():
    """{inq: display name} for every account on file, read without creating records (player_info
    would add one for an unknown id)."""
    from . import game
    from . import feegalink
    players = game._load_players()
    auth = feegalink.names()
    out = {}
    for inq, rec in players.items():
        name = rec.get("nick") or auth.get(inq, "")
        raw = game.cut_cp932(name.encode("cp932", "replace"), game.NAME_MAX) or game.CHAT_NAME
        out[inq] = _text(raw)
    return players, out


def char_of(a_hex, look_hex):
    """{"char": n} (0..7) or {"char": -1, "look": [[r, g, b] x 4]} from the stored "a" and
    "look" blocks (hex; empty = the zero default)."""
    try:
        a = bytes.fromhex(a_hex or "")
        look = bytes.fromhex(look_hex or "")
    except ValueError:
        a, look = b"", b""
    ch = struct.unpack_from("<h", (a + bytes(10))[:10], 8)[0]
    if 0 <= ch <= 7:
        return {"char": ch}
    look = (look + bytes(12))[:12]
    return {"char": -1, "look": [[min(31, v) for v in look[i:i + 3]] for i in range(0, 12, 3)]}


def _profiles():
    """Every stored profile, read once (profiles.json; nothing created)."""
    from . import game
    with game._plock:
        try:
            with open(game._profiles_path(), encoding="utf-8") as f:
                allp = json.load(f)
        except (OSError, ValueError):
            allp = {}
    return allp if isinstance(allp, dict) else {}


def _char(profiles, inq):
    prof = profiles.get(inq or "") or {}
    return char_of(prof.get("a", ""), prof.get("look", ""))


def _session_name(s):
    return _text(s.name) if isinstance(s.name, bytes) else str(s.name or "?")


def _mode(room):
    return "free" if room.mode() not in (0, 1) else "teams"


def record_match(room, members, rounds):
    """Remember one finished match: call with game._slock held, when finish_round ends it. room.wins
    holds each player's rounds won."""
    profiles = _profiles()
    players = [dict({"name": _session_name(m), "wins": int(room.wins.get(m.pid, 0))},
                    **_char(profiles, m.inq_id)) for m in members]
    players.sort(key=lambda p: -p["wins"])
    top = max([p["wins"] for p in players] or [0])
    rec = {"at": int(time.time()), "room": _text(bytes(room.hdr[0x05:0x1e])), "mode": _mode(room),
           "rounds": int(rounds), "winners": [p["name"] for p in players if top and p["wins"] == top],
           "players": players}
    with _mlock:
        _seq[0] += 1
        rec["n"] = _seq[0]
        _matches.insert(0, rec)
        del _matches[MATCHES_KEPT:]


def standings(players, names, profiles=None):
    profiles = {} if profiles is None else profiles
    rows = [{"name": names.get(inq, "?"), "pid": int(r.get("pid", 0)), "points": int(r.get("points", 0)),
             "wins": int(r.get("wins", 0)), "rounds": int(r.get("rounds", 0)), "inq": inq}
            for inq, r in players.items()]
    keys = {"points": lambda r: (r["points"],), "wins": lambda r: (r["wins"],),
            "rounds": lambda r: (r["rounds"],),
            "rate": lambda r: (r["wins"] * 1000 // r["rounds"] if r["rounds"] else 0, r["rounds"])}
    out = {}
    for cat in STANDINGS:
        key = keys[cat]
        pool = [r for r in rows if r["rounds"] > 0 and (cat == "rate" or key(r)[0] > 0)]
        if cat == "rate":
            pool = [r for r in pool if r["rounds"] >= RATE_MIN_ROUNDS]
        pool.sort(key=lambda r: (key(r), -r["pid"]), reverse=True)
        top, place, prev = [], 0, None
        for n, r in enumerate(pool[:STANDINGS_ROWS], 1):
            if key(r) != prev:
                place, prev = n, key(r)
            value = round(r["wins"] * 100.0 / r["rounds"], 1) if cat == "rate" else key(r)[0]
            top.append(dict({"place": place, "name": r["name"], "value": value, "wins": r["wins"],
                             "rounds": r["rounds"]}, **_char(profiles, r["inq"])))
        out[cat] = top
    return out


#: the RUNNING game module, handed over by start(). The server runs as `python -m bomb.game`, so
#: it is __main__; `from . import game` would load a second, empty copy (no sessions, no rooms)
#: and the board would always say nobody is online.
_GAME = {"mod": None}


def snapshot(now=None):
    game = _GAME["mod"]
    if game is None:
        from . import game
    players, names = _names()
    profiles = _profiles()
    with game._slock:
        here = [s for s in game.SESSIONS if s.in_lobby or s.room is not None]
        who = [dict({"name": _session_name(s),
                     "where": "match" if s.room is not None and s.room.in_game
                     else "room" if s.room is not None else "lobby"}, **_char(profiles, s.inq_id))
               for s in here]
        rooms = [{"id": r.id, "name": _text(bytes(r.hdr[0x05:0x1e])),
                  "players": [_session_name(m) for m in r.everyone()], "max": r.max, "mode": _mode(r),
                  "locked": bool(r.password), "in_game": bool(r.in_game), "wins": r.wins_needed()}
                 for r in sorted(game.ROOMS.values(), key=lambda r: r.id)]
    who.sort(key=lambda p: p["name"])
    with _mlock:
        matches = list(_matches)
    return {"v": VERSION, "game": "bomb", "updated": int(time.time() if now is None else now),
            "online": len(who), "players": who, "rooms": rooms, "matches": matches,
            "standings": standings(players, names, profiles),
            "totals": {"players": sum(1 for r in players.values() if r.get("rounds", 0) > 0),
                       "rounds": sum(int(r.get("rounds", 0)) for r in players.values())}}


def write(dest=None, snap=None):
    dest = dest or path()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot() if snap is None else snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, dest)


def start(game_mod=None):
    """Publish every BOMB_LIVE_EVERY seconds on a daemon thread (game.main starts it, passing
    itself: see _GAME)."""
    if game_mod is not None:
        _GAME["mod"] = game_mod
    every = float(os.environ.get("BOMB_LIVE_EVERY", "10"))
    if every <= 0:
        return None
    dest = path()

    def loop():
        while True:
            try:
                write(dest)
            except Exception as e:                     # noqa: BLE001 never take the server down
                print(f"live: could not write {dest}: {e!r}", flush=True)
            time.sleep(every)

    t = threading.Thread(target=loop, name="live", daemon=True)
    t.start()
    print(f"live snapshot -> {dest} every {every:g}s", flush=True)
    return t
