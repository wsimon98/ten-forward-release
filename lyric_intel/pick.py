"""pick.py — a channel told what to cover: artists, a genre, or years.

By default a channel covers the acts its own description names (corpus.artists_for), so Blue Hour covers the moody
pop it also writes. The owner can point a channel's covers somewhere else instead (station fields `cover_from` +
`cover_pick`), and what was typed is read here:

  artists  "staind, seether, rise against"       each name looked up on MusicBrainz (spelling and other names
                                                  forgiven: "canible corpse" is Cannibal Corpse), then lrclib is
                                                  asked for songs by that act
  genre    "country, southern rock, gangster rap" each genre matched against the table below (nicknames, spelling and
                                                  near misses forgiven); a genre the table does not know, or one with
                                                  a decade in it ("90s country"), goes to the lyric writer, which
                                                  names the acts it most likely means
  years    "1930-1940", "1999", "the 90s"        the lyric writer names well-known songs first released in those
                                                  years (other words typed with them, "90s country", narrow it), and
                                                  each one is looked up on lrclib by act and title

The reading is kept per channel (setting cover_pick:<id>) and shown back in the channel editor ("country -> Garth
Brooks, George Strait, ..."), so a wrong guess is seen and can be typed again. The songs are cached like any other
lrclib song but listed by id in the reading: they never join the channel's own lane, so the measuring half of
lyric_intel keeps comparing the channel's writing with its own kind of song.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from . import corpus, features, finder, lrclib

LOG = logging.getLogger("tenforward.lyric_intel.pick")

KINDS = ("artists", "genre", "years")
MAX_TEXT = 1000         # a long artist list ran past 300 and lost its last name half-typed
PER_ACT = 8              # songs taken per act on a first reading; a refill goes deeper
YEAR_SONGS = 40          # songs the writer is asked to name for a run of years
MAX_ACTS = 40
REFILL_BELOW = 3         # unsung songs left before the reading is topped up
RETRY_FAILED_S = 30 * 60
BUSY_RETRY_S = 10 * 60
PACE_S = 0.8             # added to lrclib's own 0.4 s gap: about one lookup a second
MAX_DEPTH = 4

_running: set[str] = set()
_lock = threading.Lock()
_llm_wait = None         # the server hands in its "wait until the graphics card has room for the writer"


def set_llm_wait(fn) -> None:
    global _llm_wait
    _llm_wait = fn


# --------------------------------------------------------------------------- genres the radio knows by heart
# canonical name: (what people call it, the acts that define it). Acts sing in English and are on lrclib.
GENRES: dict[str, tuple[list[str], list[str]]] = {
    "country": (["country", "country music", "modern country", "new country", "contemporary country", "nashville"],
                ["Garth Brooks", "George Strait", "Alan Jackson", "Brooks & Dunn", "Tim McGraw", "Kenny Chesney", "Toby Keith",
                 "Luke Bryan", "Morgan Wallen", "Chris Stapleton", "Carrie Underwood", "Miranda Lambert", "Eric Church", "Luke Combs"]),
    "classic country": (["classic country", "old country", "traditional country", "honky tonk", "honkytonk", "old school country"],
                        ["Johnny Cash", "Merle Haggard", "Willie Nelson", "Waylon Jennings", "George Jones", "Hank Williams", "Patsy Cline",
                         "Loretta Lynn", "Dolly Parton", "Conway Twitty", "Buck Owens", "Tammy Wynette"]),
    "outlaw country": (["outlaw country", "outlaw", "red dirt", "texas country"],
                       ["Waylon Jennings", "Willie Nelson", "David Allan Coe", "Kris Kristofferson", "Hank Williams Jr.", "Jamey Johnson",
                        "Sturgill Simpson", "Cody Jinks", "Whitey Morgan and the 78's", "Turnpike Troubadours"]),
    "southern rock": (["southern rock", "southern", "swamp rock"],
                      ["Lynyrd Skynyrd", "The Allman Brothers Band", "ZZ Top", "The Marshall Tucker Band", "The Charlie Daniels Band",
                       "Molly Hatchet", "38 Special", "Blackberry Smoke", "Black Stone Cherry", "The Black Crowes", "Drive-By Truckers"]),
    "bro country": (["bro country", "truck country", "party country"],
                    ["Florida Georgia Line", "Luke Bryan", "Jason Aldean", "Sam Hunt", "Cole Swindell", "Thomas Rhett", "Brantley Gilbert", "Chase Rice"]),
    "americana": (["americana", "alt country", "alternative country", "roots", "roots rock"],
                  ["Jason Isbell", "Tyler Childers", "Zach Bryan", "Colter Wall", "The Avett Brothers", "Lucinda Williams", "Wilco",
                   "Ryan Adams", "Steve Earle", "Gillian Welch"]),
    "bluegrass": (["bluegrass", "newgrass", "mountain music"],
                  ["Bill Monroe", "Ralph Stanley", "Alison Krauss", "Ricky Skaggs", "Billy Strings", "Del McCoury", "The Stanley Brothers",
                   "Nickel Creek", "Old Crow Medicine Show"]),
    "folk": (["folk", "folk music", "singer songwriter", "acoustic folk", "60s folk", "protest folk"],
             ["Bob Dylan", "Joni Mitchell", "Simon & Garfunkel", "James Taylor", "Cat Stevens", "Gordon Lightfoot", "John Prine",
              "Joan Baez", "Woody Guthrie", "Pete Seeger"]),
    "indie folk": (["indie folk", "folk pop", "stomp clap", "folk rock"],
                   ["Bon Iver", "Fleet Foxes", "Mumford & Sons", "The Lumineers", "Iron & Wine", "Noah Kahan", "Hozier",
                    "The Head and the Heart", "Of Monsters and Men"]),
    "gangsta rap": (["gangsta rap", "gangster rap", "gangsta", "gangster", "west coast rap", "west coast", "g funk", "gfunk"],
                    ["2Pac", "Dr. Dre", "Snoop Dogg", "Ice Cube", "N.W.A", "Eazy-E", "The Game", "DMX", "The Notorious B.I.G.",
                     "Mobb Deep", "50 Cent"]),
    "90s hip hop": (["90s hip hop", "boom bap", "golden age hip hop", "old school hip hop", "old school rap", "east coast rap", "east coast"],
                    ["Nas", "Wu-Tang Clan", "The Notorious B.I.G.", "A Tribe Called Quest", "Mobb Deep", "Gang Starr", "Mos Def",
                     "Big L", "Jay-Z", "Busta Rhymes"]),
    "hip hop": (["hip hop", "hiphop", "rap", "rap music", "rapping", "rappers"],
                ["Eminem", "Kendrick Lamar", "Drake", "J. Cole", "Jay-Z", "Kanye West", "Lil Wayne", "Nas", "50 Cent", "OutKast"]),
    "trap": (["trap", "trap music", "trap rap", "atlanta rap"],
             ["Future", "Young Thug", "Migos", "21 Savage", "Lil Baby", "Gucci Mane", "Travis Scott", "Jeezy", "T.I.", "Waka Flocka Flame"]),
    "drill": (["drill", "chicago drill", "ny drill", "brooklyn drill", "drill rap"],
              ["Chief Keef", "Lil Durk", "King Von", "Pop Smoke", "Fivio Foreign", "G Herbo", "Polo G", "Lil Reese"]),
    "southern rap": (["southern rap", "dirty south", "crunk", "memphis rap", "southern hip hop"],
                     ["OutKast", "UGK", "Three 6 Mafia", "Ludacris", "Lil Jon", "T.I.", "Juvenile", "Master P", "Project Pat", "8Ball & MJG"]),
    "horrorcore": (["horrorcore", "horror rap", "juggalo"],
                   ["Insane Clown Posse", "Twiztid", "Brotha Lynch Hung", "Geto Boys", "Tech N9ne", "Esham", "Necro"]),
    "emo rap": (["emo rap", "sad rap", "soundcloud rap"],
                ["Juice WRLD", "Lil Peep", "XXXTENTACION", "Trippie Redd", "Lil Uzi Vert", "iann dior"]),
    "pop": (["pop", "pop music", "top 40", "mainstream pop", "radio pop", "pop hits"],
            ["Taylor Swift", "Ed Sheeran", "Ariana Grande", "Katy Perry", "Bruno Mars", "Dua Lipa", "Harry Styles", "Justin Bieber",
             "Adele", "Billie Eilish", "Olivia Rodrigo", "The Weeknd"]),
    "sad pop": (["sad pop", "bedroom pop", "alt pop", "dark pop", "sad boy pop", "sadboy pop", "moody pop"],
                ["Joji", "Billie Eilish", "Lauv", "Conan Gray", "Clairo", "Lana Del Rey", "Gracie Abrams", "Jeremy Zucker", "Justin Bieber"]),
    "80s pop": (["80s pop", "eighties pop", "synth pop", "synthpop", "new wave", "80s new wave"],
                ["Madonna", "Michael Jackson", "Duran Duran", "Tears for Fears", "a-ha", "Cyndi Lauper", "Whitney Houston", "Wham!",
                 "Depeche Mode", "The Human League", "Prince"]),
    "boy bands": (["boy band", "boy bands", "teen pop", "90s pop", "girl groups"],
                  ["Backstreet Boys", "*NSYNC", "Britney Spears", "Christina Aguilera", "New Kids on the Block", "One Direction",
                   "Jonas Brothers", "Spice Girls", "Hanson"]),
    "dance": (["dance", "dance pop", "edm", "club", "club music", "house", "electro pop"],
              ["Calvin Harris", "David Guetta", "Avicii", "Rihanna", "Lady Gaga", "Pitbull", "Kesha", "The Chainsmokers"]),
    "disco": (["disco", "funk", "70s disco", "disco funk"],
              ["Bee Gees", "Donna Summer", "Chic", "Earth, Wind & Fire", "Gloria Gaynor", "KC and the Sunshine Band", "ABBA",
               "Kool & the Gang"]),
    "r&b": (["r&b", "rnb", "r and b", "rhythm and blues", "neo soul", "contemporary r&b", "90s r&b"],
            ["Usher", "Alicia Keys", "Beyoncé", "Boyz II Men", "TLC", "Mary J. Blige", "Brian McKnight", "SZA", "Frank Ocean", "Aaliyah"]),
    "soul": (["soul", "motown", "classic soul", "60s soul", "old soul", "northern soul"],
             ["Marvin Gaye", "Aretha Franklin", "Otis Redding", "Sam Cooke", "Al Green", "Stevie Wonder", "The Temptations",
              "Smokey Robinson", "Bill Withers", "Etta James"]),
    "blues": (["blues", "delta blues", "chicago blues", "electric blues", "blues rock"],
              ["B.B. King", "Muddy Waters", "Howlin' Wolf", "John Lee Hooker", "Stevie Ray Vaughan", "Robert Johnson", "Buddy Guy",
               "Albert King", "Gary Clark Jr."]),
    "jazz standards": (["jazz", "vocal jazz", "swing", "big band", "crooner", "crooners", "jazz standards", "standards", "rat pack"],
                       ["Frank Sinatra", "Ella Fitzgerald", "Billie Holiday", "Nat King Cole", "Louis Armstrong", "Dean Martin",
                        "Tony Bennett", "Sarah Vaughan", "Bing Crosby", "Peggy Lee", "Michael Bublé"]),
    "gospel": (["gospel", "christian", "worship", "christian rock", "ccm", "praise", "hymns", "church music"],
               ["Mahalia Jackson", "Kirk Franklin", "MercyMe", "Casting Crowns", "Hillsong UNITED", "Chris Tomlin", "Lauren Daigle",
                "Third Day", "Switchfoot"]),
    "oldies": (["oldies", "50s", "fifties", "rock and roll", "rock n roll", "rockabilly", "doo wop", "doowop"],
               ["Elvis Presley", "Chuck Berry", "Buddy Holly", "Little Richard", "Jerry Lee Lewis", "The Platters", "The Drifters",
                "Ricky Nelson", "The Everly Brothers", "Fats Domino"]),
    "60s rock": (["60s rock", "sixties rock", "british invasion", "60s", "sixties"],
                 ["The Beatles", "The Rolling Stones", "The Kinks", "The Who", "The Animals", "The Beach Boys", "The Byrds", "The Doors",
                  "Creedence Clearwater Revival", "The Zombies"]),
    "classic rock": (["classic rock", "70s rock", "arena rock", "dad rock", "rock", "rock music", "rock and roll classics"],
                     ["Led Zeppelin", "The Rolling Stones", "Queen", "Eagles", "Fleetwood Mac", "Lynyrd Skynyrd", "Tom Petty and the Heartbreakers",
                      "Bruce Springsteen", "AC/DC", "Aerosmith", "Bob Seger", "Creedence Clearwater Revival"]),
    "heartland rock": (["heartland rock", "heartland", "blue collar rock", "working class rock"],
                       ["Bruce Springsteen", "John Mellencamp", "Bob Seger", "Tom Petty and the Heartbreakers", "Bryan Adams", "The Gaslight Anthem"]),
    "soft rock": (["soft rock", "yacht rock", "70s soft rock", "adult contemporary"],
                  ["Michael McDonald", "Christopher Cross", "Hall & Oates", "Toto", "Steely Dan", "Kenny Loggins", "The Doobie Brothers",
                   "Air Supply", "Ambrosia"]),
    "hair metal": (["hair metal", "glam metal", "80s metal", "glam rock", "80s rock", "hair bands"],
                   ["Bon Jovi", "Def Leppard", "Mötley Crüe", "Poison", "Guns N' Roses", "Skid Row", "Whitesnake", "Warrant", "Cinderella",
                    "Twisted Sister", "Scorpions"]),
    "grunge": (["grunge", "seattle", "90s grunge", "seattle sound"],
               ["Nirvana", "Pearl Jam", "Soundgarden", "Alice in Chains", "Stone Temple Pilots", "Bush", "Mudhoney", "Temple of the Dog"]),
    "post-grunge": (["post grunge", "postgrunge", "butt rock", "radio rock", "2000s rock", "divorced dad rock", "active rock"],
                    ["Nickelback", "Creed", "3 Doors Down", "Staind", "Seether", "Puddle of Mudd", "Shinedown", "Daughtry",
                     "Theory of a Deadman", "Hinder", "Breaking Benjamin"]),
    "hard rock": (["hard rock", "modern hard rock", "heavy rock"],
                  ["AC/DC", "Guns N' Roses", "Van Halen", "Aerosmith", "Shinedown", "Five Finger Death Punch", "Godsmack", "Disturbed",
                   "Halestorm", "Alter Bridge", "Skillet"]),
    "alternative rock": (["alternative", "alternative rock", "alt rock", "90s alternative", "modern rock", "90s rock"],
                         ["Foo Fighters", "Red Hot Chili Peppers", "Weezer", "Radiohead", "R.E.M.", "The Smashing Pumpkins", "Live",
                          "Collective Soul", "Matchbox Twenty", "Third Eye Blind", "The Killers"]),
    "indie rock": (["indie", "indie rock", "garage rock", "garage rock revival"],
                   ["Arctic Monkeys", "The Strokes", "The White Stripes", "Modest Mouse", "Death Cab for Cutie", "The Shins",
                    "Vampire Weekend", "Kings of Leon", "The National", "Cage the Elephant"]),
    "punk": (["punk", "punk rock", "70s punk", "classic punk", "street punk", "oi"],
             ["Ramones", "The Clash", "Sex Pistols", "Misfits", "Bad Religion", "Dead Kennedys", "Social Distortion", "The Offspring",
              "Rancid", "NOFX"]),
    "pop punk": (["pop punk", "skate punk", "warped tour", "2000s pop punk"],
                 ["blink-182", "Green Day", "Sum 41", "Good Charlotte", "Simple Plan", "New Found Glory", "Yellowcard", "All Time Low",
                  "Fall Out Boy"]),
    "emo": (["emo", "emo rock", "screamo", "mall emo", "2000s emo"],
            ["My Chemical Romance", "Taking Back Sunday", "Dashboard Confessional", "Brand New", "Jimmy Eat World", "The Used",
             "Hawthorne Heights", "Senses Fail", "Paramore"]),
    "melodic hardcore": (["melodic hardcore", "hardcore punk", "hardcore", "melodic punk"],
                         ["Rise Against", "Pennywise", "Strike Anywhere", "Comeback Kid", "Strung Out", "Ignite", "Bad Religion"]),
    "metalcore": (["metalcore", "post hardcore", "posthardcore", "melodic metalcore"],
                  ["Atreyu", "Killswitch Engage", "Bullet For My Valentine", "We Came As Romans", "Avenged Sevenfold", "Trivium",
                   "As I Lay Dying", "August Burns Red", "Asking Alexandria", "A Day to Remember"]),
    "nu metal": (["nu metal", "numetal", "rap metal", "rap rock", "alternative metal", "alt metal"],
                 ["Korn", "Limp Bizkit", "Linkin Park", "Disturbed", "P.O.D.", "Papa Roach", "Slipknot", "System of a Down", "Staind",
                  "Godsmack", "Deftones", "Drowning Pool"]),
    "heavy metal": (["metal", "heavy metal", "classic metal", "thrash", "thrash metal", "speed metal", "80s thrash"],
                    ["Metallica", "Iron Maiden", "Black Sabbath", "Judas Priest", "Megadeth", "Slayer", "Pantera", "Ozzy Osbourne",
                     "Dio", "Motörhead", "Anthrax"]),
    "death metal": (["death metal", "brutal death metal", "deathcore", "gore metal", "grindcore", "old school death metal"],
                    ["Cannibal Corpse", "Deicide", "Obituary", "Morbid Angel", "Death", "Six Feet Under", "Suffocation", "Whitechapel",
                     "Chelsea Grin"]),
    "black metal": (["black metal", "symphonic black metal"],
                    ["Cradle of Filth", "Dimmu Borgir", "Behemoth", "Emperor", "Immortal", "Dark Funeral"]),
    "reggae": (["reggae", "roots reggae", "ska", "dub", "reggae rock"],
               ["Bob Marley & The Wailers", "Peter Tosh", "Jimmy Cliff", "UB40", "Toots and the Maytals", "Sublime", "Steel Pulse",
                "Damian Marley", "Rebelution", "Slightly Stoopid"]),
}

_FILLER = re.compile(r"\b(music|songs?|stuff|tunes|some|the|style|type|kind of|genre|like)\b")
_DECADE = re.compile(r"\b(?:the\s+)?'?(\d{2}|\d{4})'?s\b", re.I)
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")


def _norm_genre(text: str) -> str:
    """'Country Music' -> 'country', 'R&B' -> 'r and b', 'post-grunge' -> 'post grunge'."""
    return " ".join(_FILLER.sub(" ", finder.fold(text)).split())


_ALIASES: dict[str, str] = {}
for _canon, (_names, _acts) in GENRES.items():
    for _n in [_canon] + _names:
        if _norm_genre(_n):
            _ALIASES.setdefault(_norm_genre(_n), _canon)


def split_items(text: str) -> list[str]:
    """'staind, seether; rise against' -> three. Never splits on 'and': it is in too many band names."""
    out = []
    for part in re.split(r"[,;\n/|]+", text or ""):
        part = " ".join(part.split()).strip(" .\"'()")
        if part and part.lower() not in {p.lower() for p in out}:
            out.append(part[:80])
    return out[:MAX_ACTS]


def _like(typed: str, act: dict[str, Any]) -> bool:
    """True when an act MusicBrainz found is plausibly the one typed: misspelt ('canible corpse', 'nickleback'),
    cut short ('the cadillac thr') or another name it goes by ('three doors down' for 3 Doors Down)."""
    t = finder.name_key(typed)
    if not t:
        return False
    for k in act.get("keys") or {finder.name_key(act.get("name") or "")}:
        if not k:
            continue
        if k == t or (len(t) >= 5 and k.startswith(t)) or difflib.SequenceMatcher(None, t, k).ratio() >= 0.75:
            return True
    return False


def match_genre(text: str) -> tuple[str | None, str]:
    """(canonical genre, how it was matched) for one typed genre, or (None, '') when the table does not know it."""
    t = _norm_genre(text)
    if not t:
        return None, ""
    if t in _ALIASES:
        return _ALIASES[t], "known"
    squash = {k.replace(" ", ""): v for k, v in _ALIASES.items()}
    if t.replace(" ", "") in squash:
        return squash[t.replace(" ", "")], "known"
    close = difflib.get_close_matches(t, list(_ALIASES), n=1, cutoff=0.82)
    if close:
        return _ALIASES[close[0]], "close match"
    return None, ""


def parse_years(text: str) -> tuple[list[tuple[int, int]], str]:
    """'1930-1940' / '1999' / 'the 90s' / "'60s and 1985" -> ([(start, end), ...], the other words typed with them)."""
    t = (text or "").lower().replace("–", "-").replace("—", "-")
    spans: list[tuple[int, int]] = []

    def year_of(tok: str) -> int:
        n = int(tok)
        if n < 100:
            return (1900 + n) if n >= 30 else (2000 + n)
        return n

    rest = t
    for m in re.finditer(r"\b(1[89]\d\d|20\d\d)\s*(?:-|to|through|thru|until)\s*(1[89]\d\d|20\d\d|\d{2})\b", t):
        a, b = int(m.group(1)), m.group(2)
        b = int(b) if len(b) == 4 else (a // 100) * 100 + int(b)
        spans.append((min(a, b), max(a, b)))
        rest = rest.replace(m.group(0), " ")
    for m in _DECADE.finditer(rest):
        d = year_of(m.group(1))
        d -= d % 10
        spans.append((d, d + 9))
    rest = _DECADE.sub(" ", rest)
    for m in _YEAR.finditer(rest):
        y = int(m.group(1))
        spans.append((y, y))
    rest = _YEAR.sub(" ", rest)
    now = time.gmtime().tm_year
    spans = sorted({(max(1900, a), min(now, b)) for a, b in spans if a <= now})
    words = " ".join(w.strip("'") for w in re.sub(r"[^a-z&' ]+", " ", rest).split()
                     if w.strip("'") and w.strip("'") not in {"to", "and", "the", "from", "through", "s", "years", "year", "era", "early", "late", "mid"})
    return spans, words


def years_label(spans: list[tuple[int, int]]) -> str:
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


# --------------------------------------------------------------------------- the station's choice
def choice(station: dict[str, Any]) -> tuple[str, str]:
    kind = str((station or {}).get("cover_from") or "").strip().lower()
    text = " ".join(str((station or {}).get("cover_pick") or "").split())[:MAX_TEXT]
    return (kind, text) if kind in KINDS and text else ("", "")


def active(station: dict[str, Any]) -> bool:
    return bool(choice(station)[0])


def signature(station: dict[str, Any]) -> str:
    kind, text = choice(station)
    return hashlib.sha1(f"{kind}|{text.lower()}".encode()).hexdigest()[:10] if kind else ""


def reading(db, station: dict[str, Any]) -> dict[str, Any] | None:
    """What the channel's covers choice was read as, if it was read for what is typed there now."""
    r = db.setting(f"cover_pick:{station['id']}", None)
    if not isinstance(r, dict) or r.get("sig") != signature(station):
        return None
    return r


