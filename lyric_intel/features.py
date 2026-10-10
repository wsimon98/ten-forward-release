"""features.py — one set of lyrics in, a small bag of numbers out. Pure python, no model, no network.

Everything the repetition memory knows about a song comes from here: which content words it used, which
two-to-four word phrases, which rhyme tails it leaned on, how it opened, who is speaking, and which
image buckets it reached into. Runs in about a millisecond a song.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .lexicon import STOP, WORD_BUCKET

TAG = re.compile(r"^\s*\[(.+?)\]\s*$")
WORD = re.compile(r"[a-z']+")
VOWELS = "aeiouy"

VERSION = 1


def lyric_lines(lyrics: str) -> list[str]:
    """Sung lines only: no section tags, no blank lines."""
    out = []
    for raw in (lyrics or "").splitlines():
        line = raw.strip()
        if not line or TAG.match(line):
            continue
        out.append(line)
    return out


def sections(lyrics: str) -> list[tuple[str, list[str]]]:
    """[(tag, lines)] in order. Lines before the first tag land under ''."""
    out: list[tuple[str, list[str]]] = []
    tag, buf = "", []
    for raw in (lyrics or "").splitlines():
        m = TAG.match(raw.strip())
        if m:
            if buf or tag:
                out.append((tag, buf))
            tag, buf = m.group(1).strip(), []
        elif raw.strip():
            buf.append(raw.strip())
    if buf or tag:
        out.append((tag, buf))
    return out


def tokens(text: str) -> list[str]:
    return [w.strip("'") for w in WORD.findall((text or "").lower()) if w.strip("'")]


def content_words(text: str) -> list[str]:
    return [w for w in tokens(text) if len(w) > 2 and w not in STOP]


def rhyme_tail(word: str) -> str:
    """A rough rhyme key: from the last vowel cluster to the end, with a silent final e dropped first.
    night/light -> ight, twice/advice -> ic, love/above -> ov, rock/truck -> ock/uck (they do not rhyme)."""
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if len(w) < 2:
        return w
    if w.endswith("e") and len(w) > 3 and w[-2] not in VOWELS and any(c in VOWELS for c in w[:-1]):
        w = w[:-1]
    idx = None
    i = len(w) - 1
    while i >= 0:
        if w[i] in VOWELS:
            while i > 0 and w[i - 1] in VOWELS:
                i -= 1
            idx = i
            break
        i -= 1
    if idx is None:
        return w[-2:]
    tail = w[idx:]
    if len(tail) < 2 and idx > 0:
        tail = w[idx - 1:]
    return tail


OPENERS = [
    ("question", lambda first, toks: first.rstrip().endswith("?")),
    ("dialogue", lambda first, toks: first.lstrip()[:1] in ('"', "'", "“")),
    ("weather-time", lambda first, toks: bool(set(toks[:4]) & {"sun", "sky", "rain", "morning", "night", "light",
                                                               "moon", "dawn", "dusk", "clouds", "wind", "sunrise", "sunset"})),
    ("first-person", lambda first, toks: toks[:1] and toks[0] in ("i", "im", "ive", "ill", "id", "my", "me", "mine", "we", "our")),
    ("second-person", lambda first, toks: toks[:1] and toks[0] in ("you", "your", "youre", "youve")),
    ("third-person", lambda first, toks: toks[:1] and toks[0] in ("he", "she", "they", "his", "her", "hes", "shes", "theyre")),
    ("scene", lambda first, toks: toks[:1] and toks[0] in ("in", "on", "at", "under", "over", "through", "behind", "across",
                                                           "down", "up", "outside", "inside", "the", "a", "an", "there", "past", "beneath")),
]


def opening_kind(lines: list[str]) -> str:
    if not lines:
        return "none"
    first = lines[0]
    toks = tokens(first)
    for name, test in OPENERS:
        try:
            if test(first, toks):
                return name
        except Exception:
            continue
    return "action"


def perspective(text: str) -> str:
    t = " " + " ".join(tokens(text)) + " "
    first = len(re.findall(r" (i|im|ive|ill|id|me|my|mine|we|our|us) ", t))
    second = len(re.findall(r" (you|your|youre|youve|yours) ", t))
    third = len(re.findall(r" (he|she|they|him|her|his|hers|them|their|hes|shes|theyre) ", t))
    return max((("first", first), ("second", second), ("third", third)), key=lambda x: x[1])[0]


def ngrams(lines: list[str], lo: int = 2, hi: int = 4) -> set[str]:
    """Phrases a song used, counted once each. A phrase made only of functional words is skipped."""
    out: set[str] = set()
    for line in lines:
        toks = tokens(line)
        for k in range(lo, hi + 1):
            for i in range(len(toks) - k + 1):
                part = toks[i:i + k]
                if all(t in STOP or len(t) <= 2 for t in part):
                    continue
                out.add(" ".join(part))
    return out


def rhyme_pairs(lines: list[str]) -> list[str]:
    """Lines whose last words share a rhyme tail, as a sorted 'a/b' key. Both the next line (AABB) and the one
    after it (ABAB) count, because songs use the two schemes about equally."""
    ends = []
    for line in lines:
        toks = tokens(line)
        ends.append(toks[-1] if toks else "")
    out = []
    for i in range(len(ends)):
        for step in (1, 2):
            j = i + step
            if j >= len(ends):
                continue
            a, b = ends[i], ends[j]
            if not a or not b or a == b:
                continue
            ta, tb = rhyme_tail(a), rhyme_tail(b)
            if len(ta) >= 2 and ta == tb:
                out.append("/".join(sorted((a, b))))
    return out


def buckets_of(words: list[str]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for w in words:
        b = WORD_BUCKET.get(w)
        if b:
            c[b] += 1
    return dict(c)


def chorus_repeat(secs: list[tuple[str, list[str]]]) -> int:
    """How many times the chorus block comes back word for word."""
    blocks: Counter[str] = Counter()
    for tag, lines in secs:
        if re.match(r"(chorus|hook)", tag, re.I) and lines:
            blocks[" | ".join(l.lower() for l in lines)] += 1
    return max(blocks.values()) if blocks else 0


def extract(lyrics: str, topic: str = "") -> dict[str, Any]:
    """Everything the memory stores about one song."""
    lines = lyric_lines(lyrics)
    secs = sections(lyrics)
    words = content_words(" ".join(lines))
    uniq = sorted(set(words))
    line_lens = [len(tokens(l)) for l in lines] or [0]
    return {
        "v": VERSION,
        "words": uniq,
        "phrases": sorted(ngrams(lines)),
        "rhymes": sorted(set(rhyme_pairs(lines))),
        "ends": sorted({tokens(l)[-1] for l in lines if tokens(l)}),
        "buckets": buckets_of(words),
        "opening": opening_kind(lines),
        "opening_words": " ".join(tokens(lines[0])[:3]) if lines else "",
        "perspective": perspective(" ".join(lines)),
        "lines": len(lines),
        "sections": len([t for t, _ in secs if t]),
        "avg_line_words": round(sum(line_lens) / len(line_lens), 2),
        "ttr": round(len(uniq) / len(words), 3) if words else 0.0,
        "chorus_repeat": chorus_repeat(secs),
        "topic_words": sorted(set(content_words(topic))) if topic else [],
    }


def licence_words(*texts: str) -> set[str]:
    """Words the song is entitled to use because the brief asked for them (theme, idea, station mood)."""
    out: set[str] = set()
    for t in texts:
        out |= set(content_words(t or ""))
    return out
