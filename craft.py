"""craft.py — songwriting craft notes for the lyric writer.

The owner's music-writing guides (a 190 KB lyricism handbook, a Suno genre list, a sound-prompt format, their own preferences) were
distilled into craft/craft_notes.json: 7 "core" cards that always ride along (about 230 words) and ~70 topic cards tagged by
craft area, genre and theme. A song brief pulls the core plus a few matching topic cards, so the 14B model gets the relevant
rules in ~450 tokens instead of the whole book. craft/genres.json (137 genres with what they pair with) feeds the style
suggester. Everything degrades to nothing if the files are missing.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tenforward.craft")
CRAFT_DIR = Path(__file__).resolve().parent / "craft"

_NOTES: dict[str, Any] | None = None
_GENRES: dict[str, Any] | None = None

# genre words in a style line -> card tags
GENRE_TAGS = [
    (re.compile(r"\b(rap|rapper|hip hop|hip-hop|drill|boom bap|grime|g funk|gangsta|horrorcore)\b", re.I), ["hip-hop", "rap"]),  # no "trap": a drum sound on sung pop
    (re.compile(r"\b(country|americana|bluegrass|honky|red dirt|outlaw)\b", re.I), ["country", "storytelling"]),
    (re.compile(r"\b(rock|punk|emo|grunge|metal|hardcore)\b", re.I), ["rock"]),
    (re.compile(r"\b(metal|djent|thrash)\b", re.I), ["metal"]),
    (re.compile(r"\b(punk|emo)\b", re.I), ["punk"]),
    (re.compile(r"\b(pop|synthpop|electropop|dance)\b", re.I), ["pop", "hook"]),
    (re.compile(r"\b(folk|acoustic|singer.songwriter)\b", re.I), ["folk"]),
    (re.compile(r"\b(gospel|worship|hymn|praise)\b", re.I), ["gospel", "faith"]),
    (re.compile(r"\b(r&b|rnb|soul|neo soul)\b", re.I), ["rnb", "soul"]),
    (re.compile(r"\b(blues)\b", re.I), ["blues"]),
    (re.compile(r"\b(jazz|lounge|bossa)\b", re.I), ["jazz"]),
    (re.compile(r"\b(edm|house|techno|dubstep|electro)\b", re.I), ["edm"]),
    (re.compile(r"\b(synthwave|darksynth|retrowave)\b", re.I), ["synthwave"]),
    (re.compile(r"\b(lo-fi|lofi|chillhop)\b", re.I), ["lo-fi"]),
]
# theme ids / names / idea words -> card tags
THEME_TAGS = [
    (re.compile(r"breakup|ex\b|left me|gone|over\b|goodbye|cheat", re.I), ["breakup", "heartbreak"]),
    (re.compile(r"love|crush|kiss|her\b|him\b|date|wedding|forever", re.I), ["love", "longing"]),
    (re.compile(r"summer|lake|beach|sun|tailgate|bonfire", re.I), ["summer", "party"]),
    (re.compile(r"party|drink|club|friday|night out|dance", re.I), ["party"]),
    (re.compile(r"money|cash|check|bank|drip|swag|flex|paid|rich|chain", re.I), ["money", "hustle", "confidence"]),
    (re.compile(r"grind|shift|work|hustle|bills|broke|rent|struggle|overtime", re.I), ["struggle", "hustle"]),
    (re.compile(r"hometown|small town|porch|main street|county|back road|dirt road", re.I), ["small-town", "nostalgia"]),
    (re.compile(r"remember|used to|back then|old|childhood|nostalg", re.I), ["nostalgia"]),
    (re.compile(r"faith|god|church|pray|lord|adonai", re.I), ["faith", "gospel"]),
    (re.compile(r"funny|joke|satire|parody|ridiculous|silly", re.I), ["satire", "humor"]),
    (re.compile(r"grief|funeral|died|passed|lost him|lost her|miss you", re.I), ["grief"]),
    (re.compile(r"angry|mad|fight|revenge|hate|payback", re.I), ["anger"]),
    (re.compile(r"proud|best|winner|champion|boss|crown|confident", re.I), ["confidence"]),
    (re.compile(r"drive|highway|road trip|headlights|truck|car", re.I), ["storytelling", "imagery"]),
]


def _load() -> None:
    global _NOTES, _GENRES
    if _NOTES is None:
        try:
            _NOTES = json.loads((CRAFT_DIR / "craft_notes.json").read_text(encoding="utf-8"))
        except Exception as e:  # missing or broken file: the writer just works without craft notes
            LOG.warning("craft notes unavailable: %s", e)
            _NOTES = {"core": [], "cards": []}
    if _GENRES is None:
        try:
            _GENRES = json.loads((CRAFT_DIR / "genres.json").read_text(encoding="utf-8"))
        except Exception as e:
            LOG.warning("genre list unavailable: %s", e)
            _GENRES = {"genres": [], "families": {}}


def available() -> bool:
    _load()
    return bool(_NOTES and _NOTES.get("core"))


def tags_for(style: str = "", theme_text: str = "", station_text: str = "") -> list[str]:
    """Card tags implied by a style line, a theme (id, name, idea) and the station description."""
    tags: list[str] = []
    for rx, tg in GENRE_TAGS:
        if rx.search(style or ""):
            tags += tg
    text = f"{theme_text} {station_text}"
    for rx, tg in THEME_TAGS:
        if rx.search(text):
            tags += tg
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen


def notes(style: str = "", theme_text: str = "", station_text: str = "", max_cards: int = 3, rng: random.Random | None = None, instrumental: bool = False) -> str:
    """The craft block for a lyric prompt: every core card + up to `max_cards` topic cards matching the song (genre first,
    then theme), each as one line. Empty string when no notes are installed. About 350 to 480 tokens."""
    _load()
    if instrumental or not _NOTES or not _NOTES.get("core"):
        return ""
    rng = rng or random
    want = tags_for(style, theme_text, station_text)
    cards = list(_NOTES.get("cards") or [])
    picked: list[dict[str, Any]] = []
    # genre cards first (they change the writing the most), then theme cards, then craft fundamentals for variety
    genre_tags = [t for t in want if t in ("hip-hop", "rap", "country", "rock", "metal", "punk", "pop", "folk", "gospel", "rnb", "soul", "blues", "jazz", "edm", "synthwave", "lo-fi")]
    theme_tags = [t for t in want if t not in genre_tags]
    for group in (genre_tags, theme_tags):
        pool = [c for c in cards if c not in picked and any(t in (c.get("tags") or []) for t in group)]
        rng.shuffle(pool)
        # prefer cards that match more than one wanted tag
        pool.sort(key=lambda c: -sum(1 for t in group if t in (c.get("tags") or [])))
        for c in pool[: max(0, (max_cards + 1) // 2 if group is genre_tags else max_cards - len(picked))]:
            if len(picked) < max_cards:
                picked.append(c)
    if len(picked) < max_cards:
        pool = [c for c in cards if c not in picked and any(t in (c.get("tags") or []) for t in ("imagery", "rhyme", "hook", "storytelling", "cliche"))]
        rng.shuffle(pool)
        picked += pool[: max_cards - len(picked)]
    lines = ["Songwriting craft (follow these; they come from the owner's own writing guides):"]
    for c in _NOTES["core"]:
        lines.append(f"- {c['title']}: {c['text']}")
    for c in picked:
        lines.append(f"- {c['title']}: {c['text']}")
    return "\n".join(lines)


def genre_pairs(genre: str, n: int = 4) -> list[str]:
    """Styles that pair well with a genre (from the Suno genre list), for the style suggester."""
    _load()
    g = (genre or "").strip().lower()
    for row in _GENRES.get("genres") or []:
        if row.get("genre", "").lower() == g:
            return (row.get("pairs_with") or [])[:n]
    for row in _GENRES.get("genres") or []:
        if g and (g in row.get("genre", "").lower() or row.get("genre", "").lower() in g):
            return (row.get("pairs_with") or [])[:n]
    return []


def genre_names() -> list[str]:
    _load()
    return [row["genre"] for row in _GENRES.get("genres") or [] if row.get("genre")]


def families() -> dict[str, list[str]]:
    _load()
    return dict(_GENRES.get("families") or {})


def summary() -> dict[str, Any]:
    _load()
    return {"core": len(_NOTES.get("core") or []), "cards": len(_NOTES.get("cards") or []), "genres": len(_GENRES.get("genres") or []), "dir": str(CRAFT_DIR)}