def summary(db, station: dict[str, Any]) -> dict[str, Any]:
    """For the channel editor: {kind, text, status, note, read_as: [...], songs, unsung}."""
    kind, text = choice(station)
    if not kind:
        return {"kind": "", "status": "off"}
    r = reading(db, station)
    if not r:
        return {"kind": kind, "text": text, "status": "working" if station["id"] in _running else "waiting",
                "note": "Reading what you typed…"}
    out = {k: r.get(k) for k in ("kind", "text", "status", "note", "items", "found")}
    out["unsung"] = len(unused(db, station)) if r.get("status") == "ready" else 0
    out["working"] = station["id"] in _running
    return out


# --------------------------------------------------------------------------- the songs
def _sung_ids(db) -> set[int]:
    sung = set()
    for s in db.query("songs", "cover_of IS NOT NULL", limit=2000):
        c = s.get("cover_of") or {}
        if isinstance(c, dict) and c.get("id"):
            try:
                sung.add(int(c["id"]))
            except (TypeError, ValueError):
                pass
    return sung


def unused(db, station: dict[str, Any]) -> list[dict[str, Any]]:
    """Songs of the channel's reading, with their words kept, that no channel has sung yet."""
    r = reading(db, station)
    ids = [int(i) for i in (r or {}).get("ids") or [] if str(i).lstrip("-").isdigit()]
    if not ids:
        return []
    rows: list[dict[str, Any]] = []
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        rows += db.query("lrclib_cache", f"plain IS NOT NULL AND id IN ({','.join('?' for _ in chunk)})", tuple(chunk),
                         order="fetched DESC")
    sung = _sung_ids(db)
    return [row for row in rows if int(row.get("id") or 0) not in sung]


