"""guidance.py — the short block of instructions the analyser writes for the lyric writer.

Two rules shape everything here. A list of forbidden words primes a model as much as it forbids it, so the
block is always mostly POSITIVE: here is a drawer you have not opened lately, open that one. And a station's
own mood line is rotated rather than repeated whole, because a mood that lists five example objects every
single time is how a channel ends up with a hoodie in two songs out of three.
"""
from __future__ import annotations

import random
import re
from typing import Any

from . import novelty
from .lexicon import BUCKETS, BUCKET_BANS, BUCKET_LABELS, seed_buckets

# "one concrete object per verse (a hoodie that is not mine, a streetlight through the blinds, the fridge light)"
EXAMPLE_LIST = re.compile(r"\(([^()]{25,400})\)")

OPENING_ADVICE = {
    "first-person": "opening on the word I",
    "second-person": "opening on the word you",
    "third-person": "opening on he or she",
    "scene": "opening on a place",
    "weather-time": "opening on the weather or the hour",
    "question": "opening with a question",
    "dialogue": "opening on a line of speech",
    "action": "opening on an action",
}
OPENING_ASK = {
    "first-person": "start the first line with what the singer is doing, not with the word I",
    "second-person": "start on an object in the room instead of the word you",
    "third-person": "start on a thing rather than a person",
    "scene": "start on somebody moving rather than on a place",
    "weather-time": "start on an object in somebody's hands, not the weather, the sky or the hour",
    "question": "start on a statement rather than a question",
    "dialogue": "start on a picture rather than on speech",
    "action": "start on a place or an object",
}


# --------------------------------------------------------------------------- the station's own mood line
def _example_match(mood: str):
    """The bracket in a mood that is actually a list of example objects.

    Not simply the first bracket: what reaches the writer is `themes.mood_line()`, which wraps the mood in a
    sentence with brackets of its own ("(show it, never say these words)"). The example list is the bracket
    with the most things in it."""
    best = None
    for m in EXAMPLE_LIST.finditer(mood or ""):
        parts = [p.strip(" .;") for p in m.group(1).split(",")]
        parts = [p for p in parts if len(p) > 4]
        if len(parts) >= 3 and (best is None or len(parts) > len(best[1])):
            best = (m, parts)
    return best


def mood_examples(mood: str) -> list[str]:
    """The bracketed 'for example' list inside a station mood, if it has one."""
    best = _example_match(mood)
    return best[1] if best else []


OWN_OBJECT = ("(this song's one detail: something real from this person's life that they would say out loud - a name, a place, "
              "a car, a job, a person - not a household object and not a light)")


def _is_worn(text: str, worn: list[str] | tuple[str, ...]) -> bool:
    return any(novelty.worn_pattern(w).search(text or "") for w in worn if w)


def rotate_mood(mood: str, used: list[str] | None = None, rng: random.Random | None = None,
                worn: list[str] | tuple[str, ...] = (), share: float = 1.0) -> tuple[str, str | None]:
    """A mood that lists examples gives at most ONE of them per song, skipping the ones the last few
    songs used. Returns (mood line to send, the example chosen or None).

    Only about `share` of songs get a named example at all. Handing one to every song meant a
    five-item list guaranteed each object a turn every ~5 songs - 20% of everything a channel wrote -
    which is how "a hoodie that is not mine" became Blue Hour's signature. The rest are told to
    choose their own. An example containing a worn-out word is never handed over."""
    rng = rng or random
    best = _example_match(mood)
    if not best:
        return mood, None
    m, parts = best
    parts = [p for p in parts if not _is_worn(p, worn)]
    if not parts or rng.random() >= max(0.0, min(1.0, share)):
        return mood[:m.start()] + OWN_OBJECT + mood[m.end():], None
    used = [u for u in (used or []) if u]
    fresh = [p for p in parts if p not in used] or parts
    pick = rng.choice(fresh)
    return mood[:m.start()] + f"(this song's one detail: {pick})" + mood[m.end():], pick


_RATIO = re.compile(r"([A-Za-z][^;,()]*?)\s+in one song out of (\w+) at most", re.I)
_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10}


def settle_ratios(mood: str, rng: random.Random | None = None) -> str:
    """"phones and texting in one song out of four at most" -> decided for THIS song.

    A ratio across songs is not something a writer that sees one song at a time can keep, and it
    did not: Blue Hour had phones in 58% of songs against a stated 25%. So roll it here, once per
    song, and hand over a plain yes or no."""
    rng = rng or random

    def one(mt: re.Match) -> str:
        thing, n = mt.group(1).strip(), mt.group(2).lower()
        n = _NUMBERS.get(n) or (int(n) if n.isdigit() else 0)
        if n < 2:
            return mt.group(0)
        return f"{thing} may come into this song" if rng.random() < 1.0 / n else f"leave {thing} out of this song entirely"

    return _RATIO.sub(one, mood or "")


# --------------------------------------------------------------------------- the blocks
def _banned_buckets(station: dict[str, Any]) -> set[str]:
    try:
        import themes
        bans = set(themes.banned_list(station or {}))
    except Exception:
        bans = set()
    return {b for b, topic in BUCKET_BANS.items() if topic and topic in bans}


