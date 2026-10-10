"""lyric_intel — Ten Forward's memory of its own writing.

The radio used to reach for the same objects over and over because nothing ever looked back at what it had
written. This package looks back. It measures every finished song (which words, which phrases, which rhyme
tails, which drawer of images, how it opened, who was speaking), and before the next song is planned it hands
the writer a short block of guidance: here is a drawer you have not opened lately, here are the handful of
words this channel has worn out. After the lyrics come back it scores them, and at most once it asks for the
repeating lines to be rewritten. Nothing loops, nothing is thrown away.

Everything here is deterministic python except that one optional rewrite. There is no extra model asking a
model whether it repeats itself.

Two optional extras, both off until switched on: a statistical picture of a lane built from lrclib.net, and
covers of real songs.

Every entry point below is wrapped so a failure can only mean "no guidance this time": the radio keeps going.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from typing import Any

from . import corpus, covers, features, finder, guidance, lrclib, memory, novelty
from .lexicon import BUCKET_LABELS

LOG = logging.getLogger("tenforward.lyric_intel")

__all__ = ["enabled", "level_name", "prepare", "review", "remember", "habits", "backfill_async",
           "corpus_async", "cover_plan", "cover_chance", "status", "covers", "corpus", "finder", "lrclib",
           "features", "memory", "novelty", "guidance"]


# --------------------------------------------------------------------------- settings
def _cfg(key: str, default: Any = None) -> Any:
    try:
        import config
        return config.cfg(key)
    except Exception:
        return default


def worn_out() -> list[str]:
    return novelty.worn_words()


def worn_hits(text: str) -> list[str]:
    """Worn-out entries that appear in this text (see novelty.worn_pattern for how they match)."""
    return novelty.worn_found(text)


def level_name() -> str:
    return str(_cfg("lyric_memory", "normal") or "normal").lower()


def enabled() -> bool:
    return level_name() != "off"


def window() -> int:
    try:
        return max(5, min(200, int(_cfg("lyric_window", 25) or 25)))
    except Exception:
        return 25


def retry_on() -> bool:
    return bool(_cfg("lyric_retry", True))


def per_station() -> bool:
    return bool(_cfg("lyric_station_memory", True))


def outside_on() -> bool:
    return bool(_cfg("lrclib_enabled", False))


def keep_text() -> bool:
    return bool(_cfg("lrclib_keep_text", False))


def cover_chance(station: dict[str, Any]) -> float:
    """A channel's own covers share, falling back to the global default. Zero unless the outside source is on."""
    if not outside_on():
        return 0.0
    c = (station or {}).get("cover_chance")
    if c is None:
        c = _cfg("cover_chance", 0.0) or 0.0
    try:
        return max(0.0, min(1.0, float(c)))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- before the song is written
