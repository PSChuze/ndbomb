"""relay.py -- the match relay for Bomberman Online (TCP, port from BOMB_RELAY_PORT).

The match runs between the consoles: the members link to the match master (the room host), and
when the master's slot record +0x34 is 0 they link through this relay instead of connecting to
the master's TCP 10080 directly, so nobody needs an open port. The game server names the relay in
S->C 0x07f0 (+0x52 host string, +0x64 port for the master, +0x68 port for members).

Client side (MAIN.BIN, verified):
  0x369f50  master: 0x327580(0x1000, key(master), 0); member: 0x327580(0x2000, key(own),
            key(master)). key(pid) = ((seed & 0xffff) << 16) + pid (0x327650), seed = sub 0x467.
  0x327580  TCP connect to 0x07f0 +0x52 : +0x64 / +0x68 (0x358198), state at 0x662aa0.
  0x3272c8  on connect: master sends type 1, member sends type 3 (+8 = master key).
  0x327360  receive: frames of a 16-byte header + payload; type < 9 -> 0x327080, else 0x3271e8.
  0x326520  a member not yet linked resends type 3 every ~30 ticks, up to 20 times.

Frame (both directions, little-endian): +0 u16 total length (header included), +2 u8 type,
+4 u32 key A, +8 u32 key B, +0xc u8 flag, then payload. Clients always put their own key in A.

  type 1  master -> relay   hello (A = master key)
  type 3  member -> relay   hello (A = member key, B = master key)
  type 4  relay -> master   a member arrived (B = member key); the master opens a peer slot for
                            it (0x369e90) and answers type 5 (B = member key)
  type 5  master -> relay   ack of a type 4
  type 6  relay -> member   +0xc = 1: linked (0x327080 calls the link's connected callback)
  type 7  relay -> client   a peer is gone (A = its key): the master drops that slot, a member
                            drops its master link
  type 0x10 data            master -> member: B = the member's key (from its type 4);
                            member -> master: B is not set by the client (0x369b38), so route
                            by connection. The master matches A to the peer slot. The payload
                            (the u16 type, u16 len frames of the TCP-10080 star) passes through.

Run on its own (dev):  python -m bomb.relay --bind 0.0.0.0 --port 10081
The game server starts it in-process when BOMB_RELAY_PORT is set.
"""
import argparse
import socket
import struct
import sys
import threading
import time

HDR = struct.Struct("<HBxIIB3x")   # len, type, key A, key B, flag
DUMP_FRAMES = 12                   # data frames per link logged in full (match protocol RE)
_lock = threading.Lock()
MASTERS = {}    # master key -> Link
WAITING = {}    # master key -> {member key: Link} (hello before the master arrived)


def frame(ftype, key_a, key_b=0, flag=0, payload=b""):
    return HDR.pack(HDR.size + len(payload), ftype, key_a & 0xffffffff, key_b & 0xffffffff, flag) + payload


def default_log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} relay {msg}", flush=True)


