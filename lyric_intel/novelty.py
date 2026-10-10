"""novelty.py — is this song reaching for the same things as the last twenty five?

Deterministic python. The score is only ever used to decide between "keep it" and "ask for one rewrite of
the lines that repeat"; there is no loop, and a song is never thrown away.
"""
from __future__ import annotations

import re
from typing import Any

from . import features
from .lexicon import WORD_BUCKET

# how hard each level looks, and how low a score it tolerates
LEVELS: dict[str, dict[str, Any]] = {
    "off": {},
    "low": {"word_share": 0.50, "word_top": 4, "phrase_songs": 5, "rhyme_songs": 5, "seeds": 1, "floor": 0.0},
    "normal": {"word_share": 0.33, "word_top": 6, "phrase_songs": 3, "rhyme_songs": 3, "seeds": 1, "floor": 0.55},
    "high": {"word_share": 0.25, "word_top": 8, "phrase_songs": 2, "rhyme_songs": 2, "seeds": 2, "floor": 0.68},
}


def level(name: str) -> dict[str, Any]:
    return LEVELS.get((name or "normal").lower(), LEVELS["normal"])


def worn_words() -> list[str]:
    """The words the radio has worn out (the worn_out_words setting), lower case, no blanks.

    Kept here rather than in the package so novelty never imports its own parent."""
    try:
        import config
        raw = str(config.cfg("worn_out_words") or "")
    except Exception:
        raw = "hoodie"
    return [w.strip().lower() for w in raw.replace(";", ",").split(",") if w.strip()]


def worn_pattern(entry: str) -> "re.Pattern[str]":
    """A worn-out entry as a regex. "fade*" is a prefix (fade, fades, faded, fading); a plain entry
    matches itself and its plurals, y -> ies included (memory, memories). A plain "ghost" does NOT
    match "ghosted", which Blue Hour uses as slang on purpose."""
    e = entry.strip().lower()
    if e.endswith("*"):
        stem = e[:-1]
        alts = [re.escape(stem) + r"\w*"]
        if stem.endswith("e") and len(stem) > 2:
            alts.append(re.escape(stem[:-1]) + r"ing\b")      # fade -> fading: the silent e drops
        return re.compile(r"\b(?:" + "|".join(alts) + ")", re.I)
    alts = [re.escape(e) + r"(?:s|es)?"]
    if e.endswith("y") and len(e) > 2:
        alts.append(re.escape(e[:-1]) + "ies")
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I)


def worn_found(text: str) -> list[str]:
    """The worn-out entries that turn up in this text, as the writer should hear them (no *)."""
    return [w.rstrip("*") for w in worn_words() if worn_pattern(w).search(text or "")]


# The owner's lyric guide names these as the cliche perfect rhymes. Two line-ending words from one pair
# in the same song is the cliche; either word on its own is fine.
CLICHE_RHYMES = [("night", "light"), ("fire", "desire"), ("heart", "apart"), ("pain", "rain"), ("love", "above"),
                 ("sky", "high"), ("forever", "together"), ("dreams", "seems"), ("true", "you")]


def cliche_rhymes(lyrics: str) -> list[str]:
    ends = set()
    for line in (lyrics or "").splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        words = re.findall(r"[a-z']+", line.lower())
        if words:
            ends.add(words[-1])
    return [f"{a}/{b}" for a, b in CLICHE_RHYMES if a in ends and b in ends]


def leaning_on(state: dict[str, Any], lvl: dict[str, Any], licence: set[str] | None = None) -> list[str]:
    """Content words this channel is using in too many of its recent songs. Words the brief itself asked
    for are exempt, so a night-drive song may still say headlights.

    Worn-out words go first and are on the list whatever the numbers say: they are not being counted,
    they have been retired."""
    worn = [w.rstrip("*") for w in worn_words()]
    if not state.get("n") or not lvl:
        return worn[:]
    licence = licence or set()
    hits = [(w, s) for w, s in (state.get("words") or {}).items()
            if s >= lvl["word_share"] and w not in licence]
    # a channel that repeats itself repeats a lot of words at once, so the list would otherwise be decided by
    # dictionary order. Concrete images (the ones in a lexicon drawer) are what a listener actually notices,
    # so they come first; filler like 'slow' or 'right' only makes the list once those run out.
    hits.sort(key=lambda x: (-x[1], 0 if x[0] in WORD_BUCKET else 1, -len(x[0]), x[0]))
    counted = [w for w, _ in hits if w not in worn][:lvl["word_top"]]
    return worn + counted


