"""lrclib.py — a polite, optional client for lrclib.net (free, no key, no sign-up).

Used for two things only: building a statistical picture of how real songs in a lane use language, and, when
the owner switches covers on, fetching one song to sing. It is never in the path of a radio song: every call
is on a background thread, everything is cached locally, and every failure returns empty rather than raising.

lrclib asks clients to identify themselves and to send requests one at a time with a short gap. Both are done
here: one lock, 400 ms between calls, and Retry-After is honoured.

A failed background call rests the client for a few minutes. A search somebody typed does not wait out that
rest (only a real 429 makes it wait): it tries, tries once more, and says so when lrclib is not answering.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger("tenforward.lyric_intel.lrclib")

BASE = "https://lrclib.net/api"
GAP_S = 0.4          # the docs ask for 200-500 ms between requests
TIMEOUT = 20
ASK_TIMEOUT = 12     # someone is waiting on a search they typed
_lock = threading.Lock()
_last_call = 0.0
_down_until = 0.0    # set when a background call fails; background calls wait until it passes
_slow_until = 0.0    # set by a 429; every call waits until it passes


def app_version() -> str:
    """The running server's version, read without importing server.py (under `python server.py` the module is
    __main__, and importing `server` would load the whole app a second time)."""
    for name in ("server", "__main__"):
        v = getattr(sys.modules.get(name), "VERSION", None)
        if v:
            return str(v)
    return "0"


def user_agent() -> str:
    return f"TenForward/{app_version()} (self-hosted AI radio)"


def available() -> bool:
    return time.time() >= max(_down_until, _slow_until)


class Unreachable(Exception):
    """lrclib did not answer a search somebody typed."""


_FAILED = object()


def _get(path: str, params: dict[str, Any] | None = None, asked: bool = False) -> Any:
    """One GET, rate limited. Returns None when lrclib has nothing. A background call never raises and returns
    None on any failure; a call somebody asked for (`asked`) ignores the background rest, tries twice and
    raises Unreachable when lrclib does not answer."""
    if asked:
        if time.time() < _slow_until:
            raise Unreachable("lrclib asked us to slow down")
        for attempt in (1, 2):
            out = _get_once(path, params, ASK_TIMEOUT)
            if out is not _FAILED:
                return out
            if time.time() < _slow_until:
                break
            if attempt == 1:
                time.sleep(1.0)
        raise Unreachable("lrclib did not answer")
    if not available():
        return None
    out = _get_once(path, params, TIMEOUT)
    return None if out is _FAILED else out


def _get_once(path: str, params: dict[str, Any] | None, timeout: float) -> Any:
    """One request. Returns the data, None for a 404, or _FAILED."""
    global _last_call, _down_until, _slow_until
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        url += "?" + urllib.parse.urlencode(clean)
    ua = user_agent()
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Lrclib-Client": ua, "Accept": "application/json"})
    with _lock:
        wait = GAP_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            _last_call = time.time()
            return data
        except urllib.error.HTTPError as e:
            _last_call = time.time()
            if e.code == 404:
                return None
            if e.code == 429:
                retry = 60
                try:
                    retry = max(5, min(3600, int(e.headers.get("Retry-After") or 60)))
                except Exception:
                    pass
                _slow_until = time.time() + retry
                LOG.warning("lrclib: rate limited, leaving it alone for %ds", retry)
                return _FAILED
            LOG.warning("lrclib: %s returned %s", path, e.code)
            _down_until = time.time() + 300
            return _FAILED
        except Exception as e:
            _last_call = time.time()
            LOG.warning("lrclib: %s failed (%s)", path, e)
            _down_until = time.time() + 300
            return _FAILED


def search(q: str | None = None, track_name: str | None = None, artist_name: str | None = None,
           album_name: str | None = None, asked: bool = False) -> list[dict[str, Any]]:
    """Keyword search over TITLE, ARTIST and ALBUM (never over the lyric text). At most 20 results.
    Every word has to be somewhere in those fields. With `asked`, raises Unreachable instead of returning []."""
    if not q and not track_name:
        return []
    data = _get("/search", {"q": q, "track_name": track_name, "artist_name": artist_name, "album_name": album_name},
                asked=asked)
    return data if isinstance(data, list) else []


def get(track_name: str, artist_name: str, album_name: str | None = None, duration: float | None = None) -> dict[str, Any] | None:
    data = _get("/get", {"track_name": track_name, "artist_name": artist_name, "album_name": album_name,
                         "duration": int(duration) if duration else None})
    return data if isinstance(data, dict) else None


def get_by_id(track_id: int, asked: bool = False) -> dict[str, Any] | None:
    data = _get(f"/get/{int(track_id)}", asked=asked)
    return data if isinstance(data, dict) else None


def health() -> dict[str, Any]:
    return {"base": BASE, "ok": available(), "cooldown_s": max(0, round(max(_down_until, _slow_until) - time.time())),
            "user_agent": user_agent()}