def _keep(db, rec: dict[str, Any], keep_text: bool) -> int | None:
    """Cache one lrclib record (or find it already cached) and return its id when it can be sung."""
    try:
        plain = (rec.get("plainLyrics") or "").strip()
        rid = int(rec.get("id") or 0)
        if not rid or rec.get("instrumental") or len(plain) < 200 or not corpus.looks_english(plain):
            return None
        have = db.get("lrclib_cache", rid)
        if have:
            if keep_text and not have.get("plain"):
                db.update("lrclib_cache", rid, plain=plain)
            return rid
        h = corpus.text_hash(plain)
        same = db.query("lrclib_cache", "text_hash=?", (h,), order="fetched DESC", limit=1)
        if same:
            if keep_text and not same[0].get("plain"):
                db.update("lrclib_cache", same[0]["id"], plain=plain)
            return int(same[0]["id"])
        db.insert("lrclib_cache", {
            "id": rid, "track": rec.get("trackName") or rec.get("name"), "artist": rec.get("artistName"),
            "album": rec.get("albumName"), "duration": rec.get("duration"), "instrumental": 0, "lang": "en",
            "text_hash": h, "plain": plain if keep_text else None, "lane": "pick", "fetched": time.time(),
            "features": features.extract(plain)})
        return rid
    except Exception:
        LOG.debug("pick: could not keep a record", exc_info=True)
        return None