def worn_phrases(state: dict[str, Any], lvl: dict[str, Any], top: int = 4) -> list[str]:
    if not state.get("n") or not lvl:
        return []
    hits = [(p, c) for p, c in (state.get("phrases") or {}).items() if c >= lvl["phrase_songs"] and len(p.split()) >= 2]
    hits.sort(key=lambda x: (-x[1], -len(x[0])))
    out: list[str] = []
    for p, _ in hits:
        if any(p in q for q in out):  # keep the longest form of a repeated phrase only
            continue
        out.append(p)
        if len(out) >= top:
            break
    return out


def worn_rhymes(state: dict[str, Any], lvl: dict[str, Any], top: int = 4) -> list[str]:
    if not state.get("n") or not lvl:
        return []
    hits = [(r, c) for r, c in (state.get("rhymes") or {}).items() if c >= lvl["rhyme_songs"]]
    hits.sort(key=lambda x: -x[1])
    return [r for r, _ in hits[:top]]


SCENE_SAME = 0.40      # measured on this radio's own 192 ideas: the median pair scores 0.06 and only 0.2 % reach 0.40
SCENE_MIN_WORDS = 5    # two short ideas can look alike by accident


def scene_again(topic: str, ideas: list[set[str]]) -> dict[str, Any]:
    """Is this idea the same scene the channel has already written? Word overlap, not an embedding: the ideas
    that actually repeat here repeat their nouns (kitchen, refrigerator, blinds, streetlight, car idling), and
    this costs nothing, loads nothing onto the card and cannot fail."""
    mine = set(features.content_words(topic or ""))
    if len(mine) < SCENE_MIN_WORDS or not ideas:
        return {"same": False, "overlap": 0.0, "shared": []}
    best, shared = 0.0, set()
    for other in ideas:
        if len(other) < SCENE_MIN_WORDS:
            continue
        union = mine | other
        if not union:
            continue
        j = len(mine & other) / len(union)
        if j > best:
            best, shared = j, mine & other
    return {"same": best >= SCENE_SAME and len(shared) >= SCENE_MIN_WORDS,
            "overlap": round(best, 3),
            "shared": sorted(shared, key=lambda w: (0 if w in WORD_BUCKET else 1, w))[:10]}


# The shape nearly every idea came back in by 1.2: a still picture of one person standing, sitting or leaning
# somewhere, watching another "across the street", in "the glow of" something "dimly lit". Measured over the last
# 15 ideas per channel: Blue Hour 13, Summer Haze 8, and 11 on one of the owner's own channels - and the lyrics followed ("I watch her",
# "I sit here"), which is why so many songs sounded like the same song. Nothing happens in a picture.
STAGING = {
    "across the street/room": r"\bacross the (?:street|room|road|lot|parking lot|bar|table|yard|hall|hallway|way|field|dance floor|pool|lake|aisle|counter|diner|park|square)\b",
    "leaning on something": r"\blean(?:s|ing|ed)? (?:against|on|back)\b",
    "standing or sitting there": r"\b(?:stands?|standing|stood|sits?|sitting|sat) (?:alone|in the|at the|on the|by the|outside|behind|near|beside|under|next to|there|still|quietly)\b",
    "the glow of": r"\bglow(?:s|ing|ed)?\b",
    "dimly lit": r"\bdim(?:ly)?\b",
    "watching": r"\bwatch(?:es|ing|ed)? (?:her|him|them|from|as|the|over)\b|\bfrom (?:a distance|afar)\b",
    "staring": r"\b(?:star(?:es|ing|ed)|gaz(?:e|es|ing|ed)) (?:at|out|into|up|across|down)\b",
    "through a window": r"\bthrough (?:the|a) (?:window|glass|fence|windshield|blinds|screen door)\b",
    "in the quiet of": r"\bin the (?:quiet|silence|stillness|shadows?) of\b",
    "the corner of": r"\b(?:the )?corner of\b",
    "a hum or a buzz": r"\b(?:hum|hums|humming|hummed|buzz|buzzes|buzzing)\b",
}
_STAGING = {k: re.compile(v, re.I) for k, v in STAGING.items()}
_STREET = re.compile(r"\b(?:[A-Z][a-z]+ ){1,2}(?:Street|St\.|Avenue|Ave|Drive|Lane|Road|Boulevard|Blvd|Court|Way|Place)\b")


