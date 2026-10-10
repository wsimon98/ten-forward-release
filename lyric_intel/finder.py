"""finder.py — what somebody typed into the cover search, turned into real songs.

lrclib's search wants every typed word somewhere in a song's title, artist or album, and it files an act under
the name on the record. So a stage name typed as the person's name finds next to nothing, and "an artist plus
a word from the title" only works when the artist is typed exactly the way lrclib spells it.

So the words are looked up as acts on MusicBrainz first (free, no key, one request, cached for a day), and
then lrclib is asked the right questions:

  - all the words are an act            -> that act's songs, under each name it goes by
  - an act at the front or at the back   -> that act's songs with the other words in the title
  - and always the words as typed        -> titles, albums, anything lrclib matches on its own

The same song on ten albums is shown once, and the closest matches come first. Only someone typing a search
reaches this; nothing in song planning does.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import lrclib

LOG = logging.getLogger("tenforward.lyric_intel.finder")

MB_URL = "https://musicbrainz.org/ws/2/artist/"
MB_TIMEOUT = 8
MB_GAP_S = 1.1         # MusicBrainz allows one request a second
MB_KEEP_S = 24 * 3600
HITS_KEEP_S = 15 * 60
MAX_WORDS = 8
MAX_CALLS = 6          # lrclib questions per search, the words as typed included
READINGS = 3           # ways of splitting the words into an act and a title that get asked about
NAMES_PER_READING = 2  # names an act goes by that get asked about
SHOW = 30
MIN_WORDS_CHARS = 200  # shorter than this is a stub, not the words of a song

_mb_lock = threading.Lock()
_mb_last = 0.0
_mb_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_hits_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_ASKING = re.compile(
    r"^(?:please\s+)?(?:(?:pick|play|find|get|give me|sing|do|cover|search for|look up|show me)\s+)?(?:(?:me|us)\s+)?"
    r"(?:something|anything|a song|songs|some songs|a track|tracks|stuff|music|the songs)\s+(?:by|from)\s+", re.I)
_LUCENE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_EXTRA = re.compile(r"\s*[\(\[].*?[\)\]]")
_FEAT = re.compile(r"\s+(?:ft\.?|feat\.?|featuring)\s+.*$", re.I)
_SPLIT_ACTS = re.compile(r"\s*(?:,|;|&|/|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b)\s*", re.I)
_TRACK_NO = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]\s*|0\d\s+)")
_PARTS = re.compile(r"\s*[-–—|]\s+|\s+[-–—|]\s*|\s*:\s+|(?<=[a-z0-9])-(?=[A-Za-z])", re.I)
_UNCLOSED = re.compile(r"\s*[\(\[][^\)\]]*$")
_UPLOAD = re.compile(r"\b(?:official|music video|lyric video|visuali[sz]er|audio|hd|hq|remaster(?:ed)?)\b", re.I)


# --------------------------------------------------------------------------- matching
def fold(text: str) -> str:
    """Lower case, no accents, no punctuation, & as and: 'AC/DC' -> 'ac dc', "Guns N' Roses" -> 'guns n roses'."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def name_key(text: str) -> str:
    """An act's name with the spaces and a leading 'the' gone, so '2 pac' meets '2Pac' and 'beatles' meets 'The Beatles'."""
    f = fold(text)
    if f.startswith("the "):
        f = f[4:]
    return f.replace(" ", "")


def clean(query: str) -> str:
    """What was typed, without 'pick something by' in front of it, at most MAX_WORDS words."""
    q = " ".join((query or "").split())
    q = _ASKING.sub("", q).strip()
    return " ".join(q.split()[:MAX_WORDS])


def _by(rec: dict[str, Any], names: list[str]) -> bool:
    who = fold(rec.get("artistName") or "").replace(" ", "")
    return any(name_key(n) and name_key(n) in who for n in names)


def _first_act(rec: dict[str, Any]) -> str:
    return _SPLIT_ACTS.split(rec.get("artistName") or "", maxsplit=1)[0]