class _Busy(Exception):
    """lrclib did not answer after a few tries: the reading is kept as far as it got and finished later."""


def _lrclib(fn, *args, **kw):
    """lrclib throws 503s often, and one failed background call rests it for five minutes, which used to end a
    reading after its first act. A reading asks like a person waiting (asked=True) and tries three times."""
    for attempt in range(3):
        time.sleep(PACE_S)       # nobody is waiting on a reading; lrclib answers 503 to a steady 0.4 s pace
        try:
            return fn(*args, asked=True, **kw)
        except lrclib.Unreachable:
            time.sleep(3 + 5 * attempt)
    raise _Busy()


def _songs_by(db, act: str, keys: set[str], limit: int, keep_text: bool) -> list[int]:
    """Up to `limit` songs by one act, one per title."""
    out: list[int] = []
    titles: set[str] = set()
    names = sorted(keys or {finder.name_key(act)})
    # the song's own entry before the uploads ("Song (Official Video)", "Act - Song"): same words, cleaner title
    for rec in sorted(_lrclib(lrclib.search, q=act), key=finder._messy):
        who = finder.fold(rec.get("artistName") or "").replace(" ", "")
        if not any(k and k in who for k in names):
            continue
        title = finder.fold(finder.tidy_title(rec))
        if not title or title in titles or not finder._singable(rec):
            continue
        rid = _keep(db, rec, keep_text)
        if rid:
            titles.add(title)
            out.append(rid)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- asking the lyric writer
