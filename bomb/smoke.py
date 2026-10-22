"""smoke.py -- check a running stack from inside the game container.

  docker compose exec -T game python -m bomb.smoke

Checks that the game, ticket and web services answer, that the admin port describes the game,
and that the Feega service's account API is reachable. Prints one line per check; exit 1 when
one fails.
"""
import json
import os
import socket
import sys
import urllib.request

CHECKS = []


def check(what):
    def deco(fn):
        CHECKS.append((what, fn))
        return fn
    return deco


def connects(host, port, timeout=3):
    with socket.create_connection((host, port), timeout=timeout):
        return True


@check("game and lobby (TCP 10020)")
def game():
    return connects("127.0.0.1", int(os.environ.get("BOMB_GAME_PORT", "10020")))


@check("match relay (TCP 10081)")
def relay():
    port = int(os.environ.get("BOMB_RELAY_PORT", "0") or 0)
    return connects("127.0.0.1", port) if port else True


@check("ticket check (TCP 9901)")
def ticket():
    return connects("ticket", 9901)


@check("patch check (web, port 80)")
def web():
    req = urllib.request.Request("http://web/bombPatch/service/request_patchlist.asp?version=10200&df=0")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status == 200 and r.headers.get("X-bombClient") is not None


@check("admin port")
def admin():
    port = int(os.environ.get("BOMB_ADMIN_PORT", "0") or 0)
    if not port:
        return True
    req = urllib.request.Request("http://127.0.0.1:%d/openlobby/module.json" % port,
                                 headers={"X-Devtool-Token": os.environ.get("BOMB_ADMIN_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("key") == "bomb"


@check("Feega account API")
def feega():
    from . import feegalink
    req = urllib.request.Request(feegalink.URL + "/v1/health",
                                 headers={"X-Feega-Token": feegalink.TOKEN} if feegalink.TOKEN else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("ok") is True


def main():
    failed = 0
    for what, fn in CHECKS:
        try:
            ok = bool(fn())
            why = ""
        except Exception as e:          # noqa: BLE001 every failure is a line, not a traceback
            ok, why = False, " (%s)" % e
        print("%s %s%s" % ("ok  " if ok else "FAIL", what, why), flush=True)
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
