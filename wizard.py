"""wizard.py — a whole channel out of one plain-English sentence.

The channel editor has thirty boxes in it. What a person actually wants to say is "a late night hip hop channel
like Nas and Wu-Tang, a bit rough", so this module says that to the writer once and builds the channel out of
the answer: a name, the lane sentence the rest of the radio reads acts out of, a set of style lines, what it
sings about, its mood, its colour.

One model call, and only when somebody asks for a channel — nothing here runs while the radio is planning songs.
Everything that comes back is checked and repaired in python (style lines get their house format, invented theme
ids are dropped, a mood is rebuilt into the shape the lyric memory can rotate), so a poor answer still gives a
usable channel. If the writer cannot be reached at all there is a plain draft built from the words themselves.

Nothing here saves anything. It returns a draft in exactly the shape POST /api/stations takes, and a person
looks at it before it becomes a channel.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import llm
import themes

try:
    import craft
except Exception:  # pragma: no cover
    craft = None

LOG = logging.getLogger("tenforward.wizard")

PALETTE = ["orange", "peach", "gold", "lav", "blue", "teal", "green", "salmon", "pink", "red"]
RESERVED = {"all", "new", "none", "api", "favorites"}
MAX_TEXT = 1200

VOCAL = re.compile(r"\b(vocals?|voice|singer|sung|falsetto|tenor|soprano|alto|baritone|rapper|choir|harmonies|ad libs|"
                   r"male|female|spoken word|verse|chorus)\b", re.I)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


# --------------------------------------------------------------------------- small helpers
def _clean(text: Any, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip().strip('"').strip()
    return s[:limit]


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def free_id(db, name: str) -> str:
    """A station id nothing else is using. The name is what the person sees; this is what the folders are called."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "channel").lower()).strip("-") or "channel"
    if base in RESERVED:
        base += "-radio"
    sid, n = base, 2
    while db.get("stations", sid) is not None:
        sid, n = f"{base}-{n}", n + 1
    return sid