def _ask(system: str, user: str, max_tokens: int = 1400) -> Any:
    import llm
    if _llm_wait:
        try:
            _llm_wait()
        except Exception:
            pass
    raw = llm.ask_json(system, user, max_tokens=max_tokens, temperature=0.4, timeout=180)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("the writer did not answer with json")
    return json.loads(m.group(0))


def guess_genre(text: str) -> dict[str, Any] | None:
    """The writer's reading of a genre the table does not know: {"genre": "...", "artists": [...]}."""
    system = ("You know popular music very well. Somebody typed a genre of music they want songs from. It may be misspelled, "
              "a nickname, a joke name, or a description. Work out what they most likely meant. Answer with JSON only: "
              '{"genre": "what they meant, in a few words", "artists": ["12 well-known acts of exactly that kind, most famous first"]}. '
              "Only acts that sing in English. No commentary.")
    try:
        data = _ask(system, f"They typed: {text}", 600)
        acts = [" ".join(str(a).split())[:60] for a in data.get("artists") or [] if str(a).strip()]
        if not acts:
            return None
        return {"genre": " ".join(str(data.get("genre") or text).split())[:60], "artists": acts[:14]}
    except Exception as e:
        LOG.info("pick: the writer could not read the genre %r (%s)", text, e)
        return None


