"""admin.py -- admin pushes into the running game server: announcements, conduct warnings and
lobby tips, over a unix socket in $BOMB_DATA_DIR (no port, no secret: whoever can reach the data
directory is the admin).

From the host (the compose project "bomb"):
  docker exec bomb-game-1 python -m bomb.admin who
  docker exec bomb-game-1 python -m bomb.admin banner "Server restart in 10 minutes"
  docker exec bomb-game-1 python -m bomb.admin chat "Welcome to Bomberman Online"
  docker exec bomb-game-1 python -m bomb.admin warn PLAYERNAME
  docker exec bomb-game-1 python -m bomb.admin tip 0 "Hold O to throw a bomb"
  docker exec bomb-game-1 python -m bomb.admin tip 0 --clear
  docker exec bomb-game-1 python -m bomb.admin tips

The S->C messages (MAIN.BIN, base 0x300000):
  0x07e6 (0x339568) +0 s16 type, +2 text. A leading 0x01 byte draws it in the system colour.
         1 = banner, cut at 52 bytes, shown 3600 frames; 2 = a line in the lobby and room chat logs
         (0x34f000 / 0x347468), 0x35 bytes each.
  0x07ef (0x339668) no payload: the client's own "stop remarks that upset others" message.
  0x07f2 (0x3398a8 -> 0x340318) +0 u32 tip index 0..12, +4 text, 54 bytes kept. Lives in client
         RAM only, so game.py sends the stored tips again at every lobby entry. A cleared tip goes
         back to the stock text only when the client restarts.
Players in a running match get nothing: the handlers do not check the scene and that path is
untested.
"""
import argparse
import json
import os
import socket
import sys
import threading

DATA_DIR = os.environ.get("BOMB_DATA_DIR", os.path.join(os.getcwd(), "data"))
SOCK = os.environ.get("BOMB_ADMIN_SOCK", os.path.join(DATA_DIR, "admin.sock"))
TIP_COUNT = 13
TIP_MAX = 54
BANNER_MAX = 52
SYSTEM_COLOUR = b"\x01"


def serve(handle, log=print, path=SOCK):
    """Answer one JSON request per connection with handle(req) -> dict, in a daemon thread."""
    if not hasattr(socket, "AF_UNIX"):
        log("admin: no unix sockets on this platform, admin channel off")
        return None
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)            # 0600: only the server's own user
    try:
        s.bind(path)
    finally:
        os.umask(old)
    s.listen(4)

    def one(conn):
        with conn:
            try:
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                req = json.loads(raw.decode("utf-8"))
                out = handle(req)
            except Exception as ex:  # an admin typo must never take the server down
                out = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            conn.sendall(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")

    def loop():
        while True:
            conn, _ = s.accept()
            threading.Thread(target=one, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    log(f"admin: listening on {path}")
    return s


def request(req, path=SOCK):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(path)
    with s:
        s.sendall(json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Operator pushes into the running Bomberman game server.")
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("who", help="list connected players")
    for name, what in (("banner", "a banner at the top of the screen"), ("chat", "a system line in the chat log")):
        p = sub.add_parser(name, help=f"show {what} to everyone in the lobby and rooms")
        p.add_argument("text")
        p.add_argument("--plain", action="store_true", help="normal colour instead of the system colour")
    p = sub.add_parser("warn", help="show the conduct warning to one player")
    p.add_argument("name")
    p = sub.add_parser("tip", help="replace lobby tip 0..12 (sent now and at every lobby entry)")
    p.add_argument("index", type=int)
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--clear", action="store_true")
    sub.add_parser("tips", help="list the stored tips")
    a = ap.parse_args(argv)
    req = {"op": a.op}
    for k in ("text", "plain", "name", "index", "clear"):
        if hasattr(a, k):
            req[k] = getattr(a, k)
    out = request(req)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
