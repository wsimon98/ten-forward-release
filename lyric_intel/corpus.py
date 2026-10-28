"""corpus.py — a statistical picture of how real songs in a lane use language.

This is the optional half of the feature and it is OFF until the owner turns it on. It fetches a few songs
per artist from lrclib, measures them the same way Ten Forward measures its own writing, and keeps the
NUMBERS. The lyric text is thrown away after measuring unless covers are switched on, and no real lyric line
ever reaches the writer from here: what comes out is "songs in this lane spread their images across these
drawers", which is used to pick the drawer Ten Forward has been ignoring.

Artists come from the station's own description (the channels already name the acts they are in the lane of)
and can be overridden in data/corpus_artists.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import features, lrclib
from .lexicon import STOP

LOG = logging.getLogger("tenforward.lyric_intel.corpus")
OVERRIDE_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus_artists.json"

_building = threading.Lock()

LANE_LEADS = re.compile(r"(?:in the lane of|in the style of|lane of|the tradition of|like)\s+(.{10,400}?)(?:[:.]|$)", re.I)
NAME = re.compile(r"\b([A-Z][\w'’&.-]*(?:\s+[A-Z][\w'’&.-]*){0,3})\b")
SKIP_NAMES = {"The", "A", "An", "English", "BPM", "Instrumental"}


def lane_of(station: dict[str, Any]) -> str:
    return str(station.get("fusion_set") or station.get("id") or "other")


def _overrides() -> dict[str, list[str]]:
    try:
        if OVERRIDE_PATH.exists():
            data = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): [str(a) for a in (v or [])] for k, v in data.items()}
    except Exception as e:
        LOG.warning("corpus: could not read %s: %s", OVERRIDE_PATH, e)
    return {}


def artists_for(station: dict[str, Any]) -> list[str]:
    """Acts to sample for this channel: the override file first, then the names in its own description."""
    over = _overrides()
    for key in (station.get("id"), lane_of(station)):
        if key and over.get(key):
            return over[key][:24]
    desc = station.get("description") or ""
    m = LANE_LEADS.search(desc)
    chunk = m.group(1) if m else ""
    if not chunk:
        return []
    chunk = chunk.replace(" and ", ", ")
    out: list[str] = []
    for part in chunk.split(","):
        part = part.strip(" .;")
        if not part or len(part) < 3:
            continue
        names = NAME.findall(part)
        best = max(names, key=len) if names else ""
        best = best.strip()
        if best and best not in SKIP_NAMES and best not in out and len(best) > 2:
            out.append(best)
    return out[:24]


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()


def looks_english(text: str) -> bool:
    toks = features.tokens(text)[:400]
    if len(toks) < 40:
        return False
    hits = sum(1 for t in toks if t in STOP)
    return hits / len(toks) >= 0.18


def store(db, rec: dict[str, Any], lane: str, keep_text: bool) -> bool:
    """Measure one lrclib record and keep the numbers (and the words only if covers are on)."""
    try:
        plain = (rec.get("plainLyrics") or "").strip()
        if rec.get("instrumental") or len(plain) < 200 or not looks_english(plain):
            return False
        h = text_hash(plain)
        if db.count("lrclib_cache", "text_hash=?", (h,)):
            return False
        f = features.extract(plain)
        db.insert("lrclib_cache", {
            "id": int(rec.get("id") or 0), "track": rec.get("trackName") or rec.get("name"), "artist": rec.get("artistName"),
            "album": rec.get("albumName"), "duration": rec.get("duration"), "instrumental": 0, "lang": "en",
            "text_hash": h, "plain": plain if keep_text else None, "lane": lane, "fetched": time.time(), "features": f,
        })
        return True
    except Exception:
        LOG.debug("corpus: could not store a record", exc_info=True)
        return False


def profile_from(db, lane: str) -> dict[str, Any]:
    """Aggregate every cached song in a lane into one profile of numbers."""
    rows = [r for r in db.query("lrclib_cache", "lane=?", (lane,), order="fetched DESC", limit=2000)
            if isinstance(r.get("features"), dict)]
    if not rows:
        return {}
    buckets: Counter[str] = Counter()
    btotal = 0
    persp: Counter[str] = Counter()
    opens: Counter[str] = Counter()
    lines, ttr, avg_len, rhyme_rate, chorus = [], [], [], [], []
    vocab: Counter[str] = Counter()
    for r in rows:
        f = r["features"]
        b = f.get("buckets") or {}
        buckets.update(b)
        btotal += sum(b.values())
        persp[f.get("perspective") or "first"] += 1
        opens[f.get("opening") or "none"] += 1
        lines.append(f.get("lines") or 0)
        ttr.append(f.get("ttr") or 0)
        avg_len.append(f.get("avg_line_words") or 0)
        chorus.append(f.get("chorus_repeat") or 0)
        rhyme_rate.append(len(f.get("rhymes") or []) / max(1, f.get("lines") or 1))
        vocab.update(set(f.get("words") or []))

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    n = len(rows)
    return {
        "n": n,
        "buckets": {b: round(c / btotal, 4) for b, c in buckets.items()} if btotal else {},
        "perspectives": {k: round(v / n, 3) for k, v in persp.items()},
        "openings": {k: round(v / n, 3) for k, v in opens.items()},
        "lines": mean(lines), "ttr": mean(ttr), "avg_line_words": mean(avg_len),
        "rhyme_rate": mean(rhyme_rate), "chorus_repeat": mean(chorus),
        "vocabulary": len(vocab),
        "word_spread": round(len(vocab) / n, 1) if n else 0,
    }


def build(db, station: dict[str, Any], budget: int = 500, keep_text: bool = False, per_artist: int = 8) -> dict[str, Any]:
    """Fetch and measure songs for one channel's lane. Background work: never call this from a song plan."""
    lane = lane_of(station)
    acts = artists_for(station)
    if not acts:
        return {"lane": lane, "added": 0, "artists": [], "note": "this channel's description does not name any acts"}
    have = db.count("lrclib_cache", "lane=?", (lane,))
    added = 0
    for act in acts:
        if have + added >= budget or not lrclib.available():
            break
        for rec in lrclib.search(q=act)[:per_artist]:
            if have + added >= budget:
                break
            if store(db, rec, lane, keep_text):
                added += 1
    prof = profile_from(db, lane)
    if prof:
        db.insert("corpus_profile", {"lane": lane, "n_songs": prof.get("n", 0), "profile": prof, "built": time.time()})
    LOG.info("corpus: lane %s now holds %d songs (%d new) from %d acts", lane, prof.get("n", 0), added, len(acts))
    return {"lane": lane, "added": added, "total": prof.get("n", 0), "artists": acts}


