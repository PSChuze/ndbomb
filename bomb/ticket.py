"""ticket.py -- the ticket server (ps2bb-pkgtkt.bomberman-online.com, TCP 9901).

After Feega's AuthGroup (ChargeStatus=1, the service's ServiceStatus=2) the game authenticates
its service ticket here (MAIN.BIN 0x31c878, base 0x300000). Plain TCP, fixed-width ASCII.

Request, 0xe3 bytes (built at 0x46a0f8):
  +0x00 "ST"  +0x02 'C'
  +0x03 NowDate      14 bytes (Feega NowDate, YYYYMMDDhhmmss)
  +0x11 CommonInqID  10 bytes, right-aligned (a longer ID overwrites the date)
  +0x1b ServiceID    12 bytes, right-aligned
  +0x27 EndDate      14 bytes (the service's EndDate)
  +0x35 0xac bytes   a block from 0x31c300 (not yet understood; logged)
  +0xe1 "ED"
Fields are '0'-filled before the copies.

Reply, 7 bytes (checked at 0x31cb98..0x31cc08): "ST", two bytes not checked there, the result
byte, "ED". The result goes back to the login flow (0x343fb8): 0 = ticket accepted, 3 = state
0x5f, anything else = Ticket authentication failed with that number as the error code.

Run:
  python -m bomb.ticket --bind 0.0.0.0 --port 9901
Logs: $BOMB_LOG_DIR (default ./data/logs): ticket.log / ticket.jsonl.
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

LOG_DIR = os.environ.get("BOMB_LOG_DIR", os.path.join(os.getcwd(), "data", "logs"))
REQ_LEN = 0xE3
RESULT = int(os.environ.get("BOMB_TICKET_RESULT", "0"))


def parse(req):
    return {
        "tag": req[0:3].decode("latin1"),
        "now": req[3:0x11].decode("latin1"),
        "inq": req[0x11:0x1b].decode("latin1").lstrip("0"),
        "service": req[0x1b:0x27].decode("latin1").lstrip("0"),
        "end": req[0x27:0x35].split(b"\0")[0].decode("latin1"),
        "tail": req[0xe1:0xe3].decode("latin1"),
    }


def log(rec):
    rec["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "ticket.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    line = f"{rec['t']} {rec['peer']} {rec.get('fields', rec.get('note', ''))} -> {rec.get('reply', '')}"
    with open(os.path.join(LOG_DIR, "ticket.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def session(conn, peer):
    buf = b""
    try:
        conn.settimeout(30)
        while len(buf) < REQ_LEN:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if len(buf) < REQ_LEN:
            log({"peer": peer, "note": f"short request {len(buf)} B", "hex": buf.hex()})
            return
        req = buf[:REQ_LEN]
        reply = b"ST" + req[2:3] + b"0" + bytes([RESULT]) + b"ED"
        conn.sendall(reply)
        log({"peer": peer, "fields": parse(req), "hex": req.hex(), "reply": reply.hex()})
        time.sleep(1.0)  # let PCSX2's Sockets backend deliver the reply before our FIN
    except OSError as ex:
        log({"peer": peer, "note": f"error {ex}", "hex": buf.hex()})
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9901)
    a = ap.parse_args(argv)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.bind, a.port))
    s.listen(16)
    print(f"bomb.ticket on {a.bind}:{a.port}, result {RESULT}, logs {LOG_DIR}", flush=True)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=session, args=(conn, f"{addr[0]}:{addr[1]}"), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