def staging_found(idea: str) -> list[str]:
    """The still-picture habits in an idea (see STAGING)."""
    return [k for k, p in _STAGING.items() if p.search(idea or "")]


def is_picture(hits: list[str]) -> bool:
    """Two habits, or somebody watching or staring on their own: that is the song where nothing happens."""
    return len(hits) >= 2 or "watching" in hits or "staring" in hits


def invented_streets(idea: str, allowed: str = "") -> list[str]:
    """Made-up street names in an idea ("the diner on Elm Street"). A place the song was handed is fine."""
    return sorted({m.group(0) for m in _STREET.finditer(idea or "") if m.group(0).lower() not in (allowed or "").lower()})


# Since 1.4: the two things that made the radio sound like a radio. Measured over the songs on hand: a made-up street
# name (Sunflower Drive, Cherry Lane, Elm Street) in 32 % of one channel's songs and 49 % of another's, the word
# street in up to 76 %, and a first name (Jake, Sarah, Mike, Ray) in 76 % of one country channel's. Real songs do both
# now and then, not every time. A made-up street name is never kept; the word street and a first name are kept only on
# a song the channel's recent ones have left room for (radio.people_places_budget).
STREET_WORDS = re.compile(r"\b(?:streets?|street ?lights?|street ?lamps?|sidewalks?|avenues?|boulevards?)\b", re.I)

FIRST_NAMES = set("""
Aaron Abby Abigail Adam Al Alan Albert Alex Alice Allen Allison Amanda Amy Andrea Andrew Andy Angela Angie Ann Anna Annie
Anthony Arthur Ashley Ava Bailey Barbara Becky Ben Benjamin Benny Beth Betty Beverly Bill Billy Bo Bob Bobby Bonnie Brad
Brady Brandon Brenda Brent Brett Brian Brittany Brooke Bruce Bryan Bubba Buddy Caleb Callie Calvin Cameron Carl Carla Carlos
Carol Caroline Carrie Casey Cassie Catherine Cathy Charlie Cheryl Chloe Chris Christine Christopher Chuck Cindy
Claire Clara Cody Cole Colton Connie Connor Corey Craig Curtis Cynthia Daisy Dale Dan Dana Daniel Danny Darlene Darrell Dave
David Debbie Deborah Debra Delilah Denise Dennis Derek Dewayne Diana Diane Dolly Don Donna Donnie Dorothy Doug Douglas Duane
Dustin Dwayne Dylan Earl Eddie Edward Elijah Ella Ellie Emily Emma Eric Erin Ethan Eugene Eva Evelyn Frankie Fred Freddie
Gabe Gabriel Gary George Gerald Gina Glen Glenn Gloria Gordon Greg Gregory Gus Hailey Haley Hank Hannah Harold Harry Heidi
Helen Henry Holly Howard Ian Isaac Isabella Jack Jackie Jacob Jake James Jamie Jan Jane Janet Janice Jared Jason Jay Jeff
Jeffrey Jenna Jennifer Jenny Jeremy Jerry Jess Jesse Jessica Jessie Jill Jim Jimmy Jo Joan Joanna Jodi Jody Joe Joel Joey
John Johnny Jolene Jon Jonathan Jose Josh Joshua Josie Joyce Juan Judy Julia Julie Justin Karen Kate Katherine Kathleen
Kathy Katie Kayla Keith Kelly Ken Kenny Kenneth Kevin Kim Kimberly Kirk Kristen Kyle Lacey Larry Laura Lauren Leah Lena Leo
Leon Leroy Leslie Lily Linda Lindsay Lisa Logan Lonnie Lori Lou Louis Lucas Lucille Lucy Luke Luther Lydia Lynn Mabel
Maddie Madison Maggie Mandy Marcus Margaret Maria Marie Marilyn Marley Martha Martin Marvin Mary Matt Matthew Maurice Max
Megan Melanie Melissa Melvin Mia Michael Michelle Mickey Mike Mikey Millie Mindy Missy Molly Monica Nancy Natalie Nate Nathan
Ned Nick Nicky Nicole Noah Nora Norma Olivia Otis Owen Pam Pamela Patricia Patrick Patsy Paul Paula Peggy Pete Peter Phil
Phillip Rachel Ralph Randy Ray Raymond Rebecca Reggie Rhonda Rick Ricky Rita Rob Robbie Robert Roger Ron Ronnie Rosie Ross
Roxanne Roy Russell Ruth Ryan Sadie Sally Sam Samantha Sammy Sandra Sara Sarah Scott Sean Seth Shane Shannon Sharon Shawn
Sheila Shelby Shirley Sophia Sophie Stacy Stan Stella Stephanie Steve Steven Sue Susan Suzie Tammy Tanya Tara Ted Teddy
Teresa Terry Tessa Theresa Thomas Tim Timmy Timothy Tina Todd Tom Tommy Tony Tracy Travis Trevor Troy Tyler Valerie Vanessa
Vicky Victor Vince Vincent Virgil Wanda Walter Wayne Wendy Wes Willie Zach Zachary Zoe
Jamal Tyrone DeShawn Darnell Marquis Andre Dre Terrell Lamar Malik Jerome Rashad Tasha Keisha Shanice Jasmine Aaliyah Tre
""".split())
# a line's first word is capitalised anyway, so there a name only counts if it cannot be an ordinary word
_ORDINARY_AT_LINE_START = {"Al", "Bill", "Bo", "Buddy", "Bubba", "Chuck", "Dale", "Don", "Dolly", "Dre", "Gus", "Holly", "Jack",
                           "Jan", "Jay", "Jo", "Kim", "Lily", "Lou", "Marley", "Max", "Mickey", "Ned", "Pat", "Ray", "Rob",
                           "Sue", "Ted", "Tre", "Wes", "Will", "Daisy", "Stan", "Glen", "Gordon", "Ross", "Carol", "Ken",
                           "Nick", "Lynn", "Otis", "Patsy", "Rosie", "Sally", "Tim", "Victor", "Wanda", "Wendy", "Lacey"}