def tidy_title(rec: dict[str, Any]) -> str:
    """The song's own title, without what uploads pile onto it: '3 - 2Pac - Temptations (Official Music Video)'
    -> 'Temptations', 'Metallica: One' -> 'One'."""
    raw = rec.get("trackName") or rec.get("name") or ""
    t = _UNCLOSED.sub("", _EXTRA.sub("", raw.replace("_", " ")))
    t = _TRACK_NO.sub("", t)
    who = name_key(_first_act(rec))
    parts = [p for p in _PARTS.split(t) if p.strip()]
    if who and len(parts) > 1:
        kept = [p for p in parts if who not in name_key(p)]
        if kept and len(kept) < len(parts):
            t = " - ".join(kept)
    t = _FEAT.sub("", t)
    return " ".join(t.split()).strip(" -:|") or raw.strip()


def _messy(rec: dict[str, Any]) -> bool:
    """An upload's title rather than the song's: a track number, the act's own name, 'official video'."""
    raw = rec.get("trackName") or rec.get("name") or ""
    who = name_key(_first_act(rec))
    return bool(_TRACK_NO.match(raw) or _UPLOAD.search(raw) or "_" in raw
                or (who and who in name_key(_EXTRA.sub("", raw))))


def _singable(rec: dict[str, Any]) -> bool:
    return not rec.get("instrumental") and len((rec.get("plainLyrics") or "").strip()) >= MIN_WORDS_CHARS


# --------------------------------------------------------------------------- MusicBrainz
def acts(text: str) -> list[dict[str, Any]] | None:
    """MusicBrainz's guesses at acts named in `text`: [{"name", "keys", "score"}], keys being every name the act
    goes by. None when MusicBrainz did not answer."""
    global _mb_last
    key = fold(text)
    hit = _mb_cache.get(key)
    if hit and time.time() - hit[0] < MB_KEEP_S:
        return hit[1]
    q = " ".join(_LUCENE.sub(" ", text).split())
    if not q:
        return []
    url = MB_URL + "?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 10})
    req = urllib.request.Request(url, headers={"User-Agent": lrclib.user_agent(),
                                               "Accept": "application/json"})
    data = None
    with _mb_lock:
        for attempt in (1, 2):      # a busy MusicBrainz says 503; one more try after a short rest
            wait = (MB_GAP_S if attempt == 1 else 2.0) - (time.time() - _mb_last)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=MB_TIMEOUT) as r:
                    data = json.loads(r.read())
                break
            except Exception as e:
                LOG.info("finder: MusicBrainz did not answer (%s)", e)
            finally:
                _mb_last = time.time()
    if data is None:
        return None
    out = []
    for a in data.get("artists") or []:
        names = [a.get("name") or ""] + [x.get("name") or "" for x in a.get("aliases") or []]
        keys = {name_key(n) for n in names if name_key(n)}
        if a.get("name") and keys:
            out.append({"name": a["name"], "keys": keys, "score": int(a.get("score") or 0)})
    _mb_cache[key] = (time.time(), out)
    return out


