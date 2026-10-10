"""themes.py — what radio songs are ABOUT, who sings them, and how instrumental prompts mutate.

Stations decide the sound (style prompts). Themes decide the subject. Every invented vocal song gets:
  * a theme (love, breakup, summer, money, the grind, night drive ...), weighted, never the same as the last few
  * a singer gender read from the style prompt, so a male voice sings about a woman and a female voice about a man
  * content rules: no politics, no sports (the radio's rule), plus any per-station bans
Instrumental stations get a fresh style line every song: base sound + 1..3 rotating fusions + tempo + texture.
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tenforward.themes")

DEFAULT_BANNED = ["politics", "sports"]

BANNED_TEXT = {
    "politics": "politics, politicians, parties, elections, voting, government, laws, presidents, protests, war, soldiers, the news, flags or anything patriotic",
    "sports": "sports, teams, leagues, athletes, games, scores, coaches, stadiums or trophies",
    "religion": "religion, God, church, prayer, heaven or hell",
    "drugs": "drugs, weed, pills, getting high",
    "alcohol": "drinking, beer, whiskey, bars, being drunk",
    "profanity": "swear words",
    "death": "death, funerals, graves or dying",
}

# {singer} / {partner} / {they} / {them} / {their} are filled from the singer gender.
THEMES: list[dict[str, Any]] = [
    {"id": "love-new", "name": "New love", "weight": 10, "kind": "love",
     "brief": "{Singer} has just started falling for {partner}. A specific first moment: a ride home that took the long way, a dumb joke that landed, the first time {partner_they} called instead of texting. Sung to or about {them}."},
    {"id": "love-deep", "name": "Staying love", "weight": 7, "kind": "love",
     "brief": "{Singer} loves {partner} they have been with a long time. Ordinary proof: coffee made right, a hand on the back, keys on the hook. Sung to {them}."},
    {"id": "love-far", "name": "Long distance", "weight": 5, "kind": "love",
     "brief": "{Singer} misses {partner} who is away for a while (work, school, a trip, a different city). Phones, time zones, a countdown. Sung to {them}. Nobody died."},
    {"id": "breakup", "name": "Breakup", "weight": 10, "kind": "love",
     "brief": "{Singer} and {partner} are over. The day it ended and what actually happened: who said it, where they were, and what got taken or left behind. Sung about or to {them}."},
    {"id": "breakup-better", "name": "Better off", "weight": 6, "kind": "love",
     "brief": "{Singer} is over {partner} and doing better: new routine, new city, a song {they} would hate. Confident, not bitter."},
    {"id": "reunion", "name": "Ran into you", "weight": 4, "kind": "love",
     "brief": "{Singer} runs into {partner} (an ex) somewhere ordinary: a grocery aisle, a gas pump, a wedding. What is said and what is not."},
    {"id": "waiting", "name": "Waiting on a text", "weight": 5, "kind": "love",
     "brief": "{Singer} is waiting for {partner} to text back. Phone face down, face up, the three dots. Sung to {them}."},
    {"id": "summer-love", "name": "Summer love", "weight": 8, "kind": "summer",
     "brief": "{Singer} and {partner} in one summer. Pick ONE setting and stay in it: a lake, a county fair, a boardwalk, a back road, a rooftop, a pool after it closes, a drive-in, a diner at 2 a.m., a porch in a thunderstorm. One object that becomes theirs (a lighter, a photo strip, a hair tie on a gear shift, a borrowed cassette). Sung to {them}."},
    {"id": "summer-fun", "name": "Summer fun", "weight": 7, "kind": "summer",
     "brief": "A summer day with friends in ONE place: a boat, a river float, the fair, a bonfire, a backyard sprinkler, a parking lot show, a rope swing, a water tower climb, a gas station on the way. Coolers and fireworks at most once, not every time. No politics, no teams."},
    {"id": "summer-night", "name": "Summer night", "weight": 6, "kind": "summer",
     "brief": "One summer night after dark: a late swim nobody planned, fireflies, the drive home with the windows down and the heat still in the seats. {Singer} and whoever is there, and what they got up to."},
    {"id": "summer-end", "name": "End of summer", "weight": 5, "kind": "summer",
     "brief": "The last weekend of summer: a lake house closing up, a car packed for school, a lifeguard chair stacked, a promise about next year that both of them know. {Singer} to {partner} or to a friend."},
    {"id": "heat-wave", "name": "Heat wave", "weight": 4, "kind": "summer",
     "brief": "A heat wave: box fans in the windows, a gas station slushie, the pool that is always full, sleeping with the door open, {singer} and {partner} too hot to argue. Sticky, funny, slow."},
    {"id": "summer-job", "name": "Summer job", "weight": 5, "kind": "summer",
     "brief": "{Singer}'s summer job: an ice cream stand, a lifeguard chair, a mowing route, a boat rental dock, a fireworks tent. The regulars, the tip jar, the sunburn, and {partner}, a regular who finally says more than an order."},
    {"id": "vacation", "name": "One week away", "weight": 4, "kind": "summer",
     "brief": "A week away: a motel pool, a boardwalk, a rental car with sand in it, a photo booth strip, the wrong exit. {Singer} with {partner} or the crew; the trip is the story."},
    {"id": "summer-crush", "name": "Summer crush", "weight": 6, "kind": "summer",
     "brief": "{Singer} has a crush that only lasts the summer: the counter at the ice cream place, the neighbor's cousin visiting for July, the lifeguard on the late shift. The day {singer} finally does something about it, and how that went. Light, funny, over by September."},
    {"id": "money-up", "name": "Getting money", "weight": 7, "kind": "money",
     "brief": "{Singer} is finally getting money: a check that clears, new chains, new drip, a car with the tags still fresh, paying back the people who held {them} down. Flexing, specific brands and numbers allowed."},
    {"id": "drip", "name": "Drip", "weight": 5, "kind": "money",
     "brief": "{Singer} on how {they} dresses and moves: fits, shoes, watch, the way the room turns. Swagger, humor, specifics."},
    {"id": "grind", "name": "The grind", "weight": 8, "kind": "money",
     "brief": "{Singer} is broke and working: overtime, a second job, a bill on the counter, a check that does not stretch, still standing. Personal money only, no politics, nobody to blame but the math."},
    {"id": "self-made", "name": "Self made", "weight": 5, "kind": "money",
     "brief": "{Singer} looks back at where {they} started and where {they} is now. Old apartment, old car, the people who doubted. Earned, not given."},
    {"id": "night-drive", "name": "Night drive", "weight": 7, "kind": "scene",
     "brief": "{Singer} driving alone at night: dashboard light, gas station glow, a highway with nobody on it, a town asleep. Where {they} is going or what {they} is leaving."},
    {"id": "hometown", "name": "Hometown", "weight": 6, "kind": "scene",
     "brief": "{Singer} back in the small town {they} grew up in: the water tower, the one stoplight, a diner, someone who still knows {their} name."},
    {"id": "party", "name": "Friday night", "weight": 6, "kind": "scene",
     "brief": "Friday night out: the pregame, the ride, the floor, the lights, someone {singer} keeps looking for. Fun, loud, alive."},
    {"id": "morning-after", "name": "Morning after", "weight": 4, "kind": "scene",
     "brief": "{Singer} the morning after a big night: sunglasses inside, a text {they} should not have sent, coffee, piecing it together. Light, funny."},
    {"id": "road-trip", "name": "Road trip", "weight": 5, "kind": "scene",
     "brief": "{Singer} and friends on a road trip: a bad map, a gas station breakfast, a playlist fight, a state line at dawn."},
    {"id": "crew", "name": "The crew", "weight": 5, "kind": "scene",
     "brief": "{Singer} on {their} friends: the ones who show up with a truck when you move, who know the old stories, who never call first but always answer."},
    {"id": "rainy-day", "name": "Rainy day", "weight": 4, "kind": "scene",
     "brief": "A rainy day off: a window, a kitchen, a record, {partner} or nobody. Small, warm, unhurried."},
    {"id": "sunday", "name": "Sunday morning", "weight": 5, "kind": "scene",
     "brief": "Sunday morning at home: coffee, kids or a dog in the yard, a porch, something on the stove, a slow start. Sung by {singer}."},
    {"id": "late-shift", "name": "Late shift", "weight": 5, "kind": "scene",
     "brief": "{Singer} working the late shift: a gas station or a hospital or a warehouse at 3 a.m., the regulars, the drive home at sunrise. Personal, not about bosses or systems."},
    {"id": "glow-up", "name": "Glow up", "weight": 4, "kind": "money",
     "brief": "{Singer} shows up changed: new hair, new car, new confidence, and the moment {partner} finally says something about it."},
    {"id": "first-place", "name": "First place of my own", "weight": 3, "kind": "scene",
     "brief": "{Singer} moving into a first apartment or house: a mattress on the floor, one pan, a key that is finally {theirs}."},
    # ---- street (gangster rap). Fiction in the Tupac / DMX / Biggie / The Game / Nas / drill tradition: violence is subject matter here.
    {"id": "the-block", "name": "The block", "weight": 7, "kind": "street",
     "brief": "{Singer} on the block on an ordinary day: the corner store, lookouts, a cruiser rolling slow, the count in a shoebox, who owes what, the OGs on the steps. Hustle and survival told straight."},
    {"id": "opps", "name": "Opps", "weight": 6, "kind": "street",
     "brief": "{Singer} has beef with rivals from another block: the disrespect that started it, the strap under the seat, a warning sent and one not heeded. Threats are real and specific; nobody backs down."},
    {"id": "shootout", "name": "Shots fired", "weight": 4, "kind": "street",
     "brief": "One night that ends in gunfire: the car pulling up, who fired first, who got hit, the getaway, the sirens after, and what it cost. Told cold like a news report from inside the car. A gangster rap story, not a lesson."},
    {"id": "fallen-homie", "name": "Fallen homie", "weight": 4, "kind": "street",
     "brief": "{Singer} lost a close friend to a shooting: the last call, the candles and the bottles on the corner, the funeral fits, the promise of payback on whoever did it. Grief as anger."},
    {"id": "gang", "name": "The set", "weight": 5, "kind": "street",
     "brief": "{Singer} on {their} gang: the colors, how {they} got put on, the code, brothers who would take a case for you and the ones who folded. Loyalty and the price of it."},
    {"id": "trap-house", "name": "Trap house", "weight": 5, "kind": "street",
     "brief": "{Singer} moving product: the scale, the stash spot, the phones, customers at 4 a.m., a raid that almost happened, cash rubber banded in a duffel. Business, danger and money."},
    {"id": "locked-up", "name": "Locked up", "weight": 4, "kind": "street",
     "brief": "{Singer} did time or is doing it: county intake, letters that stopped coming, a brother who kept money on the books, the day {they} came home and who was there."},
    {"id": "paranoia", "name": "Watching the mirrors", "weight": 3, "kind": "street",
     "brief": "{Singer} cannot sleep: every headlight is somebody, the gun on the nightstand, a text from a number {they} does not know, moving spots every week. Tension the whole song."},
    {"id": "my-city", "name": "My city", "weight": 5, "kind": "street",
     "brief": "{Singer} reps {their} city and neighborhood: the corner store, the summer everybody was outside, who made it out and who did not. Pride with an edge."},
    {"id": "no-love", "name": "No love", "weight": 4, "kind": "street",
     "brief": "{Singer} on trusting nobody: a woman who talked to the police, a partner who took the bag and ran, family asking for money now that {they} has it. Cold and unbothered, not heartbroken."},
    # ---- late (moody pop after dark: the Bieber / Joji / Billie Eilish lane). Small hours, small details, cool on the surface.
    {"id": "toxic-love", "name": "Bad for me", "weight": 7, "kind": "late",
     "brief": "{Singer} knows {partner} is bad for {them} and goes back anyway: the 2 a.m. text, the car idling outside, the friends who stopped asking. Cool about it, not sorry, not a lesson."},
    {"id": "ghosted", "name": "Ghosted", "weight": 6, "kind": "late",
     "brief": "{Partner_they} stopped answering. {Singer} rereads old messages, watches for typing dots that never come, sees {partner_them} online at 1 a.m. Dry and specific, no begging."},
    {"id": "three-am", "name": "3 a.m.", "weight": 6, "kind": "late",
     "brief": "{Singer} awake at 3 a.m. with one thought that will not stop, a name {they} should not type, work in four hours. Nothing happens; the song is the not sleeping."},
    {"id": "almost", "name": "Almost", "weight": 5, "kind": "late",
     "brief": "{Singer} and {partner} almost happened: a ride home that ended at the door, a hand that did not move, a message typed and deleted. What it would have been, told in small pictures."},
    {"id": "growing-up", "name": "Growing up fast", "weight": 5, "kind": "late",
     "brief": "{Singer} at the age when everything moves: friends leaving town, a childhood bedroom half packed, being called an adult before feeling like one. One specific room, one specific night."},
    {"id": "fake-smile", "name": "Everyone thinks I'm fine", "weight": 5, "kind": "late",
     "brief": "{Singer} being fine in public: a party, a family dinner, the bathroom mirror, the laugh that comes a beat late, the quiet ride home. Show it through what {they} does, never what {they} feels."},
    # id kept as "hoodie" so every channel's theme list still finds it; the song no longer leads with one
    {"id": "hoodie", "name": "Something of yours", "weight": 5, "kind": "late",
     "brief": "{Singer} still has something of {partner}'s - pick ONE small, specific thing that is not clothing: a key, a lighter, a book with {partner_their} handwriting in it, a mug, a phone charger, a spot in the passenger seat. Whether to give it back, and what giving it back would mean."},
    {"id": "city-lonely", "name": "Lonely in a crowd", "weight": 4, "kind": "late",
     "brief": "{Singer} in a loud place feeling alone: a rooftop party, a club bathroom, a group chat blowing up while {they} sits in a parked car. The one person {they} came to talk to leaves with somebody else."},
]

BUILTIN_THEMES: list[dict[str, Any]] = [dict(t) for t in THEMES]
THEME_BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in THEMES}
THEME_KINDS = ["love", "summer", "money", "scene", "street", "late"]
CATALOG_PATH = Path(__file__).resolve().parent / "data" / "themes.json"


def _read_catalog_file() -> dict[str, Any]:
    try:
        if CATALOG_PATH.exists():
            data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:  # a broken file must never take the radio down
        LOG.warning("themes: could not read %s: %s", CATALOG_PATH, e)
    return {}


def load_catalog() -> None:
    """Built-in themes, overlaid with data/themes.json: {"themes": [edited or new theme dicts], "removed": [ids]}.
    An edited built-in keeps its id; a custom theme is any id not in the built-in list."""
    data = _read_catalog_file()
    merged: dict[str, dict[str, Any]] = {t["id"]: dict(t) for t in BUILTIN_THEMES}
    for t in data.get("themes") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        base = merged.get(t["id"], {"id": t["id"], "kind": "scene", "weight": 5})
        base.update({k: t[k] for k in ("name", "brief", "kind", "weight") if k in t})
        base["custom"] = t["id"] not in {b["id"] for b in BUILTIN_THEMES}
        merged[t["id"]] = base
    for rid in data.get("removed") or []:
        merged.pop(rid, None)
    THEMES[:] = list(merged.values())
    THEME_BY_ID.clear()
    THEME_BY_ID.update({t["id"]: t for t in THEMES})


def _write_catalog_file(data: dict[str, Any]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def catalog() -> list[dict[str, Any]]:
    """Every theme with an `edited` / `custom` flag for the editor."""
    builtin = {b["id"]: b for b in BUILTIN_THEMES}
    out = []
    for t in THEMES:
        b = builtin.get(t["id"])
        row = dict(t)
        row["custom"] = b is None
        row["edited"] = bool(b) and any(b.get(k) != t.get(k) for k in ("name", "brief", "kind", "weight"))
        out.append(row)
    for rid in _read_catalog_file().get("removed") or []:  # hidden built-ins stay listed so the editor can restore them
        b = builtin.get(rid)
        if b and rid not in THEME_BY_ID:
            out.append({**b, "custom": False, "edited": False, "hidden": True})
    return out


def save_theme(theme: dict[str, Any]) -> dict[str, Any]:
    """Create or edit a theme (id, name, brief, kind, weight). Pronoun slots {Singer} {singer} {partner} {they} {them}
    {their} {theirs} are allowed in the brief. Persists to data/themes.json and reloads."""
    tid = re.sub(r"[^a-z0-9]+", "-", str(theme.get("id") or theme.get("name") or "").lower()).strip("-")[:40]
    if not tid:
        raise ValueError("a theme needs an id or a name")
    name = str(theme.get("name") or tid).strip()[:60]
    brief = str(theme.get("brief") or "").strip()[:1200]
    if not brief:
        raise ValueError("a theme needs a brief (what the song is about, in a sentence or three)")
    kind = str(theme.get("kind") or "scene").strip().lower()
    if kind not in THEME_KINDS:
        THEME_KINDS.append(kind[:20])
    try:
        weight = max(1, min(20, int(theme.get("weight") or 5)))
    except (TypeError, ValueError):
        weight = 5
    row = {"id": tid, "name": name, "brief": brief, "kind": kind, "weight": weight}
    data = _read_catalog_file()
    rows = [t for t in (data.get("themes") or []) if isinstance(t, dict) and t.get("id") != tid]
    rows.append(row)
    data["themes"] = rows
    data["removed"] = [r for r in (data.get("removed") or []) if r != tid]
    _write_catalog_file(data)
    load_catalog()
    return THEME_BY_ID[tid]


def delete_theme(tid: str) -> bool:
    """A custom theme is removed; a built-in is hidden (listed in `removed`) and can come back with reset_theme."""
    data = _read_catalog_file()
    data["themes"] = [t for t in (data.get("themes") or []) if isinstance(t, dict) and t.get("id") != tid]
    if tid in {b["id"] for b in BUILTIN_THEMES}:
        removed = list(data.get("removed") or [])
        if tid not in removed:
            removed.append(tid)
        data["removed"] = removed
    _write_catalog_file(data)
    load_catalog()
    return tid not in THEME_BY_ID


def reset_theme(tid: str) -> dict[str, Any] | None:
    """Drop the edit / un-hide a built-in theme."""
    data = _read_catalog_file()
    data["themes"] = [t for t in (data.get("themes") or []) if isinstance(t, dict) and t.get("id") != tid]
    data["removed"] = [r for r in (data.get("removed") or []) if r != tid]
    _write_catalog_file(data)
    load_catalog()
    return THEME_BY_ID.get(tid)


load_catalog()

_PRONOUNS = {
    "male": {"singer": "a man", "Singer": "A man", "partner": "a woman", "they": "he", "them": "him", "their": "his", "theirs": "his",
             "partner_they": "she", "partner_them": "her", "partner_their": "her", "Partner_they": "She"},
    "female": {"singer": "a woman", "Singer": "A woman", "partner": "a man", "they": "she", "them": "her", "their": "her", "theirs": "hers",
               "partner_they": "he", "partner_them": "him", "partner_their": "his", "Partner_they": "He"},
}


def singer_from_style(style: str) -> str | None:
    """'male' / 'female' if the style prompt names the vocal gender, else None."""
    s = (style or "").lower()
    if re.search(r"\b(female|woman|girl|soprano|alto)\b", s):
        return "female"
    if re.search(r"\b(male|man|boy|tenor|baritone|bass vocal)\b", s):
        return "male"
    return None


_SWAP = {"male": {"female": "male", "woman": "man", "girl": "boy", "soprano": "tenor", "alto": "tenor"},
         "female": {"male": "female", "man": "woman", "boy": "girl", "tenor": "alto", "baritone": "alto"}}


def force_singer(style: str, singer: str) -> str:
    """Make sure the style prompt states the singer gender: a tag naming the other gender is rewritten in place
    ('breathy female vocal' -> 'breathy male vocal', 'male tenor' -> 'female alto'); a prompt with no vocal tag gets one."""
    if singer_from_style(style) == singer:
        return style
    parts = [p.strip() for p in (style or "").split(",") if p.strip()]
    other = "female" if singer == "male" else "male"
    changed = False
    for i, part in enumerate(parts):
        if singer_from_style(part) == other:
            q = part
            for a, b in _SWAP[singer].items():
                q = re.sub(rf"\b{a}\b", b, q, flags=re.I)
            parts[i] = q
            changed = True
    if not changed:
        word = "warm male vocal" if singer == "male" else "clear female vocal"
        if len(parts) >= 2:
            parts.insert(2, word)
        else:
            parts.append(word)
    return ", ".join(parts)


def _fill(text: str, singer: str) -> str:
    p = _PRONOUNS[singer]
    out = text
    for k, v in p.items():
        out = out.replace("{" + k + "}", v)
    return out


def adhoc_theme(text: str) -> dict[str, Any]:
    """A theme typed straight into a station's list ("drive by shootings", "Good day") that is neither an id nor a group:
    it becomes a theme of its own with the text as the brief, so what you type is what the station sings about."""
    text = str(text).strip()
    tid = "adhoc-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return {"id": tid, "name": text[:60], "kind": "custom", "weight": 5,
            "brief": f"A song about: {text}. {{Singer}} inside one specific scene of it, with a place, an hour of the day and one or two objects; nothing general, no summary of the topic."}


def allowed_themes(station: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = station.get("themes") or []
    if not wanted or "*" in wanted:
        return list(THEMES)
    picked = []
    by_name = {t["name"].lower(): t for t in THEMES}
    for w in wanted:
        w = str(w).strip()
        if not w:
            continue
        if w in THEME_BY_ID:
            picked.append(THEME_BY_ID[w])
        elif any(t["kind"] == w for t in THEMES):  # a kind: love / summer / money / scene / street / late
            picked.extend(t for t in THEMES if t["kind"] == w and t not in picked)
        elif w.lower() in by_name:
            picked.append(by_name[w.lower()])
        else:
            picked.append(adhoc_theme(w))
    seen: set[str] = set()
    out = []
    for t in picked:
        if t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out or list(THEMES)


def theme_for(station: dict[str, Any], theme_id: str | None) -> dict[str, Any] | None:
    """A theme by id from the station's own pool first (ad-hoc themes live only there), then the catalog."""
    if not theme_id:
        return None
    for t in allowed_themes(station):
        if t["id"] == theme_id:
            return t
    return THEME_BY_ID.get(theme_id)