# names that are the whole of a drink, a brand or a saying, not a person in the song
_NOT_A_PERSON = re.compile(r"\b(?:Jack Daniel'?s?|Jack and Coke|Jim Beam|Johnn(?:y|ie) Walker|Captain Morgan|Jose Cuervo|"
                           r"Evan Williams|Bloody Mary|Peeping Tom|Uncle Sam|Dear John|Lazy Susan|Jack of all|Johnny Cash|"
                           r"Johnny Law|Dale Earnhardt|Jesse James|Billy the Kid|Ray-Bans?)\b")
_TITLED = re.compile(r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Miss|Old man|Ol' man|Old lady|Uncle|Aunt|Auntie|Cousin|Pastor|Preacher|Coach|"
                     r"Sheriff|Deputy|Officer|Doc|Judge|Brother|Sister)\s+([A-Z][a-z]+)")


# Violence and the ex (1.5). An angry channel asked for rage and got the same song every time: she left, and he
# wants her dead. Some of that is the channel; all of it is a rut. These count how often a song reaches for blood
# and how often it is about her, so the planner can say "not this time" (radio.temper_budget).
VIOLENCE = re.compile(r"\b(?:kill(?:s|ed|ing)?|murder(?:s|ed)?|blood(?:y)?|bleed(?:s|ing)?|knife|knives|blade|gun|shoot|shot|bullets?"
                      r"|bur(?:y|ied)|burn(?:s|ed|ing)?|fists?|punch(?:ed)?|chok(?:e|ed|ing)|strangl(?:e|ed)|dead|die|dying|corpse|graves?"
                      r"|smash(?:ed|ing)?|shatter(?:ed)?|throat|skull|bones?|stab(?:bed)?|hunt (?:you|her|him) down|rip (?:you|her|him)"
                      r"|tear (?:you|her|him) apart|beat (?:you|her|him)|break (?:your|her|his) (?:neck|face|jaw|bones)|crush (?:you|her|him)|hurt (?:you|her|him))\b", re.I)
