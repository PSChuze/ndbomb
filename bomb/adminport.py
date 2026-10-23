"""adminport.py -- this server in an OpenLobby admin panel (the panel's admin protocol, olport.py).

Off unless BOMB_ADMIN_PORT is set. The panel is then given this address and the token:

  BOMB_ADMIN_PORT     the HTTP port, e.g. 24090
  BOMB_ADMIN_BIND     default 127.0.0.1; the panel's container reaches a host-networked server
                      through the Docker gateway, so live binds 0.0.0.0 (a token is then required)
  BOMB_ADMIN_TOKEN    the token the panel sends (given to the panel at pairing, or as
                      POL_ADMIN_GAME_BOMB_TOKEN on the panel's side)
  BOMB_ADMIN_CONTAINERS  the containers its server check covers (comma list), default this
                      compose file's (project "bomb")
  BOMB_UPDATE_REPO    the GitHub repository whose releases the update notice compares this
                      build with, default PSChuze/ndbomb; "off" turns the check off (updates.py)

What the panel gets: who is online and where, the rooms, and the admin's commands from admin.py
(announce, a chat line, the conduct warning, a lobby tip), each run through game.admin_request
exactly as `python -m bomb.admin` runs them. The panel's maintenance warning uses Announce.

Also:
  Game help       the BOMBERMAN WEB > Support tickets (support.py, $BOMB_DATA_DIR/support.json) in
                  the panel's Inbox. A reply is stored on the ticket the way `python -m bomb.support
                  reply` stores it, signed "Support", and the player reads it under Support Replies
                  the next time the game fetches the mail list. It does not reach a chat bot's
                  thread for the ticket.
  Rankings        a tool page: the standings the boards show (live.snapshot).
  Recent matches  a tool page: the last live.MATCHES_KEPT matches since the server started (kept in
                  memory only).
  Lobby tips      a tool page: the 13 tips with the admin's replacements, set or cleared there.

  Player lookup   @port.player: the account window's feega logins. The website links a login to a
                  website account by its key, the CommonInqID (the Feega service's verify, after
                  the password check), and the panel sends those keys as links["feega"]. Each one this
                  game has a players.json record for is one Bomberman: its name, whether it is on,
                  its ranking numbers and its look. A feega login that only ever played another game
                  has no record here and is left out. Handle names are never matched: a handle whose
                  name matches a feega-ID need not be the same person.
"""
import calendar
import json
import os
import time

from . import admin, live, olpage, olport, support, updates
from .olpage import esc, form, page, panel, posted, table

PORT = int(os.environ.get("BOMB_ADMIN_PORT", "0") or 0)
BIND = os.environ.get("BOMB_ADMIN_BIND", "127.0.0.1")
TOKEN = os.environ.get("BOMB_ADMIN_TOKEN", "")
CONTAINERS = [c.strip() for c in os.environ.get(
    "BOMB_ADMIN_CONTAINERS", "bomb-game-1,bomb-ticket-1,bomb-web-1").split(",")
    if c.strip()]


def _said(out):
    """game.admin_request's answer -> (ok, message for the panel)."""
    if not out.get("ok"):
        return False, out.get("error") or "refused"
    n = out.get("sent_to")
    return True, "Sent to %d player%s." % (n, "" if n == 1 else "s") if n is not None else "Done."