def prepare(db, station: dict[str, Any], brief: dict[str, Any] | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """Everything the writer should be told before it invents this song.

    Returns {idea_block, lyric_block, mood, state, licence, seeds}. On any trouble every field is empty and
    the prompts come out exactly as they did before this existed."""
    blank = {"idea_block": "", "lyric_block": "", "mood": (brief or {}).get("mood", ""), "state": {"n": 0},
             "licence": set(), "seeds": [], "example": None, "level": "off"}
    try:
        if not enabled():
            return blank
        rng = rng or random
        lvl = novelty.level(level_name())
        sid = station.get("id") if per_station() else None
        state = memory.state(db, sid, window())
        if not per_station():
            state["station_id"] = station.get("id")

        brief = brief or {}
        # the channel's mood keeps its voice but hands over ONE of its example objects, not the whole list
        used = db.setting(f"mood_examples:{station['id']}", []) or []
        worn = worn_out()
        mood_text, example = guidance.rotate_mood(brief.get("mood", ""), used, rng, worn=worn,
                                                  share=float(_cfg("mood_example_share", 0.35)))
        mood_text = guidance.settle_ratios(mood_text, rng)
        if example:
            db.set_setting(f"mood_examples:{station['id']}", ([example] + [u for u in used if u != example])[:3])

        # words the brief itself asks for are never counted against the song, and that includes the one object
        # the mood just handed over: telling the writer to use a hoodie and to avoid hoodies at once is nonsense
        licence = features.licence_words(brief.get("idea_prompt", ""), brief.get("theme_name", ""),
                                         station.get("name", ""), brief.get("place", ""), example or "")
        # a worn-out word is never licensed, even when a theme or an idea asks for it by name
        licence = {w for w in licence if not worn_hits(w)}

        idea = guidance.for_idea(state, lvl, station, licence, rng)
        # a lane built from real songs can point at a drawer this channel has been ignoring
        if outside_on():
            prof = corpus.profile(db, station)
            gaps = corpus.under_used(prof, state, skip=set(), n=2)
            if gaps:
                names = [BUCKET_LABELS.get(g, g) for g in gaps]
                extra = ("- Songs of this kind usually spread their pictures wider than this channel has been: there is room for "
                         + " and for ".join(names) + ".")
                idea["text"] = (idea["text"] + "\n" + extra) if idea["text"] else ("RECENT WRITING ON THIS CHANNEL (follow this):\n" + extra)
        lyric = guidance.for_lyric(state, lvl, licence)
        return {"idea_block": idea["text"], "lyric_block": lyric["text"], "mood": mood_text, "state": state,
                "licence": licence, "seeds": idea.get("seeds") or [], "example": example, "level": level_name()}
    except Exception:
        LOG.exception("lyric memory: could not prepare guidance for %s", (station or {}).get("id"))
        return blank


# --------------------------------------------------------------------------- after the song is written
def review(db, station: dict[str, Any], lyrics: str, topic: str = "", prep: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score the lyrics against the channel's recent writing. Returns {score, verdict, instruction, reasons}."""
    out = {"score": 1.0, "verdict": "keep", "instruction": "", "reasons": []}
    try:
        if not enabled() or not lyrics:
            return out
        lvl = novelty.level(level_name())
        prep = prep or {}
        state = prep.get("state") or memory.state(db, station.get("id") if per_station() else None, window())
        licence = set(prep.get("licence") or set()) | features.licence_words(topic)
        feat = features.extract(lyrics, topic)
        res = novelty.score(feat, state, lvl, licence)
        verdict = novelty.verdict(res, lvl) if retry_on() else "keep"
        worn = worn_hits(lyrics)
        cliche = novelty.cliche_rhymes(lyrics)
        # made-up street names always; the word street and first names unless the planner left this song room for them
        pp = novelty.people_places(lyrics, prep.get("streets_ok", True), prep.get("names_ok", True), prep.get("place", ""))
        pp_flat = novelty.people_places_flat(pp)
        if worn or cliche or pp_flat:
            # not a judgement call: a banned word or a cliche rhyme gets its lines sent back whatever the
            # score says. If the score alone would NOT have asked for a rewrite, send back only those
            # lines - listing the channel's ordinary habits too is how "one more night" became "one
            # more day" and broke its rhyme with "tight".
            if verdict != "rewrite":
                res["words"], res["phrases"], res["rhymes"] = [], [], []
            res["banned"] = worn
            res["people_places"] = pp
            res["rhymes"] = cliche + [r for r in (res.get("rhymes") or []) if r not in cliche]
            res["reasons"] = ([f"banned words: {', '.join(worn)}"] if worn else []) + \
                             ([f"cliche rhymes: {', '.join(cliche)}"] if cliche else []) + \
                             ([f"streets and names: {', '.join(pp_flat)}"] if pp_flat else []) + list(res.get("reasons") or [])
            verdict = "rewrite"
        return {"score": res["score"], "verdict": verdict, "instruction": novelty.rewrite_instruction(res) if verdict == "rewrite" else "",
                "reasons": res["reasons"], "words": res.get("words") or [], "worn": worn, "cliche": cliche,
                "pp": pp_flat, "people_places": pp}
    except Exception:
        LOG.exception("lyric memory: could not score a lyric for %s", (station or {}).get("id"))
        return out


def idea_again(db, station: dict[str, Any], topic: str, prep: dict[str, Any] | None = None) -> str:
    """Called once with the scene the writer just invented. Returns "" to accept it, or a short instruction to
    invent a different one, when it is nearly the same scene as one this channel has already written."""
    try:
        if not enabled() or not topic:
            return ""
        prep = prep or {}
        state = prep.get("state") or memory.state(db, station.get("id") if per_station() else None, window())
        res = novelty.scene_again(topic, state.get("ideas") or [])
        if not res["same"]:
            return ""
        LOG.info("lyric memory: %s invented a scene it has already written (%.0f%% the same: %s)",
                 station.get("id"), res["overlap"] * 100, ", ".join(res["shared"][:6]))
        return ("That is nearly the same scene this channel has already written, built out of the same things: "
                + ", ".join(res["shared"][:8]) +
                ". Invent a different scene instead: a different place, a different hour, different objects and a "
                "different thing happening. Do not use any of those things again.")
    except Exception:
        LOG.exception("lyric memory: scene check failed for %s", (station or {}).get("id"))
        return ""


def remember(db, song: dict[str, Any]) -> None:
    try:
        memory.remember(db, song)
    except Exception:
        LOG.exception("lyric memory: could not remember %s", (song or {}).get("id"))


# --------------------------------------------------------------------------- covers
def cover_plan(db, station: dict[str, Any], style: str, duration_s: float, row: dict[str, Any] | None = None,
               interpolate: bool | None = None, writer=None, fallback: bool = True) -> dict[str, Any] | None:
    """A real song for this channel to sing, or None. Never raises. `writer` / `fallback`: see covers.plan."""
    try:
        if not outside_on():
            return None
        return covers.plan(db, station, style, duration_s, row=row, interpolate=interpolate, writer=writer, fallback=fallback)
    except Exception:
        LOG.exception("covers: could not plan one for %s", (station or {}).get("id"))
        return None


# --------------------------------------------------------------------------- background work
def backfill_async(db, limit: int = 400) -> None:
    """Read the songs written before this existed into the memory, off the startup path."""
    def run():
        try:
            memory.backfill(db, limit)
        except Exception:
            LOG.exception("lyric memory: backfill thread failed")
    threading.Thread(target=run, daemon=True, name="tf-lyric-backfill").start()


def corpus_async(db, stations: list[dict[str, Any]]) -> None:
    """Grow the outside picture in the background, one request at a time. Does nothing when switched off."""
    if not outside_on():
        return

    def run():
        try:
            corpus.build_all(db, stations, budget=int(_cfg("lrclib_corpus", 500) or 500), keep_text=keep_text())
        except Exception:
            LOG.exception("corpus: background build failed")
    threading.Thread(target=run, daemon=True, name="tf-corpus").start()


# --------------------------------------------------------------------------- reporting
def habits(db, station_id: str | None = None) -> dict[str, Any]:
    try:
        return memory.habits(db, station_id, window())
    except Exception:
        LOG.exception("lyric memory: habits failed")
        return {"songs": 0, "station": station_id, "words": [], "phrases": [], "rhymes": [], "buckets": [],
                "openings": [], "perspectives": []}


def status(db) -> dict[str, Any]:
    try:
        return {
            "level": level_name(), "enabled": enabled(), "window": window(), "retry": retry_on(),
            "per_station": per_station(), "remembered": db.count("lyric_features"),
            "outside": {"enabled": outside_on(), "keep_text": keep_text(), **corpus.status(db)},
            "covers": {"default_chance": _cfg("cover_chance", 0.0), "sung": db.count("songs", "cover_of IS NOT NULL")},
        }
    except Exception:
        LOG.exception("lyric memory: status failed")
        return {"level": level_name(), "enabled": enabled()}
