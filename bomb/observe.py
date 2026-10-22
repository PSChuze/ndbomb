"""observe.py -- TCP observation stub: accept on the given ports, log every byte, answer nothing.

For the services we have not decoded yet. Known so far (MAIN.BIN, base 0x300000):
the client first dials the ticket server `ps2bb-pkgtkt.bomberman-online.com:9901` (struct built
at 0x31c720: type 2, port 0x26ad); the game server `ps2bb-pkggame01...` takes its port from a
server-list table (0x66aed8, 64-byte entries), so it is learned from the ticket reply.

Run:
  python -m bomb.observe --bind 0.0.0.0 --port 9901 [--port N ...]
Logs: $BOMB_LOG_DIR (default ./data/logs): observe.log (hexdump per session) and
observe.jsonl (one record per event: open / recv / close).
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

LOG_DIR = os.environ.get("BOMB_LOG_DIR", os.path.join(os.getcwd(), "data", "logs"))
_lock = threading.Lock()


def hexdump(b, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append(f"  {i:04x}  {chunk.hex(' '):<{width * 3}} {asc}")
    return "\n".join(out)


def emit(rec, text=None):
    rec["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "observe.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        line = f"{rec['t']} :{rec['port']} {rec['peer']} {rec['ev']}" + (f" {rec.get('n', '')}B" if rec["ev"] == "recv" else "")
        with open(os.path.join(LOG_DIR, "observe.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n" + (text + "\n" if text else ""))
        print(line + ("\n" + text if text else ""), flush=True)


def session(conn, peer, port):
    emit({"ev": "open", "port": port, "peer": peer})
    try:
        while True:
            b = conn.recv(65536)
            if not b:
                break
            emit({"ev": "recv", "port": port, "peer": peer, "n": len(b), "hex": b.hex()}, hexdump(b))
    except OSError as ex:
        emit({"ev": "error", "port": port, "peer": peer, "err": str(ex)})
    finally:
        conn.close()
        emit({"ev": "close", "port": port, "peer": peer})


def listen(bind, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, port))
    s.listen(8)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=session, args=(conn, f"{addr[0]}:{addr[1]}", port), daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, action="append", required=True)
    a = ap.parse_args(argv)
    for p in a.port:
        threading.Thread(target=listen, args=(a.bind, p), daemon=True).start()
    print(f"bomb.observe on {a.bind} ports {a.port}, logs {LOG_DIR}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