def _json_from(text: str) -> dict[str, Any] | None:
    """The object in the answer, whatever the model wrapped it in."""
    t = re.sub(r"```(?:json)?", "", str(text or "")).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    chunk = t[a:b + 1]
    for attempt in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
        try:
            out = json.loads(attempt)
            if isinstance(out, dict):
                return out
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- repairing what came back
def _items(raw: Any, commas: bool = False) -> list[str]:
    """A list of strings, however the model chose to send it: a real list, a list of little objects, or all of
    it inside ONE string with newlines in it (which a plain loop would walk letter by letter)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [x for x in raw.splitlines() if x.strip()]
        if commas and len(parts) <= 1:
            parts = raw.split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list[str] = []
    for x in raw:
        if isinstance(x, dict):
            x = next((v for v in x.values() if isinstance(v, str)), "")
        if isinstance(x, (list, tuple)):
            out.extend(str(i).strip() for i in x if str(i).strip())
        elif str(x).strip():
            out.append(str(x).strip())
    return out


def style_lines(raw: Any, instrumental: bool) -> list[str]:
    """The house format: sung lines lead with the language, instrumental lines say so and carry no voice."""
    out: list[str] = []
    for s in _items(raw):
        s = re.sub(r"\s+", " ", BULLET.sub("", str(s or "").strip().strip('"'))).strip(" ,.")
        if len(s) < 18:
            continue
        if instrumental:
            parts = [p.strip() for p in re.sub(r"^(english|instrumental)\s*,\s*", "", s, flags=re.I).split(",")]
            parts = [p for p in parts if p and not VOCAL.search(p)]
            if len(parts) < 2:
                continue
            s = "Instrumental, " + ", ".join(parts)
        elif not s.lower().startswith(("english", "spanish", "french", "german", "italian", "japanese", "korean", "portuguese")):
            s = "English, " + s
        s = s[:220]
        if s.lower() not in [o.lower() for o in out]:
            out.append(s)
    return out[:12]


def theme_list(raw: Any) -> list[str]:
    """Catalog ids and groups are kept as they are; a phrase becomes a theme of its own (themes.adhoc_theme).
    An id the model invented is dropped, because nothing would ever match it."""
    ids = {t["id"] for t in themes.THEMES}
    by_name = {t["name"].lower(): t["id"] for t in themes.THEMES}
    kinds = set(themes.THEME_KINDS)
    out: list[str] = []
    for w in _items(raw, commas=True):
        w = re.sub(r"\s+", " ", str(w or "").strip().strip('"').strip(" .;"))
        if not w:
            continue
        low = w.lower()
        pick = low if low in ids or low in kinds else by_name.get(low)
        if pick is None and " " in w and 4 <= len(w) <= 60:
            pick = w                      # "drive by shootings", "Good day" — what is typed is what it sings about
        if pick and pick not in out:
            out.append(pick)
    return out[:14]


def mood_line(attitude: Any, objects: Any) -> str:
    """The mood the writer keeps for every song, with a bracketed list of objects when there are enough of them.

    The bracket matters: lyric_intel hands ONE of those objects to each song and rotates them, which is how a
    channel avoids the hoodie-in-every-song problem. It is only worth writing when the list is long enough for
    the rotation to find it, so the shape is checked here rather than hoped for."""
    a = _clean(attitude, 200).strip(" .;")
    objs = [_clean(o, 48).strip(" .;") for o in _items(objects, commas=True)]
    objs = [o for o in objs if 6 <= len(o) <= 48][:5]
    if not a:
        return ""
    if len(objs) >= 3 and len(", ".join(objs)) >= 26:
        line = f"{a}; one concrete object per verse ({', '.join(objs)})"[:300]
        try:
            from lyric_intel import guidance
            if len(guidance.mood_examples(themes.mood_line({"mood": line}))) >= 3:
                return line
        except Exception:
            return line
    return a[:300]


def _artists(raw: Any) -> list[str]:
    out: list[str] = []
    for a in _items(raw, commas=True):
        a = _clean(a, 40).strip(" .,;")
        if 2 <= len(a) <= 40 and re.search(r"[A-Za-z]", a) and a.lower() not in [o.lower() for o in out]:
            out.append(a)
    return out[:12]


def channel_name(raw: Any) -> str:
    """Two or three words with spaces in them. Models like to jam a name together (NightDrones); that is pulled
    apart again, because it is what somebody reads on the dial."""
    n = _clean(raw, 40).strip(" .,;:-")
    n = re.sub(r"^(?:the\s+)?(.*?)\s+radio$", r"\1", n, flags=re.I) or n
    if " " not in n and re.search(r"[a-z][A-Z]", n):
        n = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n)
    return n[:40]


def _colour(raw: Any, name: str) -> str:
    c = _clean(raw, 20).lower()
    if c in PALETTE:
        return c
    return PALETTE[sum(ord(ch) for ch in (name or "x")) % len(PALETTE)]


# --------------------------------------------------------------------------- the one call
EXAMPLE_LINES = (
    "Two lines from another channel, for the shape only:\n"
    "  English, lounge jazz, smoky female alto, brushed drums, upright bass, muted trumpet, room reverb, 92 BPM\n"
    "  English, outlaw country, weathered male baritone, pedal steel, telecaster, upright piano, tape saturation, 78 BPM"
)

SYSTEM = (
    "You set up channels for a radio station that writes and sings its own songs. You are given a person's own "
    "words about the channel they want and you answer with ONE json object and nothing else.\n\n"
    "Keys:\n"
    '  name          the name on the dial: two or three ordinary words with spaces between them. Never one word '
    'jammed together, no word "radio", no punctuation.\n'
    '  description   one or two plain sentences: what it sounds like and what its songs are about. When acts are '
    'named or clearly implied, say "in the lane of A, B and C" inside it, because the radio reads the acts back out '
    'of this sentence. Plain words only: no sales talk, no "sonic journey", no "perfect for".\n'
    "  artists       the acts it is in the lane of, 3 to 8 of them, real ones that fit what was asked for.\n"
    "  styles        8 to 11 lines describing the SOUND for a music model, one per song it might make. Format: "
    "\"English, <genre>, <the voice>, <three or four instruments and production words>, <NN> BPM\". They are all "
    "plainly the same channel, but NO TWO LINES MAY NAME THE SAME INSTRUMENTS: change the sub-genre, the voice, the "
    "instruments, the production and the tempo down the list. Naming guitar, bass and drums in every line is a bad "
    "answer.\n"
    "  mood          a short clause of attitude the writer keeps for every song. No object names in it.\n"
    "  objects       five different ordinary things this channel's songs can be built around, each a short phrase "
    "like \"a receipt in a jacket pocket\" or \"the light over a pool table\". Five separate things, not one thing "
    "five ways. Ordinary, not poetic.\n"
    "  themes        6 to 12 of what it sings about, taken from the list of ids you are given. You may add at most "
    "two of your own as plain phrases (\"working a double shift\"), never as invented ids.\n"
    "  sound_set     a named sound set ONLY when the channel really is that kind of music; \"\" is the usual answer "
    "and a wrong one changes how the songs sound.\n"
    "  color         one of the named colours.\n"
    "  male_percent  0 to 100: how much of the singing is by a man. 50 when it does not matter.\n"
    "  explicit      true only when swearing is plainly part of what was asked for.\n\n"
    + EXAMPLE_LINES +
    "\n\nWrite for the music, not about the radio: no meta talk, no lyrics, no explanation, no markdown."
)

SYSTEM_INST = (
    "You set up channels for a radio station that plays its own instrumental music. You are given a person's own "
    "words about the channel they want and you answer with ONE json object and nothing else.\n\n"
    "Keys:\n"
    '  name          the name on the dial: two or three ordinary words with spaces between them. Never one word '
    'jammed together, no word "radio", no punctuation.\n'
    "  description   one or two plain sentences: what it sounds like and what it is for. When acts are named or clearly "
    'implied, say "in the lane of A, B and C" inside it.\n'
    "  artists       the acts it is in the lane of, 3 to 8 of them, real ones that fit.\n"
    "  styles        8 to 11 lines describing the SOUND for a music model. Format: \"Instrumental, <genre>, "
    "<three or four instruments and production words>, <NN> BPM\". They are all plainly the same channel, but NO "
    "TWO LINES MAY NAME THE SAME INSTRUMENTS: change the sub-genre, the instruments, the production and the tempo "
    "down the list. There is no singing here: never name a voice, a singer or vocals.\n"
    "  sound_set     a named sound set ONLY when the channel really is that kind of music; \"\" is the usual answer "
    "and a wrong one changes how the tracks sound.\n"
    "  color         one of the named colours.\n\n"
    "No meta talk, no explanation, no markdown."
)


def _user_block(text: str, instrumental: bool, explicit: bool | None) -> str:
    lines = [f"What they asked for, in their own words:\n{text.strip()[:MAX_TEXT]}", ""]
    sets = [f"{k} ({', '.join(v.get('fusions', [])[:3])})" for k, v in themes.FUSION_SETS.items()]
    lines.append("Sound sets to choose from, or \"\" for none: " + "; ".join(sets) + ".")
    lines.append("Colours to choose from: " + ", ".join(PALETTE) + ".")
    if not instrumental:
        lines.append("Theme ids to choose from: " + ", ".join(t["id"] for t in themes.THEMES) + ".")
        lines.append("Whole groups may be used instead of single ids: " + ", ".join(themes.THEME_KINDS) + ".")
        if explicit:
            lines.append("They have already said swearing is allowed on this channel, so set explicit to true.")
    if craft is not None:
        try:
            low = text.lower()
            hits = [g for g in craft.genre_names() if g in low][:4]
            if hits:
                pairs = craft.genre_pairs(hits[0]) or []
                lines.append(f"Genres they named: {', '.join(hits)}." +
                             (f" Genres that pair well with {hits[0]}: {', '.join(pairs[:4])}." if pairs else ""))
        except Exception:
            pass
    return "\n".join(lines)


# --------------------------------------------------------------------------- the plain draft, when nothing answers
def _plain(text: str, instrumental: bool) -> dict[str, Any]:
    """No model: a channel built out of the words themselves. Rough, but it plays."""
    words = [w for w in re.findall(r"[A-Za-z']+", text) if len(w) > 3]
    name = " ".join(w.capitalize() for w in words[:2]) or "New Channel"
    genre = ""
    if craft is not None:
        try:
            low = text.lower()
            genre = next((g for g in craft.genre_names() if g in low), "")
        except Exception:
            genre = ""
    genre = genre or ("ambient" if instrumental else "pop")
    beds = [("warm", 90, "felt piano, soft pads, brushed percussion"), ("bright", 110, "clean guitar, electric piano, light drums"),
            ("slow", 76, "strings, low synth, long reverb"), ("driving", 124, "synth bass, arpeggios, tight drums"),
            ("sparse", 100, "single piano, room tone, no percussion"), ("full", 84, "acoustic guitar, upright bass, organ")]
    if instrumental:
        styles = [f"Instrumental, {genre}, {feel}, {kit}, {bpm} BPM" for feel, bpm, kit in beds]
    else:
        styles = [f"English, {genre}, {feel} lead vocal, {kit}, {bpm} BPM" for feel, bpm, kit in beds]
    return {"name": name, "description": _clean(text, 400), "artists": [], "styles": styles, "themes": [],
            "mood": "", "objects": [], "sound_set": "", "color": "", "male_percent": None, "explicit": False}


# --------------------------------------------------------------------------- the draft
def draft(db, text: str, instrumental: bool = False, cover_chance: float | None = None, explicit: bool | None = None,
          minutes: float | None = None, keep_ahead: int | None = None, enabled: bool = True,
          auto_generate: bool = True, duration_default: int = 180, keep_default: int = 2) -> dict[str, Any]:
    """Plain English in, a channel out. Returns {station, notes, artists, source}; nothing is saved."""
    text = _clean(text, MAX_TEXT * 2)
    notes: list[str] = []
    source = "writer"
    try:
        answer = llm.ask_json(SYSTEM_INST if instrumental else SYSTEM,
                              _user_block(text, instrumental, explicit))
        spec = _json_from(answer)
        if not spec:
            LOG.warning("wizard: the answer had no json in it (%d chars)", len(answer or ""))
            spec, source = _plain(text, instrumental), "plain"
            notes.append("The writer answered with something that was not a channel, so this is a plain draft. "
                         "Try again, or fill it in yourself.")
    except Exception as e:
        LOG.warning("wizard: the writer did not answer: %s", e)
        spec, source = _plain(text, instrumental), "plain"
        notes.append("The writer did not answer, so this is a plain draft built from your own words. "
                     "Try again in a moment, or fill it in yourself.")

    name = channel_name(spec.get("name")) or "New Channel"
    styles = style_lines(spec.get("styles"), instrumental)
    if len(styles) < 3:
        styles = (styles + _plain(text, instrumental)["styles"])[:8]
        notes.append("It only wrote a couple of usable style lines, so a few plain ones were added.")
    artists = _artists(spec.get("artists"))
    desc = _clean(spec.get("description"), 600) or _clean(text, 600)
    if artists and "lane of" not in desc.lower():
        desc = desc.rstrip(". ") + ". In the lane of " + _join(artists) + "."

    sid = free_id(db, name)
    want_explicit = bool(explicit) if explicit is not None else bool(spec.get("explicit"))
    fusion = _clean(spec.get("sound_set"), 24).lower()
    station: dict[str, Any] = {
        "id": sid,
        "name": name,
        "description": desc[:600],
        "style_prompts": styles,
        "themes": [] if instrumental else theme_list(spec.get("themes")),
        "mood": "" if instrumental else mood_line(spec.get("mood"), spec.get("objects")),
        "color": _colour(spec.get("color"), name),
        "fusion_set": fusion if fusion in themes.FUSION_SETS else "",
        "instrumental": 1 if instrumental else 0,
        "explicit": 1 if (want_explicit and not instrumental) else 0,
        "enabled": 1 if enabled else 0,
        "auto_generate": 1 if auto_generate else 0,
        "variation": 1,
        "replay_policy": "fresh",
        "mode": 0,
        "duration_s": int(max(60, min(600, round((minutes or 0) * 60)))) if minutes else int(duration_default),
        "keep_ahead": int(max(0, min(6, keep_ahead))) if keep_ahead is not None else int(keep_default),
        "banned_topics": "",
        "male_ratio": None,
        "cover_chance": None,
    }
    if not instrumental:
        try:
            mp = spec.get("male_percent")
            if mp is not None and str(mp).strip() != "":
                station["male_ratio"] = max(0.0, min(1.0, float(mp) / 100.0))
        except (TypeError, ValueError):
            pass
        if cover_chance:
            station["cover_chance"] = max(0.0, min(1.0, float(cover_chance)))
    elif cover_chance:
        notes.append("An instrumental channel has nothing to sing, so the covers share was left off.")

    if artists:
        notes.append("It will draw on " + _join(artists[:6]) + (" and a few more." if len(artists) > 6 else "."))
    if station["explicit"]:
        notes.append("Swearing is allowed on this channel.")
    if not station["themes"] and not instrumental:
        notes.append("No themes were picked, so it can sing about anything in the catalog.")
    return {"station": station, "notes": notes, "artists": artists, "source": source}