def _at(iso):
    """support.py's "%Y-%m-%dT%H:%M:%SZ" as unix seconds (None if unreadable)."""
    try:
        return calendar.timegm(time.strptime(iso or "", "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else ""


def _ticket(tid):
    """(number, ticket) for the panel's id, (None, None) when there is no such ticket."""
    try:
        n = int(str(tid).strip().lstrip("#"))
    except ValueError:
        return None, None
    return n, next((t for t in support.api_tickets() if t.get("id") == n), None)


REPLY_AS = "Support"        # the name above a reply in game, as a chat bot signs it
REPLY_MAX = support.BODY_MAX


def help_desk(game):
    """(list, thread, reply, close) over support.py, for port.help_desk."""

    def online():
        with game._slock:
            return {s.inq_id for s in game.SESSIONS if s.inq_id}

    def listing(open_only):
        rows = []
        for t in reversed(support.api_tickets()):
            if open_only and t.get("closed"):
                continue
            rows.append({"id": str(t["id"]), "subject": t.get("subject") or "(no subject)",
                         "who": t.get("name") or t.get("inq") or "?", "at": _at(t.get("t")),
                         "closed": bool(t.get("closed")),
                         # a ticket is one message from the player; any reply answers it
                         "waiting": not t.get("closed") and not t.get("replies"),
                         "count": 1 + len(t.get("replies") or [])})
        return rows

    def thread(tid):
        _n, t = _ticket(tid)
        if t is None:
            return None
        subject = t.get("subject") or "(no subject)"
        msgs = [{"from": "player", "subject": subject, "text": t.get("body") or "",
                 "at": _at(t.get("t"))}]
        msgs += [{"from": "staff", "subject": "Re: " + subject, "text": r.get("text") or "",
                  "at": _at(r.get("t"))} for r in t.get("replies") or []]
        return {"id": str(t["id"]), "subject": subject, "closed": bool(t.get("closed")),
                "who": {"name": t.get("name") or t.get("inq") or "?",
                        "online": t.get("inq") in online()},
                "messages": msgs}

    def reply(tid, text, close, actor):
        n, t = _ticket(tid)
        if t is None:
            return False, "There is no request %s." % tid
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            return False, "Write a reply first."
        if len(text) > REPLY_MAX:
            return False, "A reply is at most %d characters." % REPLY_MAX
        try:
            support.reply(n, text, REPLY_AS)
            if close:
                support.close(n)
        except SystemExit:              # support.py's "no ticket N" (gone in between)
            return False, "There is no request %s." % tid
        print("adminport: %s replied to support #%d%s" % (actor or "?", n, " and closed it" if close else ""),
              flush=True)
        return True, "Sent. %s reads it under Support Replies%s." % (
            t.get("name") or "The player", "; the request is closed" if close else "")

    def close(tid, actor):
        n, t = _ticket(tid)
        if t is None:
            return False, "There is no request %s." % tid
        try:
            support.close(n)
        except SystemExit:
            return False, "There is no request %s." % tid
        print("adminport: %s closed support #%d" % (actor or "?", n), flush=True)
        return True, "Closed."

    return listing, thread, reply, close


#: live.char_of's preset numbers, as the customize screen colours them
CHAR_NAMES = ("white", "black", "red", "blue", "green", "yellow", "aqua", "pink")


def player_lookup(game, who):
    """What this game knows of the feega logins linked to one account (port.player), or None."""
    keys = []
    for link in (who.get("links") or {}).get("feega") or []:
        k = str(link.get("key") or "").strip()
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return None
    with game._plock:                   # player_info would create a record for an unknown key
        players = game._load_players()
        try:
            with open(game._profiles_path(), encoding="utf-8") as f:
                profiles = json.load(f)
        except (OSError, ValueError):
            profiles = {}
    profiles = profiles if isinstance(profiles, dict) else {}
    keys = [k for k in keys if isinstance(players.get(k), dict)]
    if not keys:
        return None
    with game._slock:
        where = {}
        for s in game.SESSIONS:
            if s.inq_id in keys:
                where[s.inq_id] = ("in a match" if s.room is not None and s.room.in_game
                                   else "in a room" if s.room is not None
                                   else "in the lobby" if s.in_lobby else "signing in")
    chars = []
    for k in keys:
        rec = players[k]
        feega_id = game.auth_name(k)
        name = rec.get("nick") or feega_id or "?"
        look = live._char(profiles, k)
        rounds, wins = int(rec.get("rounds", 0)), int(rec.get("wins", 0))
        facts = [{"label": "Points", "value": int(rec.get("points", 0))},
                 {"label": "Rounds won", "value": "%d of %d" % (wins, rounds)},
                 {"label": "Bomberman", "value": CHAR_NAMES[look["char"]]
                  if 0 <= look["char"] < len(CHAR_NAMES) else "custom look"}]
        if feega_id and feega_id != name:
            facts.insert(0, {"label": "feega-ID", "value": feega_id})
        chars.append({"name": name, "detail": where.get(k, ""), "online": k in where,
                      "facts": facts})
    return {"characters": chars}


def find_players(game, q, limit):
    """The panel's player picker: display names (what Warn takes) matching q, online first."""
    _players, names = live._names()
    with game._slock:
        online = {s.inq_id for s in game.SESSIONS if s.inq_id}
    q = (q or "").strip().lower()
    rows = [{"id": name, "label": name, "detail": "", "online": inq in online}
            for inq, name in names.items() if not q or q in name.lower()]
    rows.sort(key=lambda r: (not r["online"], r["label"].lower()))
    return rows[:limit]


RANK_BY = (("points", "Points"), ("wins", "Wins"), ("rounds", "Rounds"), ("rate", "Win rate"))


def rankings_page(game, query):
    q = dict(p.split("=", 1) for p in (query or "").split("&") if "=" in p)
    by = q.get("by") if q.get("by") in dict(RANK_BY) else "points"
    label = dict(RANK_BY)[by]
    snap = live.snapshot()
    rows = [[r["place"], r["name"], ("%.1f%%" % r["value"]) if by == "rate" else r["value"],
             r["wins"], r["rounds"]] for r in snap["standings"].get(by, [])]
    tabs = " ".join('<span class="pill">%s</span>' % esc(lbl) if key == by
                    else '<a href="?by=%s">%s</a>' % (key, esc(lbl)) for key, lbl in RANK_BY)
    note = {"points": "%d points a round won." % game.ROUND_POINTS,
            "rate": "Rounds won out of rounds played, for players with at least %d rounds."
                    % live.RATE_MIN_ROUNDS}.get(by, "")
    return page("Rankings",
                panel("", olpage.Raw("<p>%s</p>" % tabs),
                      table(["#", "Player", label, "Wins", "Rounds"], rows,
                            empty="Nobody is ranked by this yet."),
                      olpage.Raw('<p class="muted">%s</p>' % esc(note)) if note else ""),
                sub="The top %d by %s. %d ranked players, %d rounds played."
                % (live.STANDINGS_ROWS, label.lower(), snap["totals"]["players"],
                   snap["totals"]["rounds"]))


def matches_page():
    with live._mlock:
        matches = list(live._matches)
    rows = [[_when(m["at"]), m["room"], "Free-for-all" if m["mode"] == "free" else "Teams",
             m["rounds"], ", ".join(m["winners"]) or "-",
             ", ".join("%s %d" % (p["name"], p["wins"]) for p in m["players"])] for m in matches]
    return page("Recent matches",
                panel("", table(["Ended", "Room", "Mode", "Rounds", "Won", "Rounds won"], rows,
                                empty="No match has ended since the server started.")),
                sub="The last %d matches since the server started; a restart forgets them."
                % live.MATCHES_KEPT)


def tips_page(game, method, body):
    note = None
    if method == "POST":
        f = posted(body)
        try:
            i = int(f.get("index", ""))
        except ValueError:
            i = -1
        text = "" if f.get("clear") else f.get("text", "")
        if not 0 <= i < admin.TIP_COUNT:
            note = (False, "The tip number is 0 to %d." % (admin.TIP_COUNT - 1))
        elif not (f.get("clear") or text):
            note = (False, "Give a text, or clear the tip.")
        else:
            out = game.admin_request({"op": "tip", "index": i, "text": text, "clear": not text})
            if not out.get("ok"):
                note = (False, out.get("error") or "refused")
            elif not text:
                note = (True, "Tip %d cleared; clients show the stock tip after a restart." % i)
            else:
                n = out.get("sent_to", 0)
                note = (True, "Tip %d set and sent to %d player%s in the lobby."
                        % (i, n, "" if n == 1 else "s"))
    tips = {int(k): v for k, v in (game.admin_request({"op": "tips"}).get("tips") or {}).items()}
    rows = [[i, tips[i] if i in tips else olpage.Raw('<span class="muted">the stock tip</span>'),
             form([], "Clear", hidden={"index": i, "clear": "1"}, danger=True, inline=True)
             if i in tips else ""] for i in range(admin.TIP_COUNT)]
    return page("Lobby tips",
                panel("Set a tip", form([("index", "Tip 0-%d" % (admin.TIP_COUNT - 1), ""),
                                         ("text", "Text", "")], "Set")),
                panel("Tips", table(["#", "Text", ""], rows)),
                sub="A tip set here replaces the stock one, now and at every lobby entry.", note=note)


def build(game):
    """The AdminPort for `game` (the running bomb.game module); not started."""
    port = olport.AdminPort(
        "bomb", "Bomberman Online", short="Bomberman", platform="PS2 HDD",
        about="Lobby, rooms and matches; sign-in is the shared feega login.",
        token=TOKEN, bind=BIND, port=PORT, accounts="feega",
        services=[{"name": "Bomberman Online", "containers": CONTAINERS, "ports": {"10020": "game"},
                   "warning": "Everyone in Bomberman Online is disconnected."}])

    check = updates.from_env()

    @port.status
    def status():
        snap = live.snapshot()
        playing = [r for r in snap["rooms"] if r.get("in_game")]
        return {
            "up": True, "online": snap["online"],
            "players": [{"name": p["name"], "where": p["where"]} for p in snap["players"]],
            "rooms": [{"name": r["name"] or "Room %d" % r["id"], "players": len(r["players"]),
                       "max": r["max"], "mode": r.get("mode") or "",
                       "state": "playing" if r.get("in_game") else "locked" if r.get("locked") else "open"}
                      for r in snap["rooms"]],
            "rooms_label": "rooms",
            "summary": ("%d match%s playing" % (len(playing), "" if len(playing) == 1 else "es"))
            if playing else "",
            "facts": [{"label": "ranked players", "value": snap["totals"]["players"]},
                      {"label": "rounds played", "value": snap["totals"]["rounds"]}],
            "update": check.get(),
        }

    @port.action("announce", "Announce", kind="announce",
                 fields=[{"id": "text", "label": "Message", "type": "text"}],
                 about="A banner at the top of the screen (52 bytes) and the whole text as a "
                       "system line in the lobby and room chat. Players in a match see nothing.")
    def announce(params, actor):
        text = " ".join(str(params.get("text") or "").split())
        if not text:
            return False, "Nothing to announce."
        ok, msg = _said(game.admin_request({"op": "banner", "text": text}))
        if not ok:
            return ok, msg
        return _said(game.admin_request({"op": "chat", "text": text}))

    @port.action("chat", "Chat line", fields=[{"id": "text", "label": "Line", "type": "text"}],
                 about="A system line in the lobby and room chat logs.")
    def chat(params, actor):
        text = " ".join(str(params.get("text") or "").split())
        return _said(game.admin_request({"op": "chat", "text": text})) if text else (False, "No text.")

    @port.action("warn", "Warn a player", fields=[{"id": "name", "label": "Player", "type": "player"}],
                 about="The game's own \"stop remarks that upset others\" message, to one player.")
    def warn(params, actor):
        name = str(params.get("name") or "").strip()
        return _said(game.admin_request({"op": "warn", "name": name})) if name else (False, "No name.")

    @port.action("tip", "Set a lobby tip",
                 fields=[{"id": "index", "label": "Tip 0-12", "type": "text"},
                         {"id": "text", "label": "Text (empty clears it)", "type": "text"}],
                 about="Replaces one of the 13 lobby tips, now and at every lobby entry.")
    def tip(params, actor):
        try:
            i = int(str(params.get("index", "")).strip())
        except ValueError:
            return False, "The tip number is 0 to 12."
        text = str(params.get("text") or "").strip()
        out = game.admin_request({"op": "tip", "index": i, "text": text, "clear": not text})
        if out.get("ok") and not text:
            return True, "Tip %d cleared; clients show the stock tip after a restart." % i
        return _said(out)

    port.help_desk(*help_desk(game))

    @port.player
    def player(who):
        return player_lookup(game, who)

    @port.players
    def players(q, limit):
        return find_players(game, q, limit)

    @port.tool("rankings", "Rankings", "rankings",
               about="The standings by points, wins, rounds and win rate.")
    def rankings(method, sub, query, body, actor):
        return 200, olpage.CTYPE, rankings_page(game, query).encode()

    @port.tool("matches", "Recent matches", "matches", about="The last matches since the server started.")
    def matches(method, sub, query, body, actor):
        return 200, olpage.CTYPE, matches_page().encode()

    @port.tool("tips", "Lobby tips", "tips", about="The 13 lobby tips; set or clear your own.")
    def tips(method, sub, query, body, actor):
        return 200, olpage.CTYPE, tips_page(game, method, body).encode()

    return port


def start(game, log=print):
    """Start the admin port if BOMB_ADMIN_PORT is set. Never raises: a bad setting is logged and
    the game runs on without it."""
    if not PORT:
        return None
    try:
        port = build(game)
        host, n = port.start()
    except (OSError, ValueError) as ex:
        log(f"adminport: not started ({ex})")
        return None
    log(f"adminport: OpenLobby admin protocol on {host}:{n}" + ("" if TOKEN else " (no token)"))
    return port
