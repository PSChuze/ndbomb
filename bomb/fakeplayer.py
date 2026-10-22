"""fakeplayer.py -- a scripted second player for the game server (TCP 10020).

Speaks the lobby / room side of the protocol the way the PS2 client does, so one console can be
tested against a second member: log in (0x03ea), load the profile (0x0438), enter (0x03ee), list
rooms (0x040c), then create (0x03f4) or join (0x03f6) a room. It answers 0x07dc (start go) and
0x044c sub 0x469 (next round) with 0x044c sub 0x466 (setup done), as 0x336718 does. It does not
play the match itself (that runs between the consoles on TCP 10080).

Run (while a console sits in a room or the lobby):
  python -m bomb.fakeplayer --host 127.0.0.1 --name BOT --join first
  python -m bomb.fakeplayer --host 127.0.0.1 --name BOT --create "bot room"
"""
import argparse
import hashlib
import socket
import struct
import sys
import threading
import time


def frame(cmd, payload=b""):
    return struct.pack("<HH", cmd, len(payload)) + payload


def login_payload(inq_id):
    """0x03ea / 0x0404 layout (187 B): CommonInqID padded to 0x75, IP to 0x86, u16 0x10, three u32
    lengths, then CommonInqID, ServiceID, EndDate NUL-terminated."""
    p = bytearray(0x86)
    p[0:len(inq_id)] = inq_id.encode()
    ip = b"192.0.2.101"
    p[0x75:0x75 + len(ip)] = ip
    strs = [inq_id.encode(), b"SLPS20343100", b"20371231235959"]
    p += struct.pack("<H", 0x10) + b"".join(struct.pack("<I", len(x) + 1) for x in strs)
    p += b"".join(x + b"\0" for x in strs)
    return bytes(p)


def room_request(rid=0, name=b"", password=b"", mode=2, max_players=8):
    """62-byte room record, as 0x03f4 (create) and 0x03f6 (join) send it."""
    r = bytearray(0x3e)
    struct.pack_into("<h", r, 0, rid)
    r[5:5 + len(name[:0x18])] = name[:0x18]
    r[0x1e:0x1e + len(password[:0xd])] = password[:0xd]
    struct.pack_into("<hh", r, 0x2c, mode, max_players)
    return bytes(r)


