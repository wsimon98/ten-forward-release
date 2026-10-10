"""covers.py — the channel sings somebody else's song, in its own sound.

The idea: the radio is private, nothing is published and most of it is swept in a few days, so now and
then a channel can just cover a real song. Two shapes:

  * a straight cover — the real words, cut into sections and trimmed to fit the channel's song length, sung
    in the channel's own style (a bedroom-pop song can come out as a porch waltz);
  * an interpolation — the real chorus is kept and the writer writes new verses around it. On a channel the
    verses are written under that channel's rules, the same ones its own songs get (radio.interpolation_writer);
    without one, under the plain lyric rules (plain_writer). Either way the chorus is put back word for word
    afterwards, and a verse line lifted from the original is sent back once.

Both are off unless the owner sets a covers share on a channel AND the outside lyric source is switched on.
A cover is tagged as one, carries who wrote it, is never written into a lyrics folder, and is kept out of the
repetition memory: a cover is not this radio's own habit.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from . import features, lrclib

LOG = logging.getLogger("tenforward.lyric_intel.covers")

SECONDS_PER_LINE = 4.6      # measured from this radio's own finished songs, rounded up so a cover is not clipped
MIN_LINES = 12
JUNK = re.compile(r"^\s*[\[(]?\s*(verse|chorus|hook|bridge|intro|outro|pre.?chorus|refrain|instrumental|solo)[^\]\)]*[\])]?\s*:?\s*$", re.I)
CREDIT = re.compile(r"(lyrics?\s*(by|:)|written by|produced by|\bfeat\.?\b|\bft\.?\b|copyright|all rights reserved|©)", re.I)
TAG = re.compile(r"^\s*\[(.+?)\]\s*$")
CHORUS_TAG = re.compile(r"^(?:final |last )?(?:chorus|hook|refrain)\b", re.I)
PLAIN_PERSPECTIVE = ("First person throughout. The verses are about the same people the chorus sings to or about, "
                     "with the same he, she or you.")


def clean_title(track: str, artist: str) -> str:
    """lrclib often stores the title as 'Artist - Title'; the channel only wants the title."""
    t = (track or "a song").strip()
    a = (artist or "").strip()
    if a and t.lower().startswith(a.lower()):
        t = t[len(a):].lstrip(" -–—:·|").strip() or t
    # "(Official Video)", "[Official Lyric Video]", "(Audio)", "(Remastered 2011)" - any bracket that STARTS with
    # one of these words, however it goes on. The old pattern only matched a bare "(Official)" or "(Video)", so a
    # song reached the radio as "Last Train Home (Official Video) (cover)". Looped: some titles carry two.
    junk = re.compile(r"\s*[\(\[]\s*(official|audio|video|lyrics?|visuali[sz]er|hd|4k|remaster(ed)?)\b[^\)\]]*[\)\]]\s*$", re.I)
    prev = None
    while prev != t:
        prev, t = t, junk.sub("", t).strip()
    return t or "a song"


def clean_plain(plain: str) -> list[list[str]]:
    """The real lyric text as stanzas of lines, with the source's own section words and credits dropped.
    Text that arrives as one unbroken block is cut into four-line stanzas so it can still be shaped."""
    stanzas: list[list[str]] = []
    buf: list[str] = []
    for raw in (plain or "").splitlines():
        line = raw.strip()
        if not line:
            if buf:
                stanzas.append(buf)
                buf = []
            continue
        if JUNK.match(line) or CREDIT.search(line):
            continue
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            buf.append(line[:1].upper() + line[1:])
    if buf:
        stanzas.append(buf)
    stanzas = [s for s in stanzas if s]
    if len(stanzas) == 1 and len(stanzas[0]) >= 8:
        flat = stanzas[0]
        stanzas = [flat[i:i + 4] for i in range(0, len(flat), 4)]
    return stanzas


def tag_sections(stanzas: list[list[str]]) -> list[tuple[str, list[str]]]:
    """Work out which stanza is the chorus by which one comes back, and label the rest in order."""
    keys = [" | ".join(l.lower() for l in s) for s in stanzas]
    counts: dict[str, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    chorus_key = None
    repeated = [(k, c) for k, c in counts.items() if c >= 2]
    if repeated:
        chorus_key = max(repeated, key=lambda x: (x[1], len(x[0])))[0]
    out: list[tuple[str, list[str]]] = []
    verse_n = 0
    chorus_seen = 0
    for k, s in zip(keys, stanzas):
        if chorus_key and k == chorus_key:
            chorus_seen += 1
            out.append(("Chorus", s))
        elif len(out) and out[-1][0] == "Chorus" and chorus_seen >= 2 and len(s) <= 4 and verse_n >= 2:
            out.append(("Bridge", s))
        else:
            verse_n += 1
            out.append((f"Verse {verse_n}", s))
    if chorus_key is None and len(out) >= 3:
        # nothing repeats: make the shortest middle stanza the chorus so the song still has a centre
        mid = min(range(1, len(out) - 1), key=lambda i: len(out[i][1]))
        out[mid] = ("Chorus", out[mid][1])
    return out


def arrange(secs: list[tuple[str, list[str]]], duration_s: float) -> list[tuple[str, list[str]]]:
    """Build a song this radio can actually sing out of whatever shape the source came in.

    Real lyric text is ragged: nine stanzas and no chorus, or one block with no breaks at all. So instead of
    trimming the source's own running order, the parts are taken out and a plain song is put together from
    them — verse, chorus, verse, chorus, and a bridge and a last chorus when the channel's song length has
    room. Verses come out numbered in order, which is what the section tags have to be for YuE2."""
    budget = max(MIN_LINES, int(float(duration_s or 180) / SECONDS_PER_LINE))
    chorus: list[str] = []
    verses: list[list[str]] = []
    bridge: list[str] = []
    seen: set[str] = set()
    for tag, lines in secs:
        if not lines:
            continue
        key = " | ".join(l.lower() for l in lines)
        if tag == "Chorus" and not chorus:
            chorus = lines[:6]
            seen.add(key)
            continue
        if key in seen:
            continue
        seen.add(key)
        if tag == "Bridge" and not bridge:
            bridge = lines[:4]
        else:
            verses.append(lines[:8])
    if not verses:
        return []
    if not chorus:
        chorus = verses.pop(len(verses) // 2)[:6] if len(verses) > 1 else verses[0][:4]
    if not bridge and len(verses) > 2:
        bridge = verses.pop(2)[:4]

    out: list[tuple[str, list[str]]] = [("Verse 1", verses[0]), ("Chorus", chorus)]
    if len(verses) > 1:
        out.append(("Verse 2", verses[1]))
        out.append(("Chorus", chorus))

    def total(rows):
        return sum(len(l) for _, l in rows)

    if bridge and total(out) + len(bridge) + len(chorus) <= budget:
        out.append(("Bridge", bridge))
        out.append(("Chorus", chorus))
    # over the channel's song length: drop the tail, then shorten the longest verse rather than lose the song
    while total(out) > budget and len(out) > 2:
        out.pop()
    while total(out) > budget:
        i = max(range(len(out)), key=lambda i: len(out[i][1]))
        if len(out[i][1]) <= 4:
            break
        out[i] = (out[i][0], out[i][1][:-2])
    return out


def render(secs: list[tuple[str, list[str]]]) -> str:
    out = []
    for tag, lines in secs:
        out.append(f"[{tag}]")
        out.extend(lines)
        out.append("")
    return "\n".join(out).strip()


def hook_of(secs: list[tuple[str, list[str]]]) -> list[str]:
    for tag, lines in secs:
        if tag == "Chorus":
            return lines[:6] if len(lines) <= 6 else lines[:4]
    return []


# --------------------------------------------------------------------------- interpolation: the chorus stays
def sections(text: str) -> list[tuple[str, list[str]]]:
    """Written lyrics as (tag, lines). Lines before the first tag count as Verse 1."""
    out: list[tuple[str, list[str]]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = TAG.match(line)
        if m:
            out.append((m.group(1).strip(), []))
            continue
        if not out:
            out.append(("Verse 1", []))
        out[-1][1].append(line)
    return out


def is_chorus(tag: str) -> bool:
    return bool(CHORUS_TAG.match(tag or ""))


def _pull_inline_chorus(secs: list[tuple[str, list[str]]], hook: list[str]) -> list[tuple[str, list[str]]]:
    """A rap writer often sings the hook at the bottom of a verse instead of giving it its own section, so the verse
    runs long and the hook is never tagged. Those lines come out of the verse and become the chorus after it."""
    theirs = {_plain(h) for h in hook}
    tag = next((t for t, _ in secs if is_chorus(t)), "Chorus")
    out: list[tuple[str, list[str]]] = []
    for i, (t, lines) in enumerate(secs):
        if is_chorus(t) or not t.lower().startswith(("verse", "bridge", "pre")):
            out.append((t, lines))
            continue
        own = [l for l in lines if _plain(l) not in theirs]
        if len(lines) - len(own) < 2:
            out.append((t, lines))
            continue
        out.append((t, own))
        if not (i + 1 < len(secs) and is_chorus(secs[i + 1][0])):
            out.append((tag, list(hook)))
    return out


def keep_chorus(text: str, hook: list[str]) -> str | None:
    """Every chorus in a draft becomes the real chorus, word for word. None when the draft has no chorus to put
    it in, or did not write at least two verses of its own (the point of the thing)."""
    secs = _pull_inline_chorus(sections(text), hook)
    if not any(is_chorus(t) for t, _ in secs):
        return None
    if sum(1 for t, l in secs if t.lower().startswith("verse") and len(l) >= 2) < 2:
        return None
    return render([(t, list(hook) if is_chorus(t) else l) for t, l in secs])


def without_chorus(text: str) -> str:
    """The draft with its choruses taken out, so a review only ever touches the new lines."""
    return render([(t, l) for t, l in sections(text) if not is_chorus(t)])


def with_chorus(layout: str, verses: str, hook: list[str]) -> str | None:
    """Put the real chorus back between reviewed verses. None when the review changed the shape."""
    lay = sections(layout)
    new = sections(verses)
    if len(new) != sum(1 for t, _ in lay if not is_chorus(t)):
        return None
    it = iter(new)
    return render([(t, list(hook)) if is_chorus(t) else (t, next(it)[1]) for t, _ in lay])


def _plain(line: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (line or "").lower().replace("'", "")).split())


def copied(text: str, original: list[str], hook: list[str]) -> list[str]:
    """New lines that are really lines of the original song (outside its chorus)."""
    theirs = {_plain(l) for l in original if len(_plain(l).split()) >= 3} - {_plain(h) for h in hook}
    return [l for t, ls in sections(text) if not is_chorus(t) for l in ls if _plain(l) in theirs]


def around_chorus(hook: list[str], original: list[str], style: str = "", brief: dict[str, Any] | None = None,
                  station_desc: str = "", lyric_block: str = "", tries: int = 2) -> str | None:
    """New verses and a bridge around a real chorus. The chorus is put back word for word whatever the writer did
    with it, and lines it lifted from the original are sent back once. None when no draft had verses of its own."""
    import llm
    for attempt in range(tries):
        try:
            draft = llm.write_around_chorus(hook, style=style, brief=brief, station_desc=station_desc, lyric_block=lyric_block)
        except Exception as e:
            LOG.warning("covers: the writer did not answer for an interpolation (%s)", e)
            return None
        text = keep_chorus(draft, hook)
        if not text:
            LOG.info("covers: interpolation draft %d had no chorus or no verses of its own; %s", attempt + 1,
                     "trying again" if attempt + 1 < tries else "giving up")
            continue
        lifted = copied(text, original, hook)
        if lifted:
            fix = ("These lines are copied from the original song. Replace each one with a new line of your own that fits "
                   "the story, and change nothing else:\n" + "\n".join(f"- {l}" for l in lifted))
            again = keep_chorus(llm.rewrite_lines(text, fix, style), hook)
            if again and len(copied(again, original, hook)) < len(lifted):
                text = again
            LOG.info("covers: interpolation lifted %d line(s) from the original; %d left", len(lifted), len(copied(text, original, hook)))
        if len(features.lyric_lines(text)) >= MIN_LINES:
            return text
    return None


def tidy_new_lines(text: str, hook: list[str], style: str = "") -> str:
    """With no channel there is no channel review, but the radio-wide worn-out words, the tired rhymes and made-up
    street names still go: one rewrite of the new lines only, kept when it is cleaner."""
    import llm
    from . import novelty

    def found(verses: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
        return (novelty.worn_found(verses), novelty.people_places(verses, True, True, ""), novelty.cliche_rhymes(verses))

    def dirt(f) -> int:
        return len(f[0]) + len(novelty.people_places_flat(f[1])) + len(f[2])

    verses = without_chorus(text)
    before = found(verses)
    if not dirt(before):
        return text
    fix = novelty.focused_instruction(before[0], before[1])
    if before[2]:
        fix = ((fix[:-1] + ". Also, ") if fix else "Change ONLY the lines that contain the things listed below; every other "
               "line stays exactly as it is. ") + "these rhymes are worn out, so end those lines differently: " + ", ".join(before[2]) + "."
    better = llm.rewrite_lines(verses, fix, style)
    if better.strip() == verses.strip() or dirt(found(better)) >= dirt(before):
        return text
    LOG.info("covers: rewrote the new lines that reached for %s", ", ".join(before[0] + novelty.people_places_flat(before[1]) + before[2]))
    return with_chorus(text, better, hook) or text


def plain_writer(style: str = ""):
    """Verses around a chorus with no channel behind them: the plain lyric rules and the default content rules."""
    def write(hook: list[str], original: list[str], track: str, artist: str) -> str | None:
        import themes
        brief = {"rules": themes.content_rules({}) + " Swear only if the chorus does.", "perspective": PLAIN_PERSPECTIVE}
        text = around_chorus(hook, original, style, brief=brief)
        return tidy_new_lines(text, hook, style) if text else None
    return write


# --------------------------------------------------------------------------- picking one
def unused(db, station_id: str, lane: str) -> list[dict[str, Any]]:
    """Cached songs with words kept that this channel has not sung yet."""
    rows = db.query("lrclib_cache", "plain IS NOT NULL AND (lane=? OR lane IS NULL)", (lane,), order="fetched DESC", limit=600)
    if not rows:
        return []
    sung = set()
    for s in db.query("songs", "cover_of IS NOT NULL", limit=500):
        c = s.get("cover_of") or {}
        if isinstance(c, dict) and c.get("id"):
            sung.add(int(c["id"]))
    return [r for r in rows if int(r.get("id") or 0) not in sung]


def fetch_one(db, query: str, lane: str = "search") -> dict[str, Any] | None:
    """Look a song up on demand and keep its words (used by the search box in Create)."""
    from . import corpus
    for rec in lrclib.search(q=query)[:10]:
        if corpus.store(db, rec, lane, keep_text=True):
            h = corpus.text_hash((rec.get("plainLyrics") or "").strip())
            rows = db.query("lrclib_cache", "text_hash=?", (h,), order="fetched DESC", limit=1)
            if rows:
                return rows[0]
    return None


def plan(db, station: dict[str, Any], style: str, duration_s: float, row: dict[str, Any] | None = None,
         interpolate: bool | None = None, rng: random.Random | None = None, writer=None,
         fallback: bool = True) -> dict[str, Any] | None:
    """Turn a cached real song into something the channel can sing. Returns None when there is nothing to sing.
    `writer(hook, original_lines, track, artist)` writes the verses of an interpolation (plain_writer when not
    given). With `fallback` off, an interpolation whose verses did not get written returns None instead of
    quietly becoming a straight cover: somebody asked for new verses."""
    from . import corpus
    rng = rng or random
    if row is None:
        pool = unused(db, station["id"], corpus.lane_of(station))
        row = rng.choice(pool) if pool else None
    if not row or not row.get("plain"):
        return None
    stanzas = clean_plain(row["plain"])
    if sum(len(s) for s in stanzas) < MIN_LINES:
        return None
    secs = tag_sections(stanzas)
    if interpolate is None:
        interpolate = rng.random() < 0.35
    artist = row.get("artist") or "unknown"
    track = clean_title(row.get("track") or "a song", artist)
    if interpolate:
        hook = hook_of(secs)
        if len(hook) >= 2:
            original = [l for s in stanzas for l in s]
            written = (writer or plain_writer(style))(hook, original, track, artist)
            if written:
                return interpolation_plan(row, written)
        if not fallback:
            return None
    secs = arrange(secs, duration_s)
    if not secs:
        return None
    lyrics = render(secs)
    if len(features.lyric_lines(lyrics)) < MIN_LINES:
        return None
    return {"title": f"{track} (cover)", "lyrics": lyrics, "topic": f"a cover of '{track}' by {artist}",
            "cover_of": {"id": row.get("id"), "track": track, "artist": artist, "kind": "cover"}}


def interpolation_plan(row: dict[str, Any], lyrics: str) -> dict[str, Any]:
    """An interpolation of this cached song with these words (written just now, or read and passed back by the owner)."""
    artist = row.get("artist") or "unknown"
    track = clean_title(row.get("track") or "a song", artist)
    return {"title": f"{track} (interpolation)", "lyrics": lyrics, "topic": f"built around the hook of '{track}' by {artist}",
            "cover_of": {"id": row.get("id"), "track": track, "artist": artist, "kind": "interpolation"}}


def rolled(station: dict[str, Any], chance: float, rng: random.Random | None = None) -> bool:
    rng = rng or random
    try:
        c = float(chance or 0)
    except (TypeError, ValueError):
        c = 0.0
    return c > 0 and rng.random() < min(1.0, c)


def recent(db, limit: int = 50) -> list[dict[str, Any]]:
    out = []
    for s in db.query("songs", "cover_of IS NOT NULL", limit=limit):
        c = s.get("cover_of") or {}
        out.append({"song_id": s["id"], "title": s.get("title"), "station_id": s.get("station_id"),
                    "created": s.get("created"), **(c if isinstance(c, dict) else {})})
    return out


def touch(db) -> float:
    return time.time()