HER = re.compile(r"\b(?:she|her|hers|girlfriend|wife|my ex|baby girl|that woman|he|him|his|boyfriend|husband)\b", re.I)


def violence_hits(text: str) -> int:
    return len(VIOLENCE.findall(text or ""))


def about_a_lover(text: str) -> int:
    """How many lines are about a partner or an ex: the third person pronouns, girlfriend, wife, husband, ex."""
    return sum(1 for l in (text or "").splitlines() if not l.strip().startswith("[") and HER.search(l))


def street_words(text: str, allowed: str = "") -> list[str]:
    """The street family (street, streets, streetlight, sidewalk, avenue, boulevard), lower case, once each.
    A made-up street name is counted on its own (invented_streets), not here as well."""
    t = text or ""
    for s in invented_streets(t, allowed):
        t = t.replace(s, " ")
    return sorted({m.group(0).lower() for m in STREET_WORDS.finditer(t)})


def first_names(text: str, allowed: str = "") -> list[str]:
    """People called by name: Sarah, old man Jenkins, Uncle Ray. Most named first. Section tags are skipped;
    a drink or a saying that happens to hold a name (Jack and Coke, Peeping Tom) is not a person."""
    seen: dict[str, int] = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("["):
            continue
        s = _NOT_A_PERSON.sub(" ", s)
        for m in _TITLED.finditer(s):
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        words = list(re.finditer(r"[A-Za-z][A-Za-z'’]*", s))
        for i, m in enumerate(words):
            w = m.group(0).replace("’", "'")
            base = w[:-2] if w.endswith("'s") else w
            if base not in FIRST_NAMES:
                continue
            if s[m.end():m.end() + 1] == "-":
                continue
            if i == 0 and base in _ORDINARY_AT_LINE_START and not s[m.end():].startswith((",", "'s")):
                continue
            seen[base] = seen.get(base, 0) + 1
    low = (allowed or "").lower()
    names = [n for n in seen if not (low and re.search(r"\b" + re.escape(n.lower()) + r"\b", low))]
    return sorted(names, key=lambda n: (-seen[n], n))


def people_places(text: str, streets_ok: bool = True, names_ok: bool = True, allowed: str = "") -> dict[str, list[str]]:
    """What this song says that the radio says too often: a made-up street name (never kept), the word street (kept
    when the channel has left room for it) and people's first names (one kept when the channel has left room)."""
    names = first_names(text, allowed)
    return {"named_streets": invented_streets(text, allowed),
            "street_words": [] if streets_ok else street_words(text, allowed),
            "names": names if not names_ok else names[1:]}


def people_places_flat(found: dict[str, list[str]]) -> list[str]:
    return list(found.get("named_streets") or []) + list(found.get("street_words") or []) + list(found.get("names") or [])


def people_places_bits(found: dict[str, list[str]]) -> list[str]:
    """The rewrite asks for these, in words the writer can act on. No example replacements: an example handed over
    for every song becomes the next word every song uses."""
    bits = []
    if found.get("named_streets"):
        bits.append("these are made-up street names and real songs almost never name a street, so say those lines "
                    "without naming any street or road: " + ", ".join(found["named_streets"]))
    if found.get("street_words"):
        bits.append("this song may not say " + " or ".join(found["street_words"]) +
                    ": say those lines without it")
    if found.get("names"):
        bits.append("nobody in this song is called by name, so replace each of these names with what that person is "
                    "to the singer, or with you, she or he: " + ", ".join(found["names"]))
    return bits


def focused_instruction(worn: list[str], found: dict[str, list[str]]) -> str:
    """A second rewrite about nothing but what the first one kept."""
    bits = []
    if worn:
        bits.append("these words are not allowed on this radio, so replace each one with a plain, real thing a person "
                    "would say: " + ", ".join(worn))
    bits += people_places_bits(found)
    if not bits:
        return ""
    return ("Change ONLY the lines that contain the things listed below; every other line stays exactly as it is. "
            + " Also, ".join(bits) + ".")