class Player:
    def __init__(self, host, port, name, quiet=False):
        # the same stable id feega.py would have handed out for this AuthID
        self.inq = "%010d" % (int(hashlib.sha1(name.encode()).hexdigest(), 16) % 10 ** 10)
        self.name, self.quiet = name, quiet
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        self.pid = None
        self.inbox = []           # (cmd, payload) in arrival order
        self.cv = threading.Condition()
        threading.Thread(target=self._reader, daemon=True).start()

    def log(self, msg):
        if not self.quiet:
            print(f"[{self.name}] {msg}", flush=True)

    def send(self, cmd, payload=b""):
        self.sock.sendall(frame(cmd, payload))

    def _reader(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                with self.cv:
                    self.inbox.append((None, b""))
                    self.cv.notify_all()
                return
            buf += chunk
            while len(buf) >= 4:
                cmd, n = struct.unpack_from("<HH", buf)
                if len(buf) < 4 + n:
                    break
                payload, buf = buf[4:4 + n], buf[4 + n:]
                self.on_message(cmd, payload)
                with self.cv:
                    self.inbox.append((cmd, payload))
                    self.cv.notify_all()

    def on_message(self, cmd, p):
        if cmd == 0x07dc or (cmd == 0x044c and struct.unpack_from("<h", p)[0] == 0x469):
            sub = bytearray(0x14)
            struct.pack_into("<hxxh", sub, 0, 0x466, self.pid or 0)
            self.send(0x044c, bytes(sub))
        s32 = struct.unpack_from("<i", p)[0] if len(p) >= 4 else None
        extra = ""
        if cmd in (0x07d4, 0x07d5, 0x07dd, 0x07e5):
            extra = f" room {struct.unpack_from('<h', p)[0]} members {struct.unpack_from('<h', p, 2)[0]} " \
                    f"name {p[5:0x1e].split(bytes(1))[0].decode('cp932', 'replace')!r}"
        elif cmd in (0x07d7, 0x07d8):
            extra = f" player {struct.unpack_from('<h', p)[0]} slot {struct.unpack_from('<h', p, 2)[0]} " \
                    f"name {p[4:0x2c].split(bytes(1))[0].decode('cp932', 'replace')!r} host {struct.unpack_from('<h', p, 0x30)[0]}"
        elif cmd in (0x03ff, 0x03f1):
            extra = f" {p.split(bytes(1))[0].decode('cp932', 'replace')!r}"
        elif cmd == 0x044c:
            extra = f" sub 0x{struct.unpack_from('<H', p)[0]:x} seed {struct.unpack_from('<I', p, 0x10)[0]}"
        elif s32 is not None:
            extra = f" {s32}"
        self.log(f"<- 0x{cmd:04x}{extra}")

    def wait(self, cmd, timeout=5, since=0):
        """Wait for the first `cmd` at inbox index >= since; returns (index, payload)."""
        end = time.time() + timeout
        with self.cv:
            while True:
                for i in range(since, len(self.inbox)):
                    if self.inbox[i][0] == cmd:
                        return i, self.inbox[i][1]
                    if self.inbox[i][0] is None:
                        raise ConnectionError(f"{self.name}: closed while waiting for 0x{cmd:04x}")
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(f"{self.name}: no 0x{cmd:04x} within {timeout} s")
                self.cv.wait(left)

    def mark(self):
        with self.cv:
            return len(self.inbox)

    def got(self, cmd, since=0):
        with self.cv:
            return [p for c, p in self.inbox[since:] if c == cmd]

    def enter_lobby(self):
        self.send(0x03ea, login_payload(self.inq))
        self.pid = struct.unpack("<i", self.wait(0x03eb)[1])[0]
        self.send(0x0438)
        self.wait(0x0445)
        self.send(0x03ee)
        self.wait(0x03ef)
        return self.list_rooms()

    def list_rooms(self):
        m = self.mark()
        self.send(0x040c, struct.pack("<HH", 1, 0))
        self.wait(0x040d, since=m)
        time.sleep(0.3)
        return [struct.unpack_from("<h", p)[0] for p in self.got(0x07d4, m)]

    def create(self, name, password=b""):
        m = self.mark()
        self.send(0x03f4, room_request(name=name.encode("cp932"), password=password))
        rid = struct.unpack("<i", self.wait(0x03f5, since=m)[1])[0]
        self.wait(0x07df, since=m)
        return rid

    def join(self, rid, password=b""):
        m = self.mark()
        self.send(0x03f6, room_request(rid=rid, password=password))
        code = struct.unpack("<i", self.wait(0x03f7, since=m)[1])[0]
        if code >= 0:
            self.wait(0x07df, since=m)
        return code

    def chat(self, text):
        """Room chat (0x03fe); lobby chat is 0x03f0."""
        self.send(0x03fe, text.encode("cp932") + b"\0")

    def lobby_chat(self, text):
        self.send(0x03f0, text.encode("cp932") + b"\0")

    def report_round(self, outcome, code):
        """The end-of-round reports a client sends (0x373250): one sub 0x453 per player who is OUT
        (+4 player id, +0x0a 0 = died, entry state 7), none for the living, then sub 0x476
        (+4 own id, +0x0a round code). outcome: pid -> 1 out / 0 standing."""
        for pid, out in outcome.items():
            if not out:
                continue
            sub = bytearray(0x14)
            struct.pack_into("<hxxh", sub, 0, 0x453, pid)
            self.send(0x044c, bytes(sub))
        sub = bytearray(0x14)
        struct.pack_into("<hxxh", sub, 0, 0x476, self.pid)
        struct.pack_into("<h", sub, 0x0a, code)
        self.send(0x044c, bytes(sub))

    def start(self):
        self.send(0x03fa, struct.pack("<hhhh", self.pid, 0, 0, 0))

    def leave(self):
        m = self.mark()
        self.send(0x03f8, struct.pack("<I", 3))
        self.wait(0x03f9, since=m)

    def heartbeat_loop(self):
        while True:
            time.sleep(10)
            try:
                self.send(0x0514, struct.pack("<I", 0))
            except OSError:
                return


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10020)
    ap.add_argument("--name", default="BOT", help="AuthID to log in as (becomes the room name tag)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--join", help="room id, or 'first' for the first listed room")
    g.add_argument("--create", help="create a room with this name")
    ap.add_argument("--password", default="")
    ap.add_argument("--say", help="chat line to send once in the room")
    ap.add_argument("--stay", type=float, default=600, help="seconds to stay connected")
    a = ap.parse_args(argv)

    pl = Player(a.host, a.port, a.name)
    rooms = pl.enter_lobby()
    pl.log(f"in lobby as player {pl.pid}; rooms {rooms}")
    threading.Thread(target=pl.heartbeat_loop, daemon=True).start()
    if a.create:
        pl.log(f"created room {pl.create(a.create, a.password.encode())}")
    elif a.join:
        rid = rooms[0] if a.join == "first" and rooms else int(a.join) if a.join != "first" else None
        if rid is None:
            pl.log("no room to join")
            return 1
        pl.log(f"join room {rid}: {pl.join(rid, a.password.encode())}")
    if a.say:
        pl.chat(a.say)
    try:
        time.sleep(a.stay)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