# Words the drawers keep for MEASURING a song, but that must never be suggested TO the writer: every
# one is set dressing rather than something a person says. Suggesting "the kitchen (freezer, faucet,
# toaster)" or "openings (blinds, latch, threshold)" is how writing-workshop props got into the songs.
SEED_SKIP = set("""
    fridge refrigerator freezer faucet toaster microwave kettle crumbs countertop napkin
    blinds curtain curtains shutter shutters latch keyhole threshold doorbell porchlight screen
    flicker flickers flickering glow glowing gleam beam beams lantern flashlight bulb neon streetlight
    streetlights dashboardlight moonlight firelight shine shining spark sparks lamp lamps
    shadow shadows silhouette gloom dim twilight dusk darkness blackout
    mirror nightstand dresser drawer drawers rug carpet shelf shelves pillow pillows sheets
    odometer ignition clutch gearshift wiper wipers windshield bumper dashboard
    hem cuff cuffs zipper collar sleeve sleeves apron
    thermos flask sips sip brew
    receipt unread notification inbox scroll typing seen
    clock alarm oclock minute minutes
    spine ribs wrist wrists palm lashes eyebrows freckles glance blink stare
    """.split())


def seeds(state: dict[str, Any], station: dict[str, Any], n: int = 1, rng: random.Random | None = None) -> list[str]:
    """Image drawers this channel has barely opened lately, as a sentence fragment with a few examples."""
    rng = rng or random
    shares = state.get("buckets") or {}
    skip = _banned_buckets(station)
    pool = [b for b in seed_buckets() if b not in skip]
    pool.sort(key=lambda b: shares.get(b, 0.0))
    take = pool[:max(6, n * 4)]
    rng.shuffle(take)
    out = []
    for b in take[:n]:
        worn = novelty.worn_words()
        words = sorted(w for w in BUCKETS[b] if w not in SEED_SKIP and not _is_worn(w, worn))
        if len(words) < 3:
            continue                     # a drawer that is nothing but set dressing is not worth suggesting
        picked = rng.sample(words, k=3)
        out.append(f"{BUCKET_LABELS.get(b, b)} ({', '.join(picked)})")
    return out


def for_idea(state: dict[str, Any], lvl: dict[str, Any], station: dict[str, Any], licence: set[str] | None = None,
             rng: random.Random | None = None) -> dict[str, Any]:
    """The block that goes to the call that invents the scene. Positive first."""
    if not lvl or not state.get("n"):
        return {"text": "", "seeds": [], "avoid": []}
    rng = rng or random
    avoid = novelty.leaning_on(state, lvl, licence)
    picks = seeds(state, station, lvl.get("seeds", 1), rng)
    lines = []
    if picks:
        lines.append("Build this scene around " + " and around ".join(picks) + ". Those are a starting point, not a list to tick off.")
    if avoid:
        lines.append("This channel has used these too often lately, so keep them out of the idea unless the theme itself "
                     "asks for one: " + ", ".join(avoid) + ".")
    opens = state.get("openings") or {}
    if opens:
        common, count = max(opens.items(), key=lambda x: x[1])
        if count / max(1, state["n"]) > 0.55 and common in OPENING_ASK:
            lines.append("The recent songs nearly all end up " + OPENING_ADVICE.get(common, "the same way") +
                         ", so put this scene somewhere that does not lead there.")
    persp = state.get("perspectives") or {}
    if persp:
        top, count = max(persp.items(), key=lambda x: x[1])
        if top == "first" and count / max(1, state["n"]) > 0.8:
            lines.append("Almost every recent song is one person talking about themselves; this idea can watch somebody else "
                         "for a change, or put two people in the room.")
    if not lines:
        return {"text": "", "seeds": picks, "avoid": avoid}
    return {"text": "RECENT WRITING ON THIS CHANNEL (follow this):\n" + "\n".join("- " + l for l in lines),
            "seeds": picks, "avoid": avoid}


def for_lyric(state: dict[str, Any], lvl: dict[str, Any], licence: set[str] | None = None) -> dict[str, Any]:
    """The block that goes to the call that writes the words."""
    if not lvl or not state.get("n"):
        return {"text": "", "avoid": [], "phrases": [], "rhymes": []}
    avoid = novelty.leaning_on(state, lvl, licence)
    phrases = novelty.worn_phrases(state, lvl)
    rhymes = novelty.worn_rhymes(state, lvl)
    lines = []
    if avoid:
        lines.append("Words this channel has worn out lately. Use a different concrete thing instead, unless the idea above "
                     "already names one of them: " + ", ".join(avoid) + ".")
    if phrases:
        lines.append("Lines already written on this channel, so say it another way: " + "; ".join(f'"{p}"' for p in phrases) + ".")
    if rhymes:
        lines.append("Rhymes leaned on too often, so end those lines somewhere else: "
                     + ", ".join(r.replace("/", " / ") for r in rhymes) + ".")
    opens = state.get("opening_words") or {}
    if opens:
        top, count = max(opens.items(), key=lambda x: x[1])
        if count >= 2:
            lines.append(f'Recent songs have started with "{top}", so open on something else entirely.')
    if not lines:
        return {"text": "", "avoid": avoid, "phrases": phrases, "rhymes": rhymes}
    return {"text": "RECENT LANGUAGE GUIDANCE (this channel, not a general rule):\n" + "\n".join("- " + l for l in lines),
            "avoid": avoid, "phrases": phrases, "rhymes": rhymes}