def build_all(db, stations: list[dict[str, Any]], budget: int = 500, keep_text: bool = False) -> list[dict[str, Any]]:
    """One pass over every channel that can have a lane. Runs on its own thread, one request at a time."""
    if not _building.acquire(blocking=False):
        return []
    try:
        out = []
        for st in stations:
            if st.get("instrumental") or not st.get("enabled"):
                continue
            try:
                out.append(build(db, st, budget=budget, keep_text=keep_text))
            except Exception:
                LOG.exception("corpus: lane build failed for %s", st.get("id"))
        return out
    finally:
        _building.release()


def profile(db, station: dict[str, Any]) -> dict[str, Any]:
    row = db.get("corpus_profile", lane_of(station))
    prof = (row or {}).get("profile")
    return prof if isinstance(prof, dict) else {}


def under_used(prof: dict[str, Any], state: dict[str, Any], skip: set[str] | None = None, n: int = 3) -> list[str]:
    """Drawers real songs in this lane open more than Ten Forward has been opening them."""
    if not prof or not prof.get("buckets"):
        return []
    skip = skip or set()
    mine = state.get("buckets") or {}
    gaps = [(b, share - mine.get(b, 0.0)) for b, share in prof["buckets"].items() if b not in skip]
    gaps.sort(key=lambda x: -x[1])
    return [b for b, gap in gaps[:n] if gap > 0.01]


def status(db) -> dict[str, Any]:
    rows = db.query("corpus_profile", order="built DESC", limit=50)
    return {
        "lanes": [{"lane": r["lane"], "songs": r.get("n_songs") or 0, "built": r.get("built"),
                   "profile": r.get("profile") if isinstance(r.get("profile"), dict) else {}} for r in rows],
        "cached_songs": db.count("lrclib_cache"),
        "with_text": db.count("lrclib_cache", "plain IS NOT NULL"),
        "lrclib": lrclib.health(),
    }