def songs_from_years(spans: list[tuple[int, int]], hint: str = "", n: int = YEAR_SONGS, skip: list[str] | None = None) -> list[dict[str, Any]]:
    """Well-known songs first released in those years, as the writer remembers them: [{"artist", "title", "year"}]."""
    label = years_label(spans)
    system = ("You know popular music and the charts very well. Answer with JSON only: "
              '{"songs": [{"artist": "...", "title": "...", "year": 1999}]}. Real songs with English lyrics that people would '
              "recognise, each first released in the years asked for, spread across those years and across the different "
              "kinds of music popular then. Never invent a song. No commentary.")
    user = f"List {n} songs first released in {label}."
    if hint:
        user += f" Only songs of this kind: {hint}."
    if skip:
        user += " Do not list any of these again: " + "; ".join(skip[:60])
    try:
        data = _ask(system, user, 2200)
    except Exception as e:
        LOG.info("pick: the writer could not list songs from %s (%s)", label, e)
        return []
    out = []
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    for s in data.get("songs") or []:
        if not isinstance(s, dict):
            continue
        artist, title = " ".join(str(s.get("artist") or "").split()), " ".join(str(s.get("title") or "").split())
        try:
            year = int(s.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if artist and title and (not year or lo - 1 <= year <= hi + 1):
            out.append({"artist": artist[:80], "title": title[:100], "year": year or None})
    return out


def _song(db, artist: str, title: str, keep_text: bool) -> int | None:
    rec = _lrclib(lrclib.get, title, artist)
    if rec and finder._singable(rec):
        return _keep(db, rec, keep_text)
    want = finder.name_key(artist)
    for rec in _lrclib(lrclib.search, track_name=title, artist_name=artist)[:5]:
        if finder._singable(rec) and want and want in finder.fold(rec.get("artistName") or "").replace(" ", ""):
            return _keep(db, rec, keep_text)
    return None


# --------------------------------------------------------------------------- reading what was typed
def resolve(db, station: dict[str, Any], depth: int = 1, merge: bool = False) -> dict[str, Any]:
    """Read the channel's covers choice, find its songs, keep the reading. Background work: never from a song plan.
    `depth` > 1 goes further than last time; `merge` keeps the songs already found (a reading lrclib cut short)."""
    kind, text = choice(station)
    sig = signature(station)
    key = f"cover_pick:{station['id']}"
    old = db.setting(key, None)
    old = old if isinstance(old, dict) and old.get("sig") == sig else None
    keep_text = bool(_cfg("lrclib_keep_text", False))
    base = {"sig": sig, "kind": kind, "text": text, "at": time.time(), "depth": depth}
    if not kind:
        return {}
    if not _cfg("lrclib_enabled", False) or not keep_text:
        r = {**base, "status": "failed", "note": "Covers need *Learn from real songs* and *Keep the real words too* "
                                                 "switched on in Settings."}
        db.set_setting(key, r)
        return r
    ids: list[int] = list((old or {}).get("ids") or []) if (depth > 1 or merge) else []
    items: list[dict[str, Any]] = []
    per_act = PER_ACT * depth
    busy = 0          # lookups lrclib did not answer; three in a row and the rest waits for the next try

    if kind in ("artists", "genre"):
        acts: list[tuple[str, set[str]]] = []
        for typed in split_items(text):
            canon, how = match_genre(typed)
            if kind == "artists":
                found = finder.acts(typed)
                if found is None:            # MusicBrainz did not answer; once more before taking the name as typed
                    time.sleep(3)
                    found = finder.acts(typed)
                found = found or []
                # MusicBrainz's best guess must look like what was typed: a half-typed "The Cadilla" came back as
                # The Beatles. The first candidate whose name (or another name it goes by) is close wins.
                top = next((a for a in found if a["score"] >= 85 and _like(typed, a)), None)
                if canon and how == "known" and not (top and finder.name_key(top["name"]) == finder.name_key(typed)):
                    # "country" typed as an artist is the genre; a band really called that keeps its name
                    items.append({"typed": typed, "read_as": canon, "how": "a genre", "acts": GENRES[canon][1]})
                    acts += [(a, set()) for a in GENRES[canon][1]]
                    continue
                name = top["name"] if top else typed
                items.append({"typed": typed, "read_as": name, "how": "found" if top else "as typed"})
                acts.append((name, top["keys"] if top else {finder.name_key(typed)}))
            else:
                guess = None
                if not canon or _DECADE.search(typed) or _YEAR.search(typed):
                    guess = guess_genre(typed)
                if guess:
                    items.append({"typed": typed, "read_as": guess["genre"], "how": "guessed", "acts": guess["artists"]})
                    acts += [(a, set()) for a in guess["artists"]]
                elif canon:
                    items.append({"typed": typed, "read_as": canon, "how": how, "acts": GENRES[canon][1]})
                    acts += [(a, set()) for a in GENRES[canon][1]]
                else:
                    items.append({"typed": typed, "read_as": None, "how": "not understood"})
        seen: set[str] = set()
        for name, keys in acts[:MAX_ACTS * 2]:
            k = finder.name_key(name)
            if not k or k in seen:
                continue
            seen.add(k)
            try:
                got = _songs_by(db, name, set(keys) | {k}, per_act, keep_text)
                busy = 0 if busy < 3 else busy
            except _Busy:
                busy += 1
                if busy >= 3:
                    break
                continue
            for i in got:
                if i not in ids:
                    ids.append(i)
            for it in items:
                if name in (it.get("acts") or [it.get("read_as")]):
                    it["songs"] = it.get("songs", 0) + len(got)
    else:
        spans, hint = parse_years(text)
        if not spans:
            r = {**base, "status": "failed", "note": "No years in that. Try 1999, 1985-1990 or the 90s.",
                 "items": [{"typed": text, "read_as": None, "how": "not understood"}]}
            db.set_setting(key, r)
            return r
        skip = [f"{s['artist']} - {s['title']}" for s in (old or {}).get("songs") or []] if depth > 1 else []
        listed = songs_from_years(spans, hint, skip=skip)
        found = 0
        for s in listed:
            try:
                rid = _song(db, s["artist"], s["title"], keep_text)
            except _Busy:
                busy += 1
                if busy >= 3:
                    break
                continue
            if rid and rid not in ids:
                ids.append(rid)
                found += 1
        items.append({"typed": text, "read_as": years_label(spans) + (f" ({hint})" if hint else ""), "how": "years",
                      "songs": found, "asked": len(listed)})
        base["songs"] = ((old or {}).get("songs") or []) + listed if depth > 1 else listed

    understood = [it for it in items if it.get("read_as")]
    if ids:
        status, note = "ready", ("lrclib was busy for part of it; the rest is looked up again in a few minutes." if busy else "")
    elif not understood:
        status, note = "failed", "Could not make sense of that. Try different words."
    elif busy:
        status, note = "failed", "Understood, but lrclib is not answering right now; it tries again in a few minutes."
    else:
        status, note = "failed", "Understood, but no songs with words were found for it."
    r = {**base, "status": status, "note": note, "items": items, "ids": ids, "found": len(ids), "incomplete": bool(busy)}
    db.set_setting(key, r)
    LOG.info("pick: %s covers %s %r -> %d songs (%s)", station["id"], kind, text, len(ids),
             "; ".join(f"{it['typed']} = {it.get('read_as')}" for it in items))
    return r


def _cfg(key: str, default: Any = None) -> Any:
    try:
        import config
        return config.cfg(key)
    except Exception:
        return default


def ensure(db, station: dict[str, Any], force: bool = False) -> bool:
    """Start reading the channel's covers choice in the background when it has not been read for what is typed now,
    when the last try failed a while ago, or when its songs are nearly all sung. True when a reading was started."""
    if not active(station):
        return False
    r = db.setting(f"cover_pick:{station['id']}", None)
    r = r if isinstance(r, dict) and r.get("sig") == signature(station) else None
    depth, merge = 1, False
    if not force and r:
        age = time.time() - float(r.get("at") or 0)
        if r.get("status") == "failed":
            # lrclib not answering: a few minutes; not understood or nothing found: half an hour
            if age < (BUSY_RETRY_S if r.get("incomplete") else RETRY_FAILED_S):
                return False
        elif r.get("incomplete") and age >= BUSY_RETRY_S:
            depth, merge = int(r.get("depth") or 1), True       # finish what lrclib cut short
        elif len(unused(db, station)) >= REFILL_BELOW or int(r.get("depth") or 1) >= MAX_DEPTH or age < 3600:
            return False
        else:
            depth = int(r.get("depth") or 1) + 1                # nearly sung out: go deeper
    sid = station["id"]
    with _lock:
        if sid in _running:
            return False
        _running.add(sid)
    sig = signature(station)

    def run():
        try:
            resolve(db, station, depth, merge)
        except Exception:
            LOG.exception("pick: reading the covers choice of %s failed", sid)
        finally:
            with _lock:
                _running.discard(sid)
        # the text was changed while this one was being read: read the new one
        now = db.get("stations", sid)
        if now and active(now) and signature(now) != sig:
            ensure(db, now)
    threading.Thread(target=run, daemon=True, name=f"tf-pick-{sid}").start()
    return True