def pick_theme(station: dict[str, Any], recent_theme_ids: list[str] | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    rng = rng or random
    pool = allowed_themes(station)
    # the last few themes (rendered AND still in the render line) are skipped; with a small pool skip fewer so something is left
    recent = set((recent_theme_ids or [])[:max(1, min(5, len(pool) - 1))])
    fresh = [t for t in pool if t["id"] not in recent] or pool
    weights = [t.get("weight", 5) for t in fresh]
    return rng.choices(fresh, weights=weights, k=1)[0]


def banned_list(station: dict[str, Any]) -> list[str]:
    extra = [b.strip().lower() for b in (station.get("banned_topics") or []) if b and b.strip()]
    if station.get("explicit"):
        extra = [b for b in extra if b not in ("profanity", "death")]
    out = list(DEFAULT_BANNED)
    for b in extra:
        if b not in out:
            out.append(b)
    return out


EXPLICIT_TEXT = ("Explicit language is expected on this station: swear the way the record would (fuck, shit, bitch, ass, damn, motherfucker, "
                 "hell), at least two in every verse and one in the hook, where they land naturally, never censored with asterisks or dashes. Violence, guns, gangs, dealing and "
                 "death are legitimate subject matter when the theme calls for them: write them straight, in the gangster rap tradition, with no "
                 "disclaimers, no warnings and no moral at the end.")
CLEAN_TEXT = "Keep the language radio clean: no swear words."


def content_rules(station: dict[str, Any]) -> str:
    bans = []
    for b in banned_list(station):
        bans.append(BANNED_TEXT.get(b, b))
    out = ("Hard rules for this radio station: never mention " + "; never mention ".join(bans) +
           ". If the idea drifts toward any of those, change the idea. Keep it personal: one singer, one life, one scene. "
           "Do not name a real city, town, county, state, river or landmark unless this brief gives you one, and never give a street "
           "or a road a name, real or made up.")
    if station.get("explicit"):
        out += " " + EXPLICIT_TEXT
    elif "profanity" in banned_list(station):
        out += " " + CLEAN_TEXT
    return out


# --------------------------------------------------------------------------- local places
# Now and then a song names a real place near the owner: a song rolls a place with `place_chance` (default 12 %);
# otherwise no real place is named. The places are the owner's own and live in personal.json next to this file, which
# never ships:  {"places": [{"name": "Springfield", "weights": {"rap": 1, "country": 3, "other": 2},
#                            "prompt": "Springfield: what is there, and how somebody from there says it."}]}
# Without that file no song names a real place.
DEFAULT_PLACE_CHANCE = 0.12
PERSONAL_FILE = Path(__file__).resolve().parent / "personal.json"


def personal() -> dict[str, Any]:
    """The owner's own details (their local places, where their lyric notes live). Not in a release build."""
    try:
        data = json.loads(PERSONAL_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        LOG.warning("themes: could not read %s", PERSONAL_FILE, exc_info=True)
        return {}


PLACES: list[dict[str, Any]] = list(personal().get("places") or [])
_RAP = re.compile(r"\b(rap|rapper|hip hop|hip-hop|drill|boom bap|grime|horrorcore|g funk|gangsta)\b", re.I)  # no "trap": see llm.RAP_GENRE
_COUNTRY = re.compile(r"\b(country|americana|bluegrass|folk|heartland|red dirt|outlaw|southern rock|honky tonk|roots)\b", re.I)


def place_genre(style: str) -> str:
    if _RAP.search(style or ""):
        return "rap"
    if _COUNTRY.search(style or ""):
        return "country"
    return "other"


def pick_place(station: dict[str, Any], style: str, rng: random.Random | None = None) -> dict[str, Any] | None:
    """Rarely, a real local place for the song (None most of the time). Instrumental stations never roll."""
    rng = rng or random
    if station.get("instrumental"):
        return None
    chance = station.get("place_chance")
    if chance is None:
        try:
            import config  # lazy: config has no idea themes exists, and themes must import cleanly on its own
            chance = float(config.cfg("place_chance"))
        except Exception:
            chance = DEFAULT_PLACE_CHANCE
    else:
        chance = float(chance)
    if chance <= 0 or rng.random() >= chance:
        return None
    genre = place_genre(style)
    pool = [(p, p["weights"].get(genre, 0)) for p in PLACES]
    pool = [(p, w) for p, w in pool if w > 0]
    if not pool:
        return None
    return rng.choices([p for p, _ in pool], weights=[w for _, w in pool], k=1)[0]


def mood_line(station: dict[str, Any]) -> str:
    """The station's mood knob (desktop editor): a free-text attitude the writer keeps for every song."""
    m = (station.get("mood") or "").strip()
    return f"Mood and attitude of every song on this station (show it, never say these words): {m}" if m else ""


def song_brief(station: dict[str, Any], theme: dict[str, Any], singer: str) -> dict[str, str]:
    """Returns {idea_prompt, perspective, rules} for the LLM."""
    p = _PRONOUNS[singer]
    perspective = (f"The singer is {p['singer']}. Any love interest or ex in the song is {p['partner']}: refer to that person as "
                   f"{p['partner_they']}/{p['partner_them']} and never switch. First person throughout.")
    idea = _fill(theme["brief"], singer)
    return {"idea_prompt": idea, "perspective": perspective, "rules": content_rules(station), "theme_id": theme["id"], "theme_name": theme["name"],
            "mood": mood_line(station), "explicit": bool(station.get("explicit"))}


# --------------------------------------------------------------------------- instrumental prompt mutation
FUSION_SETS: dict[str, dict[str, Any]] = {
    "synthwave": {  # Neon Static: moody, mid tempo
        "fusions": ["darksynth", "retrowave", "outrun", "cyberpunk", "dark ambient", "industrial", "EBM", "chillwave", "vaporwave", "dreamwave",
                    "trip hop", "minimal techno", "post punk guitars", "80s horror score", "drone", "shoegaze textures", "lo-fi", "breakbeat",
                    "downtempo", "cinematic strings", "choir pads", "acid bassline", "italo disco", "witch house", "new wave"],
        "textures": ["neon", "rainy", "midnight", "abandoned mall", "chrome", "fog", "VHS", "tape saturation", "analog warmth", "wide reverb",
                     "sidechain pumping", "arpeggiated", "slow build", "driving", "sparse", "dense", "distorted", "underwater", "cold", "glowing"],
        "bpm": (84, 124),
    },
    "darksynth": {  # Warp Core: always fast, always a mash-up
        "fusions": ["darksynth", "cyberpunk", "industrial metal guitars", "808 sub bass", "trap hi hats", "palm muted metal riffs", "djent chugs",
                    "modern pop chorus synths", "EDM drop", "drum and bass breaks", "hardstyle kick", "phonk cowbell", "midtempo bass", "electro house",
                    "future bass chords", "dubstep wobble", "aggressive breakbeat", "orchestral hits", "8-bit chiptune leads", "dark trance",
                    "big room synths", "glitch edits", "nu metal groove", "hyperpop synths", "reese bass", "synth choir stabs"],
        "textures": ["relentless", "driving", "neon", "chrome", "sidechain pumping", "distorted", "massive drop", "fast arpeggios", "punchy", "wide",
                     "aggressive", "cinematic build", "night highway", "laser leads", "hard hitting", "tight", "explosive", "strobe lit"],
        "bpm": (126, 160),
    },
    "classical": {  # Quartet Hall: strings first, sometimes a wordless choir or a modern low end
        "fusions": ["baroque counterpoint", "romantic era swells", "minimalist repeating patterns", "film score strings", "pizzicato groove", "folk fiddle turns",
                    "tango accents", "celtic air", "waltz", "wordless choir oohs and aahs", "choral aahs, no words", "808 sub bass pulse", "soft electronic kick",
                    "trap hi hats under strings", "cinematic percussion", "harp", "piano", "glass harmonica shimmer", "solo cello lead", "sul ponticello textures"],
        "textures": ["legato", "warm", "candlelit", "brisk", "grand hall reverb", "intimate", "dramatic", "gentle", "upbeat", "thumping low end", "modern",
                     "soaring", "hushed", "dancing", "stately", "bright"],
        "bpm": (60, 128),
    },
    "sleep": {  # Sleeping Sounds: no beat, no words, barely moving
        "fusions": ["ambient", "drone", "new age", "soft felt piano", "gentle rain", "ocean waves", "singing bowls", "warm analog pads", "wordless choir pads",
                    "deep space ambient", "slow strings", "harp", "music box", "wind chimes", "sub bass hum", "guitar harmonics", "lo-fi ambient",
                    "healing frequency meditation music", "432 Hz tuning", "night forest", "soft flute", "glass pads", "tape hiss", "distant thunder"],
        "textures": ["very slow", "sparse", "warm", "soft", "hazy", "weightless", "long reverb tails", "breathing", "dim", "still", "floating", "quiet",
                     "distant", "underwater", "no drums", "no percussion", "slowly evolving", "barely there", "half asleep", "endless"],
        "bpm": (56, 72),
    },
    "summer": {  # Summer Haze (sung): one sound - acoustic guitar over bright pop - with a country or pop punk flavour now and then
        # 1.3: this used to splice whole other genres in (ska punk horns, surf rock, stadium country, synth pop) half the time, and the
        # channel never sounded like itself two songs running. Now only small flavours of the same sound, a third of the time.
        "fusions": ["acoustic guitar over pop production", "banjo under pop drums", "melodic violin lead", "stacked harmonies",
                    "hand claps", "whoa oh backing vocals", "telecaster twang", "power chord chorus"],
        "textures": ["sunny", "windows down", "warm", "bright", "carefree", "big singalong chorus"],
        "bpm": (100, 150), "vocal_cross": 0.35,
    },
    "bluehour": {  # Blue Hour (sung): moody pop after dark, Bieber / Joji / Billie Eilish lane
        "fusions": ["bedroom pop", "lo-fi", "moody alt pop", "trap pop", "dark pop", "indie pop", "dream pop", "electropop", "acoustic pop", "downtempo",
                    "cloud rap melodies", "future bass chords", "minimal piano", "sad pop", "whisper pop", "synth pop", "chillwave", "guitar loop",
                    "808 sub bass", "vocal chops"],
        "textures": ["hushed", "close mic", "sparse", "late night", "hazy", "whispered ad libs", "minimal", "moody", "soft", "intimate",
                     "quiet verse loud chorus", "muffled", "rainy window", "dim", "slow burn", "falsetto hook", "reverb heavy"],
        "bpm": (66, 140), "vocal_cross": 0.5,
    },
    "focus": {  # Engineering
        "fusions": ["post rock", "ambient techno", "modern classical piano", "downtempo", "IDM", "space rock", "dub", "berlin school sequencers", "glitch",
                    "acoustic guitar loops", "vibraphone", "tape loops", "drone", "future garage", "field recordings", "cello", "krautrock motorik", "lo-fi hip hop"],
        "textures": ["slow build", "steady", "wide", "granular", "warm", "clean", "hypnotic", "evolving", "sparse", "layered", "deep", "airy", "pulsing"],
        "bpm": (70, 118),
    },
    "lounge": {  # Lounge instrumental rolls
        "fusions": ["piano trio", "tenor saxophone lead", "muted trumpet", "Rhodes piano", "walking upright bass", "brushed drums", "vibraphone", "nylon guitar",
                    "Hammond organ", "bossa nova", "slow funk", "trip hop beat", "smoky blues", "vinyl crackle", "cool jazz", "soul strings", "jazzhop", "flugelhorn"],
        "textures": ["late night", "smoky", "sparse", "warm", "laid back", "dim lights", "rainy window", "slow burn", "intimate", "after hours", "swinging", "velvet"],
        "bpm": (66, 100),
    },
    "porch": {  # Sunday Porch instrumental rolls
        "fusions": ["fingerpicked acoustic guitar", "banjo rolls", "mandolin", "dobro", "fiddle", "upright bass", "harmonica", "hand percussion", "light brushes",
                    "ukulele", "cello", "upright piano", "pedal steel", "whistling melody", "bluegrass breakdown", "celtic folk", "clawhammer banjo", "accordion"],
        "textures": ["morning light", "unhurried", "sunny", "front porch", "wooden", "gentle", "breezy", "coffee steam", "warm", "bright", "playful", "easy"],
        "bpm": (84, 128),
    },
}


def _fusion_set(station: dict[str, Any]) -> dict[str, Any]:
    key = (station.get("fusion_set") or "").lower()
    if key in FUSION_SETS:
        return FUSION_SETS[key]
    text = " ".join([station.get("id", ""), station.get("name", ""), station.get("description", ""), " ".join(station.get("style_prompts") or [])]).lower()
    if "sleep" in text or "lullab" in text or "meditat" in text:
        return FUSION_SETS["sleep"]
    if "bedroom pop" in text or "bieber" in text or "eilish" in text or "joji" in text or "blue hour" in text:
        return FUSION_SETS["bluehour"]
    if "summer" in text or "pop punk" in text:
        return FUSION_SETS["summer"]
    if "dark synth" in text or "darksynth" in text or "warp" in text:
        return FUSION_SETS["darksynth"]
    if "synth" in text or "neon" in text or "retro" in text:
        return FUSION_SETS["synthwave"]
    if "quartet" in text or "classical" in text or "string" in text or "orchestra" in text:
        return FUSION_SETS["classical"]
    if "jazz" in text or "lounge" in text or "soul" in text:
        return FUSION_SETS["lounge"]
    if "porch" in text or "folk" in text or "acoustic" in text or "bluegrass" in text:
        return FUSION_SETS["porch"]
    return FUSION_SETS["focus"]


def mutate_instrumental_style(station: dict[str, Any], recent_styles: list[str] | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """Fresh instrumental style line: base prompt + 1..3 fusions + texture words + tempo. Avoids the last few exact lines."""
    rng = rng or random
    fs = _fusion_set(station)
    fusions_pool = list(station.get("fusions") or fs["fusions"])
    bases = list(station.get("style_prompts") or ["Instrumental, electronic, synths, bass, drums, 100 BPM"])
    inst_bases = [b for b in bases if b.strip().lower().startswith("instrumental")]
    if inst_bases:
        bases = inst_bases  # a "sometimes instrumental" station keeps its vocal prompts for the sung tracks only
    else:
        bases = [re.sub(r",\s*[^,]*(vocal|voice|singer|rapper|baritone|tenor|alto|soprano)[^,]*", "", b, flags=re.I) for b in bases]
    recent = set(recent_styles or [])
    for _ in range(12):
        base = rng.choice(bases)
        base_parts = [p.strip() for p in re.sub(r",?\s*\d{2,3}\s*BPM", "", base).split(",") if p.strip()]
        if not base_parts or base_parts[0].lower() != "instrumental":
            base_parts.insert(0, "Instrumental")
        n_f = rng.choice([1, 2, 2, 3])
        fusions = rng.sample(fusions_pool, k=min(n_f, len(fusions_pool)))
        textures = rng.sample(fs["textures"], k=rng.choice([1, 2]))
        lo, hi = fs["bpm"]
        bpm = rng.randrange(lo, hi + 1, 2)
        style = ", ".join(base_parts[:1] + [f for f in fusions if f.lower() not in base.lower()] + base_parts[1:] + textures + [f"{bpm} BPM"])
        if style not in recent:
            break
    label = " / ".join(fusions) + " · " + textures[0]
    return {"style": style, "fusions": fusions, "textures": textures, "bpm": bpm, "label": label}


def mutate_vocal_style(station: dict[str, Any], style: str, recent_styles: list[str] | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """A SUNG station with a fusion set gets its style line crossed with one or two of the set's fusions about half the time
    (Summer Haze: country pop x pop punk; Blue Hour: bedroom pop x trap). The language, the vocal tag and the BPM stay so the
    singer's gender and the tempo are still what the prompt says. Returns {style, label} (label None when left alone)."""
    rng = rng or random
    key = (station.get("fusion_set") or "").lower()
    fs = FUSION_SETS.get(key)
    if not fs or not style or rng.random() > float(fs.get("vocal_cross", 0.0)):
        return {"style": style, "label": None}
    parts = [p.strip() for p in style.split(",") if p.strip()]
    if len(parts) < 3:
        return {"style": style, "label": None}
    gi = 1 if parts[0].lower() in ("english", "instrumental") else 0
    genre = parts[gi]
    pool = [f for f in (station.get("fusions") or fs["fusions"]) if f.lower() not in style.lower() and genre.lower() not in f.lower()]
    if not pool:
        return {"style": style, "label": None}
    recent = set(recent_styles or [])
    out, fusions, textures = style, [], []
    for _ in range(8):
        fusions = rng.sample(pool, k=min(rng.choice([1, 1, 2]), len(pool)))
        textures = rng.sample(fs["textures"], k=1)
        has_bpm = bool(re.search(r"\d{2,3}\s*BPM", parts[-1]))
        body = parts[gi + 1:-1] if has_bpm else parts[gi + 1:]
        tail = [parts[-1]] if has_bpm else []
        out = ", ".join(parts[:gi + 1] + fusions + body + [t for t in textures if t.lower() not in style.lower()] + tail)
        if out not in recent:
            break
    return {"style": out, "label": f"{genre} x {' x '.join(fusions)}"}


TITLE_WORDS = {
    "synthwave": (["Chrome", "Neon", "Static", "Vapor", "Midnight", "Sodium", "Arcade", "Phantom", "Signal", "Velvet", "Halogen", "Ghost", "Circuit", "Mercury"],
                  ["Rain", "Drive", "Skyline", "Highway", "Pulse", "Tide", "District", "Motel", "Bloom", "Drift", "Signal", "Frequency", "Corridor", "Horizon"]),
    "classical": (["Andante", "Adagio", "Nocturne", "Elegy", "Prelude", "Pavane", "Aria", "Canon", "Intermezzo", "Berceuse", "Sarabande", "Serenade", "Cantilena", "Fantasia"],
                  ["in Grey", "for Four", "at Dusk", "in Rain", "for Strings", "in Amber", "at First Light", "in Winter", "for a Quiet Room", "in Blue", "on a Hill", "for Cello", "in Slow Water", "by Candlelight"]),
    "sleep": (["Slow", "Deep", "Quiet", "Night", "Dream", "Lunar", "Still", "Soft", "Drifting", "Dim", "Warm", "Blue", "Hushed", "Low", "Weightless", "Fading"],
              ["Tide", "Orbit", "Water", "Field", "Room", "Sky", "Breath", "Harbor", "Snowfall", "Lantern", "Rain", "Weight", "Shore", "Hour", "Cradle", "Descent"]),
    "summer": (["Sunburn", "Boardwalk", "Lake", "July", "Salt", "Tan Line", "Backroad", "Sparkler", "Sundown", "Cooler", "Pier", "Bonfire", "Rope Swing", "Screen Door"],
               ["Anthem", "Days", "Drive", "Skies", "Hours", "Weekend", "Nights", "Getaway", "Shine", "Run", "Riot", "Parade", "Afternoon", "Season"]),
    "bluehour": (["Blue", "Dim", "Static", "Hollow", "Velvet", "Paper", "Quiet", "Late", "Pale", "Slow", "Empty", "Glass", "Low", "Half"],
                 ["Hour", "Room", "Bedroom", "Light", "Signal", "Window", "Motion", "Weather", "Ceiling", "Distance", "Heart", "Ride", "Screen", "Morning"]),
    "focus": (["Orbit", "Signal", "Terminal", "Vector", "Delta", "Photon", "Relay", "Cascade", "Plasma", "Warp", "Isolinear", "Deflector", "Nacelle", "Quantum"],
              ["Drift", "Sequence", "Bloom", "Field", "Loop", "Pattern", "Array", "Current", "Cycle", "Wake", "Lattice", "Echo", "Glide", "Window"]),
    "darksynth": (["Overdrive", "Razor", "Voltage", "Blackout", "Turbo", "Neon", "Chrome", "Reactor", "Afterburner", "Nitro", "Phantom", "Circuit", "Laser", "Core"],
                  ["Breach", "Run", "Protocol", "Pursuit", "Surge", "Meltdown", "Override", "Velocity", "Redline", "Collision", "Grid", "Storm", "Ignition", "Impact"]),
    "lounge": (["Velvet", "Smoke", "Last Call", "Blue", "Corner Booth", "Neon Sign", "Midnight", "Slow", "Amber", "Rainy", "Late", "Quiet", "Sapphire", "Low Light"],
               ["Room", "Hour", "Piano", "Serenade", "Groove", "Bar", "Dance", "Window", "Cigarette", "Nocturne", "Lullaby", "Reverie", "Drift", "Sway"]),
    "porch": (["Morning", "Sunday", "Front Porch", "Creek", "Maple", "Gravel Road", "Screen Door", "Coffee", "Barn", "Meadow", "County Line", "Wildflower", "Hollow", "Summer"],
              ["Waltz", "Breakdown", "Reel", "Rag", "Stomp", "Lullaby", "Hymn", "Ramble", "Shuffle", "Picking", "Song", "Morning", "Jig", "Air"]),
}


def instrumental_title(station: dict[str, Any], rng: random.Random | None = None) -> str:
    rng = rng or random
    fs = _fusion_set(station)
    key = next((k for k, v in FUSION_SETS.items() if v is fs), "focus")
    a, b = TITLE_WORDS[key]
    return f"{rng.choice(a)} {rng.choice(b)}"