def readings(words: list[str], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ways to read the words as an act plus words from a title, longest act first:
    [{"names": [...], "rest": "girlfriend", "span": 2, "side": "front"}]. "rest" is empty when every word is the act."""
    n = len(words)
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for a in found:
        best = None
        for span in range(n, 0, -1):
            sides = [("all", words, [])] if span == n else [("front", words[:span], words[span:]),
                                                              ("back", words[n - span:], words[:n - span])]
            for side, part, rest in sides:
                if name_key(" ".join(part)) in a["keys"]:
                    best = (side, span, rest)
                    break
            if best:
                break
        if not best:
            continue
        side, span, rest = best
        slot = out.setdefault((side, span), {"names": [], "rest": " ".join(rest), "span": span, "side": side, "score": 0})
        if all(name_key(a["name"]) != name_key(x) for x in slot["names"]):
            slot["names"].append(a["name"])
        slot["score"] = max(slot["score"], a["score"])
    return sorted(out.values(), key=lambda r: (-r["span"], -r["score"]))


# --------------------------------------------------------------------------- the search
def find(query: str) -> list[dict[str, Any]] | None:
    """Songs for what somebody typed: [{"id", "track", "artist", "album", "duration"}], closest first.
    None when lrclib did not answer at all."""
    text = clean(query)
    words = fold(text).split()
    if not words:
        return []
    cached = _hits_cache.get(" ".join(words))
    if cached and time.time() - cached[0] < HITS_KEEP_S:
        return cached[1]

    asked: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []   # (kind, params, reading)
    answers: list[tuple[str, dict[str, Any] | None, list[dict[str, Any]]]] = []  # (kind, reading, songs)
    failed = 0

    def ask(params: dict[str, Any]) -> list[dict[str, Any]] | None:
        try:
            return lrclib.search(asked=True, **params)
        except lrclib.Unreachable:
            return None

    # the words as typed and the act lookup go out together; they are different services
    with ThreadPoolExecutor(max_workers=2) as pool:
        typed_f = pool.submit(ask, {"q": text})
        acts_f = pool.submit(acts, text)
        typed, found = typed_f.result(), acts_f.result()
    asked.append(("typed", {"q": text}, None))
    if typed is None:
        failed += 1
    else:
        answers.append(("typed", None, typed))

    # a search that is plainly a title does not need splitting into an act and a title
    phrase = " ".join(words)
    titled = sum(1 for r in typed or [] if _singable(r) and phrase in fold(tidy_title(r)))
    used = [rd for rd in readings(words, found or [])[:READINGS] if not (rd["rest"] and titled >= 3)]
    seen = {json.dumps({"q": phrase})}
    for rd in used:
        for name in rd["names"][:NAMES_PER_READING]:
            params = {"track_name": rd["rest"], "artist_name": name} if rd["rest"] else {"q": name}
            sig = json.dumps({k: fold(v) for k, v in params.items()}, sort_keys=True)
            if sig in seen or len(asked) >= MAX_CALLS:
                continue
            seen.add(sig)
            asked.append(("act", params, rd))
            got = ask(params)
            if got is None:
                failed += 1
            else:
                answers.append(("act", rd, got))

    if failed == len(asked):
        return None

    def score(rec: dict[str, Any], kind: str, title: str) -> float:
        s = (4.0 if phrase in title else 1.0) if kind == "typed" else 0.5
        for i, rd in enumerate(used):
            if not _by(rec, rd["names"]):
                continue
            if not rd["rest"]:
                s = max(s, 5.0)                      # by the act that is everything typed
            elif all(w in title for w in rd["rest"].split()):
                # the likelier reading first: 'beatles yesterday' is Yesterday by the Beatles before a song
                # called The Beatles by a band called Yesterday
                s = max(s, 3.0 + (0.5 if rd["rest"] in title else 0.0) + 0.1 * (len(used) - i) / len(used))
        return s - (0.3 if _messy(rec) else 0.0)

    def same_song(rec: dict[str, Any], title: str) -> tuple[str, str]:
        # one act under two names (Tupac Shakur, 2Pac) is still one act
        for i, rd in enumerate(used):
            if _by(rec, rd["names"]):
                return title, f"reading {i}"
        return title, name_key(_first_act(rec))

    best: dict[tuple[str, str], tuple[float, int, dict[str, Any]]] = {}
    order = 0
    for kind, rd, recs in answers:
        for rec in recs:
            if not _singable(rec):
                continue
            title = fold(tidy_title(rec))
            s = score(rec, kind, title)
            k = same_song(rec, title)
            order += 1
            if k not in best or s > best[k][0]:
                best[k] = (s, best[k][1] if k in best else order, rec)
    ranked = sorted(best.values(), key=lambda x: (-x[0], x[1]))[:SHOW]
    out = [{"id": r.get("id"), "track": tidy_title(r), "artist": r.get("artistName"),
            "album": r.get("albumName"), "duration": r.get("duration")} for _, _, r in ranked]
    if not failed:
        _hits_cache[phrase] = (time.time(), out)
    LOG.info("finder: %r -> %d songs from %d questions (%d unanswered)", text, len(out), len(asked), failed)
    return out
