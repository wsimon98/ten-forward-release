"""memory.py — what Ten Forward has been writing lately, per channel and across the dial.

Reads the feature rows saved after every song (and back-fills any song written before this existed), then
answers one question: which words, phrases, rhyme tails, image buckets, openings and points of view is this
channel leaning on right now. Counted in SONGS, not hours, because the radio can write seventy in a day.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any

from . import features

LOG = logging.getLogger("tenforward.lyric_intel.memory")

WINDOW_DEFAULT = 25
_backfill_lock = threading.Lock()


# --------------------------------------------------------------------------- writing
def remember(db, song: dict[str, Any]) -> dict[str, Any] | None:
    """Store the feature row for a finished song. Imports, covers and instrumentals are not this radio's
    own writing habit, so they are left out of the memory."""
    try:
        if not song or not song.get("lyrics"):
            return None
        if (song.get("source") or "") in ("import",):
            return None
        if song.get("cover_of"):
            return None
        lines = features.lyric_lines(song["lyrics"])
        if len(lines) < 4:
            return None  # an instrumental skeleton
        f = features.extract(song["lyrics"], song.get("topic") or "")
        row = {"song_id": song["id"], "station_id": song.get("station_id"), "created": song.get("created") or time.time(),
               "features": f, "novelty": song.get("_novelty"), "guidance": song.get("_guidance"), "version": features.VERSION}
        db.insert("lyric_features", row)
        return row
    except Exception:
        LOG.exception("lyric memory: could not store features for %s", (song or {}).get("id"))
        return None


def backfill(db, limit: int = 400) -> int:
    """Give the memory the songs written before it existed. Safe to call at every start."""
    if not _backfill_lock.acquire(blocking=False):
        return 0
    try:
        done = {r["song_id"] for r in db.query("lyric_features", limit=5000, order="created DESC")}
        todo = [s for s in db.query("songs", "lyrics IS NOT NULL AND status='ready'", limit=limit, order="created DESC")
                if s["id"] not in done]
        n = 0
        for s in todo:
            if remember(db, s):
                n += 1
        if n:
            LOG.info("lyric memory: read %d older songs into the memory", n)
        return n
    except Exception:
        LOG.exception("lyric memory: backfill failed")
        return 0
    finally:
        _backfill_lock.release()


# --------------------------------------------------------------------------- reading
def _rows(db, station_id: str | None, window: int) -> list[dict[str, Any]]:
    if station_id:
        rows = db.query("lyric_features", "station_id=?", (station_id,), order="created DESC", limit=window)
    else:
        rows = db.query("lyric_features", order="created DESC", limit=window)
    return [r for r in rows if isinstance(r.get("features"), dict)]


def _pending(db, station_id: str | None) -> list[dict[str, Any]]:
    """Songs already planned but not rendered yet count as recent: three plans in ten minutes used to land
    on the same images."""
    out = []
    try:
        where = "type='song' AND status IN ('queued','running')"
        params: list[Any] = []
        if station_id:
            where += " AND params LIKE ?"
            params.append(f'%"station_id": "{station_id}"%')
        for j in db.query("jobs", where, params, limit=12):
            p = j.get("params") or {}
            if p.get("lyrics") and not p.get("cover_of"):
                out.append(features.extract(p["lyrics"], p.get("topic") or ""))
    except Exception:
        LOG.debug("lyric memory: could not read the render line", exc_info=True)
    return out


def state(db, station_id: str | None, window: int = WINDOW_DEFAULT) -> dict[str, Any]:
    """What this channel has been reaching for. Shares are 'in this fraction of the last N songs'."""
    feats = [r["features"] for r in _rows(db, station_id, window)] + _pending(db, station_id)
    n = len(feats)
    if not n:
        return {"n": 0, "words": {}, "phrases": {}, "rhymes": {}, "buckets": {}, "openings": {}, "perspectives": {},
                "opening_words": {}, "ideas": [], "station_id": station_id}
    ideas: list[set[str]] = []
    words: Counter[str] = Counter()
    phrases: Counter[str] = Counter()
    rhymes: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    openings: Counter[str] = Counter()
    opening_words: Counter[str] = Counter()
    persp: Counter[str] = Counter()
    bucket_total = 0
    for f in feats:
        if f.get("topic_words"):
            ideas.append(set(f["topic_words"]))
        words.update(set(f.get("words") or []))
        phrases.update(set(f.get("phrases") or []))
        rhymes.update(set(f.get("rhymes") or []))
        b = f.get("buckets") or {}
        buckets.update(b)
        bucket_total += sum(b.values())
        openings[f.get("opening") or "none"] += 1
        if f.get("opening_words"):
            opening_words[f["opening_words"]] += 1
        persp[f.get("perspective") or "first"] += 1
    return {
        "n": n,
        "station_id": station_id,
        "ideas": ideas,
        "words": {w: c / n for w, c in words.items()},
        "phrases": dict(phrases),
        "rhymes": dict(rhymes),
        "buckets": {b: c / bucket_total for b, c in buckets.items()} if bucket_total else {},
        "bucket_counts": dict(buckets),
        "openings": dict(openings),
        "opening_words": dict(opening_words),
        "perspectives": dict(persp),
    }


def habits(db, station_id: str | None = None, window: int = WINDOW_DEFAULT, top: int = 12) -> dict[str, Any]:
    """A readable version of the same thing, for the Settings page."""
    st = state(db, station_id, window)
    if not st["n"]:
        return {"songs": 0, "station": station_id, "words": [], "phrases": [], "rhymes": [], "buckets": [],
                "openings": [], "perspectives": []}
    return {
        "songs": st["n"],
        "station": station_id,
        "words": [{"word": w, "share": round(s, 3)} for w, s in sorted(st["words"].items(), key=lambda x: -x[1])[:top] if s >= 0.2],
        "phrases": [{"phrase": p, "songs": c} for p, c in sorted(st["phrases"].items(), key=lambda x: -x[1])[:top] if c >= 3],
        "rhymes": [{"pair": r.replace("/", " / "), "songs": c} for r, c in sorted(st["rhymes"].items(), key=lambda x: -x[1])[:top] if c >= 2],
        "buckets": [{"bucket": b, "share": round(s, 3)} for b, s in sorted(st["buckets"].items(), key=lambda x: -x[1])[:top]],
        "openings": [{"kind": k, "songs": c} for k, c in sorted(st["openings"].items(), key=lambda x: -x[1])],
        "perspectives": [{"kind": k, "songs": c} for k, c in sorted(st["perspectives"].items(), key=lambda x: -x[1])],
    }