class Link:
    def __init__(self, conn, peer, log):
        self.conn, self.peer, self.log = conn, peer, log
        self.key = None
        self.role = None         # "master" / "member"
        self.master = None       # member: its master Link once linked
        self.members = {}        # master: member key -> Link
        self.target = None       # member: the master key it asked for
        self.frames = self.bytes = self.lost = 0   # data frames in from this console (>= 9)
        self._send_lock = threading.Lock()

    def send(self, data):
        try:
            with self._send_lock:
                self.conn.sendall(data)
        except OSError:
            pass

    def link_member(self, master, member):
        """Introduce a member to its master (type 4); the master's type 5 completes it."""
        member.master = master
        master.members[member.key] = member
        master.send(frame(4, master.key, member.key))

    def on_frame(self, ftype, a, b, flag, payload, raw=None):
        if ftype == 1:
            with _lock:
                self.role, self.key = "master", a
                old = MASTERS.get(a)
                MASTERS[a] = self
                waiting = WAITING.pop(a, {})
            if old is not None and old is not self:
                self.log(f"{self.peer} master {a:08x} replaces an older link")
            self.log(f"{self.peer} master hello {a:08x} ({len(waiting)} member(s) waiting)")
            for m in waiting.values():
                self.link_member(self, m)
        elif ftype == 3:
            with _lock:
                self.role, self.key, self.target = "member", a, b
                master = MASTERS.get(b)
                if master is None:
                    WAITING.setdefault(b, {})[a] = self
            if master is None:
                self.log(f"{self.peer} member {a:08x} waits for master {b:08x}")
            elif self.master is master and self.key in master.members:
                self.send(frame(6, master.key, a, 1))   # a repeated hello: already linked
            else:
                self.log(f"{self.peer} member {a:08x} -> master {b:08x}")
                self.link_member(master, self)
        elif ftype == 5 and self.role == "master":
            m = self.members.get(b)
            if m is not None:
                m.send(frame(6, self.key, m.key, 1))
                self.log(f"master {self.key:08x} accepted member {b:08x}")
        elif ftype >= 9:
            # Forward the bytes as they came: the header's spare bytes (+3, +0x0d..0x0f) are not
            # known to be unused, and rebuilding the frame would zero them.
            data = raw if raw is not None else frame(ftype, a, b, flag, payload)
            self.frames += 1
            self.bytes += len(data)
            if self.frames == 1:
                self.log(f"{self.peer} first data frame ({self.role or 'no hello'} {self.key or 0:08x}, "
                         f"type 0x{ftype:x}, {len(payload)} B)")
            if self.frames <= DUMP_FRAMES:
                # The match payload is not mapped yet: the first frames of each link, raw.
                self.log(f"{self.peer} data #{self.frames} {self.role or 'no hello'} type 0x{ftype:x} "
                         f"hdr {data[:HDR.size].hex(' ')} | {payload[:48].hex(' ')}")
            if self.role == "master":
                m = self.members.get(b)
                if m is not None:
                    m.send(data)
                else:
                    self.lost += 1
            elif self.role == "member" and self.master is not None:
                self.master.send(data)
            else:
                self.lost += 1
        else:
            self.log(f"{self.peer} unexpected type {ftype} a={a:08x} b={b:08x}")

    def close(self):
        with _lock:
            if self.role == "master" and MASTERS.get(self.key) is self:
                del MASTERS[self.key]
            if self.role == "member" and self.target is not None:
                WAITING.get(self.target, {}).pop(self.key, None)
                if not WAITING.get(self.target, True):
                    WAITING.pop(self.target, None)
        if self.role == "master":
            for m in list(self.members.values()):
                m.send(frame(7, self.key))
                m.master = None
        elif self.role == "member" and self.master is not None:
            self.master.members.pop(self.key, None)
            self.master.send(frame(7, self.key))
        self.log(f"{self.peer} closed ({self.role or 'no hello'} {self.key or 0:08x}): "
                 f"{self.frames} data frames / {self.bytes} B in, {self.lost} with nowhere to go")

    def run(self):
        buf = b""
        try:
            while True:
                chunk = self.conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= HDR.size:
                    n, ftype, a, b, flag = HDR.unpack_from(buf)
                    if n < HDR.size:
                        raise OSError(f"bad frame length {n}")
                    if len(buf) < n:
                        break
                    raw, buf = buf[:n], buf[n:]
                    self.on_frame(ftype, a, b, flag, raw[HDR.size:], raw)
        except OSError as ex:
            self.log(f"{self.peer} error {ex}")
        finally:
            self.close()
            self.conn.close()


def serve(bind, port, log=default_log):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, port))
    s.listen(32)
    log(f"listening on {bind}:{port}")
    while True:
        conn, addr = s.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=Link(conn, f"{addr[0]}:{addr[1]}", log).run, daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=10081)
    a = ap.parse_args(argv)
    serve(a.bind, a.port)


if __name__ == "__main__":
    sys.exit(main())
