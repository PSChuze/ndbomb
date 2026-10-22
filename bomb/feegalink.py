"""feegalink.py -- the feega-IDs behind the CommonInqIDs, asked of the Feega service.

The game only ever sees a player's CommonInqID (login 0x0404); the feega-ID the player signed in
with is their name in rooms, chat and the rankings. The Feega service (its own container, on the
shared `openlobby` Docker network) keeps the accounts, so this asks it:

  GET <BOMB_FEEGA_URL>/v1/accounts    {"accounts": {"<CommonInqID>": "<feega-ID>", ...}}

with the header X-Feega-Token when BOMB_FEEGA_TOKEN is set. The answer is kept for
BOMB_FEEGA_CACHE seconds (5), so a rename shows up within that time. When Feega cannot be
reached the last answer stays in use and the next try waits the same time; before the first
answer, names are empty and players show as their nickname or PLAYER.
"""
import json
import os
import threading
import time
import urllib.request

URL = os.environ.get("BOMB_FEEGA_URL", "http://feega:24030").rstrip("/")
TOKEN = os.environ.get("BOMB_FEEGA_TOKEN", "")
TTL = float(os.environ.get("BOMB_FEEGA_CACHE", "5") or 0)
TIMEOUT = 3

_lock = threading.Lock()
_cache = {"names": {}, "at": 0.0, "ok": False}


def fetch(url=None, timeout=TIMEOUT):
    """{CommonInqID: feega-ID} from the service, or None when it cannot be asked."""
    req = urllib.request.Request((url or URL) + "/v1/accounts",
                                 headers={"X-Feega-Token": TOKEN} if TOKEN else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.load(r)
    except (OSError, ValueError):
        return None
    names = doc.get("accounts") if isinstance(doc, dict) else None
    if not isinstance(names, dict):
        return None
    return {str(k): v for k, v in names.items() if isinstance(v, str)}


def names():
    """Every account's feega-ID by CommonInqID (cached; see the module docstring)."""
    with _lock:
        if _cache["at"] and time.monotonic() - _cache["at"] < TTL:
            return _cache["names"]
        got = fetch()
        _cache["at"] = time.monotonic()
        # A failed ask also waits out the TTL, so a missing service never stalls a session
        # on one timeout per name.
        if got is not None:
            _cache["names"], _cache["ok"] = got, True
        return _cache["names"]


def name(inq_id):
    """The feega-ID for one CommonInqID, "" when unknown."""
    return names().get(inq_id or "", "")


def forget():
    """Drop the cached answer (tests, and after a change this process made)."""
    with _lock:
        _cache.update(names={}, at=0.0, ok=False)