def score(feat: dict[str, Any], state: dict[str, Any], lvl: dict[str, Any], licence: set[str] | None = None) -> dict[str, Any]:
    """0 to 1, where 1 is a song that shares nothing with the recent ones. Returns the reasons too."""
    if not lvl or not state.get("n"):
        return {"score": 1.0, "reasons": [], "words": [], "phrases": [], "rhymes": []}
    licence = licence or set()
    worn = set(leaning_on(state, lvl, licence))
    song_words = [w for w in (feat.get("words") or []) if w not in licence]
    hit_words = sorted(worn & set(song_words))
    word_pen = min(0.45, 0.09 * len(hit_words))

    phrase_floor = lvl["phrase_songs"]
    hit_phrases = sorted({p for p in (feat.get("phrases") or []) if (state.get("phrases") or {}).get(p, 0) >= phrase_floor
                          and len(p.split()) >= 3}, key=lambda p: -len(p))[:6]
    phrase_pen = min(0.30, 0.08 * len(hit_phrases))

    hit_rhymes = sorted({r for r in (feat.get("rhymes") or []) if (state.get("rhymes") or {}).get(r, 0) >= lvl["rhyme_songs"]})
    rhyme_pen = min(0.15, 0.05 * len(hit_rhymes))

    # one bucket carrying the song while the channel already leans there
    bucket_pen = 0.0
    b = feat.get("buckets") or {}
    tot = sum(b.values())
    if tot:
        top_b, top_n = max(b.items(), key=lambda x: x[1])
        share = top_n / tot
        recent = (state.get("buckets") or {}).get(top_b, 0)
        if share > 0.33 and recent > 0.22:
            bucket_pen = 0.10

    open_pen = 0.0
    opens = state.get("openings") or {}
    if opens:
        common = max(opens.items(), key=lambda x: x[1])
        if feat.get("opening") == common[0] and common[1] / max(1, state["n"]) > 0.6:
            open_pen = 0.08
    if feat.get("opening_words") and (state.get("opening_words") or {}).get(feat["opening_words"], 0) >= 2:
        open_pen += 0.10

    total = max(0.0, 1.0 - (word_pen + phrase_pen + rhyme_pen + bucket_pen + open_pen))
    reasons = []
    if hit_words:
        reasons.append("words the channel keeps using: " + ", ".join(hit_words[:8]))
    if hit_phrases:
        reasons.append("phrases already in recent songs: " + "; ".join(hit_phrases[:4]))
    if hit_rhymes:
        reasons.append("rhymes already leaned on: " + ", ".join(r.replace("/", "/") for r in hit_rhymes[:4]))
    if bucket_pen:
        reasons.append("most of the images come from one drawer the channel already opens a lot")
    if open_pen:
        reasons.append("it opens the way the recent songs open")
    return {"score": round(total, 3), "reasons": reasons, "words": hit_words, "phrases": hit_phrases, "rhymes": hit_rhymes}


def verdict(result: dict[str, Any], lvl: dict[str, Any]) -> str:
    """'keep' or 'rewrite'."""
    if not lvl or not lvl.get("floor"):
        return "keep"
    return "rewrite" if result.get("score", 1.0) < lvl["floor"] else "keep"


def rewrite_instruction(result: dict[str, Any]) -> str:
    """What to tell the writer when one pass is worth it. Named, specific, and it keeps everything else."""
    bits = []
    if result.get("banned"):
        bits.append("these words are not allowed on this radio at all, so replace every one with a plain, "
                    "real thing a person would say: " + ", ".join(result["banned"]))
    if result.get("words"):
        bits.append("these words have been in too many recent songs on this channel, so swap each one for a "
                    "different concrete thing: " + ", ".join(result["words"][:8]))
    if result.get("phrases"):
        bits.append("these exact phrases have already been used, so write the thought a different way: "
                    + "; ".join(f'"{p}"' for p in result["phrases"][:4]))
    if result.get("rhymes"):
        bits.append("these rhyme pairs are worn out, so end those lines on different words: "
                    + ", ".join(r.replace("/", " / ") for r in result["rhymes"][:4]))
    bits += people_places_bits(result.get("people_places") or {})
    if not bits:
        return ""
    return ("Change ONLY the lines that contain the things listed below. Every other line, the section tags, the "
            "structure and the story stay exactly as they are, word for word. " + " Also, ".join(bits) +
            ". Keep each rewritten line the same length and the same meaning as the line it replaces.")
