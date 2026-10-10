"""
sheet.py — lead sheets for YuE2 (1.6).

YuE2 sings from a lead sheet: ABC text in the dialect SheetSage2 writes and the model itself plans in (two voices,
`V: Vocal` and `V: Ins`; chords as "Am" in quotes; section labels as `% verse` comment lines; `Z` for whole-bar
rests). This module knows that dialect and nothing else about music:

  * parse()        read a sheet: its key, tempo, bars, the sections with their times, and the melody's phrases
  * budget()       how many lines each section wants and how many syllables each line should carry (notes per phrase)
  * align_words()  put the words Whisper heard (with times) under the sections of the sheet they were sung in
  * check_fit()    does a set of lyrics sit on the melody? per line: syllables it has against what the melody wants
  * from_midi() / from_musicxml() / from_pdf()   a sheet from a file, through SheetSage2's own assembler so the
                   dialect is exactly right; MusicXML brings its chord symbols and the words under the notes with it

Nothing here touches the graphics card or the database; the engine runs SheetSage2 and the server keeps the rows.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tenforward.sheet")
TF_ROOT = Path(__file__).resolve().parent
WANGP_ROOT = Path(os.environ.get("TF_WANGP_ROOT") or (TF_ROOT / "wan2gp"))

# --------------------------------------------------------------------------- section names
# SheetSage2's structure labels -> the section tag YuE2 reads in the lyrics. Anything without singing gets a tag
# and no words, so the sheet and the words stay in step.
TAG_FOR = {
    "intro": "Intro", "outro": "Outro", "verse": "Verse", "chorus": "Chorus", "bridge": "Bridge", "pre-chorus": "Pre-Chorus",
    "post-chorus": "Chorus", "interlude": "Interlude", "fade-out": "Outro", "loop": "Chorus", "rap": "Verse", "preshot": "Verse",
    "irregular": "Verse", "instrumental": "Interlude", "intro and verse": "Verse", "pre-chorus and chorus": "Chorus",
    "verse and pre-chorus": "Verse", "solo": "Interlude", "theme": "Verse", "development": "Verse", "variation": "Verse",
    "pre-outro": "Outro", "silence": "",
}
SUNG = {"Verse", "Chorus", "Bridge", "Pre-Chorus"}

_MUSIC = re.compile(r'"[^"]*"|\[[^\]]*\]|![^!]*!|\+[^+]*\+|Z\d*|z\d*(?:/\d*)?|[_^=]*[A-Ga-g][,\']*\d*(?:/\d*)?|-|\||\(|\)|[<>]+|\S')
_NOTE = re.compile(r"^([_^=]*)([A-Ga-g])([,']*)(\d+)?(/(\d*))?$")
_REST = re.compile(r"^z(\d+)?(/(\d*))?$")
_TAG = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _dur(num: str | None, slash: str | None, den: str | None) -> float:
    d = float(num) if num else 1.0
    if slash is not None:
        d /= float(den) if den else 2.0
    return d


class Sheet(dict):
    """A parsed sheet. Plain dict so it stores as JSON; the keys are documented in parse()."""


def parse(abc: str) -> Sheet:
    """Read a YuE2/SheetSage2 lead sheet.

    Returns {key, meter, unit, bpm, bar_s, bars, duration_s, has_chords, melody_voice, sections[], phrases[]}
    where a section is {label, tag, bar_start, bar_end, bars, start_s, end_s, lines, syllables[]} and lines /
    syllables come from the melody voice's phrases inside it (a run of notes between rests of a beat or more;
    tied notes count once). Raises ValueError on something that is not a sheet."""
    text = (abc or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("empty sheet")
    lines = text.split("\n")
    meter = (4, 4)
    unit = 16
    bpm = 120.0
    q_unit = (1, 4)
    key = ""
    voices_declared: list[str] = []
    i = 0
    # header: everything up to and including the first K: line
    body_start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("M:"):
            m = re.match(r"M:\s*(\d+)\s*/\s*(\d+)", s)
            if m:
                meter = (int(m.group(1)), int(m.group(2)))
        elif s.startswith("L:"):
            m = re.match(r"L:\s*1\s*/\s*(\d+)", s)
            if m:
                unit = int(m.group(1))
        elif s.startswith("Q:"):
            m = re.match(r"Q:\s*(?:(\d+)\s*/\s*(\d+)\s*=\s*)?(\d+(?:\.\d+)?)", s)
            if m:
                bpm = float(m.group(3))
                if m.group(1):
                    q_unit = (int(m.group(1)), int(m.group(2)))
        elif s.startswith("V:"):
            m = re.match(r"V:\s*(\w+)", s)
            if m:
                voices_declared.append(m.group(1))
        elif s.startswith("K:"):
            key = s[2:].strip()
            body_start = i + 1
            break
    if body_start is None:
        raise ValueError("no K: line; this is not an ABC sheet")

    def bar_seconds(m: tuple[int, int]) -> float:
        return (m[0] / m[1]) / (q_unit[0] / q_unit[1]) * (60.0 / bpm)

    def beat_units(m: tuple[int, int]) -> float:
        return unit / m[1]     # L-units in one beat of the meter

    # body: groups of (labels, {voice: bars}); a bar is a list of events. SheetSage2 and the model both write a
    # group as: optional "% label" lines, "V: Vocal" + its bars, "V: Ins" + its bars; a voice named a second time
    # opens the next group.
    groups: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    voice = "Vocal"
    labels: list[str] = []
    cur_meter = meter
    has_chords = False
    for line in lines[body_start:]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("%"):
            lab = s[1:].strip().lower()
            if lab and lab != "ss2":
                labels.append(lab)
            continue
        if s.startswith("V:"):
            m = re.match(r"V:\s*(\w+)", s)
            voice = m.group(1) if m else "Vocal"
            if cur is None or voice in cur["voices"] or labels:
                cur = {"labels": labels, "voices": {}, "meter": cur_meter}
                groups.append(cur)
                labels = []
            cur["voices"].setdefault(voice, [])
            continue
        if s.startswith("M:"):
            m = re.match(r"M:\s*(\d+)\s*/\s*(\d+)", s)
            if m:
                cur_meter = (int(m.group(1)), int(m.group(2)))
                if cur is not None:
                    cur["meter"] = cur_meter
            continue
        if s.startswith("K:") or s.startswith("L:") or s.startswith("Q:") or s.startswith("w:") or s.startswith("W:"):
            continue
        if cur is None:
            cur = {"labels": labels, "voices": {}, "meter": cur_meter}
            groups.append(cur)
            labels = []
        bars = cur["voices"].setdefault(voice, [])
        bar: list[dict[str, Any]] = []
        chord = None
        just_z = False
        for tok in _MUSIC.findall(s):
            if tok == "|":
                if just_z:
                    just_z = False          # "Z4|": the bar line closes the rest bars already counted
                    continue
                bars.append(bar)
                bar = []
                continue
            just_z = False
            if tok.startswith('"'):
                chord = tok.strip('"')
                if chord and chord not in ("N", "X", "?"):
                    has_chords = True
                continue
            if tok[0] == "Z":
                n = int(tok[1:]) if len(tok) > 1 else 1
                for _ in range(n):
                    bars.append(bar)
                    bar = []
                just_z = True
                continue
            if tok == "-":
                if bar and bar[-1]["kind"] == "note":
                    bar[-1]["tie"] = True
                elif bars and bars[-1] and bars[-1][-1]["kind"] == "note":
                    bars[-1][-1]["tie"] = True
                continue
            m = _REST.match(tok)
            if m:
                bar.append({"kind": "rest", "dur": _dur(m.group(1), m.group(2), m.group(3))})
                continue
            m = _NOTE.match(tok)
            if m:
                bar.append({"kind": "note", "pitch": m.group(1) + m.group(2) + m.group(3), "dur": _dur(m.group(4), m.group(5), m.group(6)), "chord": chord})
                chord = None
                continue
            # slurs, decorations, broken rhythm, stray text: ignored
        if bar:
            bars.append(bar)
    if not groups:
        raise ValueError("the sheet has no music in it")

    # bar count per group = the longest voice in it (the voices are written bar for bar)
    melody_voice = "Vocal"
    note_count = {"Vocal": 0, "Ins": 0}
    for g in groups:
        g["bars"] = max((len(b) for b in g["voices"].values()), default=0)
        for v, bars_ in g["voices"].items():
            note_count[v] = note_count.get(v, 0) + sum(1 for b in bars_ for e in b if e["kind"] == "note")
    if note_count.get("Vocal", 0) == 0 and note_count.get("Ins", 0) > 0:
        melody_voice = "Ins"

    # sections: runs of groups under one label
    sections: list[dict[str, Any]] = []
    bar_index = 0
    t = 0.0
    label = "verse"
    for g in groups:
        if g["labels"]:
            label = g["labels"][-1]
        secs = bar_seconds(g["meter"])
        n = g["bars"]
        if sections and sections[-1]["label"] == label and not g["labels"]:
            sec = sections[-1]
        else:
            sec = {"label": label, "tag": TAG_FOR.get(label, "Verse"), "bar_start": bar_index, "bar_end": bar_index, "bars": 0,
                   "start_s": round(t, 3), "end_s": round(t, 3), "lines": 0, "syllables": [], "notes": 0, "_events": []}
            sections.append(sec)
        sec["bar_end"] = bar_index + n
        sec["bars"] += n
        sec["end_s"] = round(t + n * secs, 3)
        # the melody voice's events with absolute times, for the phrases
        bars_ = g["voices"].get(melody_voice, [])
        bu = beat_units(g["meter"])
        unit_s = secs / (g["meter"][0] * bu) if g["meter"][0] * bu else 0.0
        for bi in range(n):
            tb = t + bi * secs
            pos = 0.0
            for e in (bars_[bi] if bi < len(bars_) else []):
                ev = dict(e, start_s=tb + pos * unit_s, beats=e["dur"] / bu if bu else 0.0)
                sec["_events"].append(ev)
                pos += e["dur"]
        bar_index += n
        t += n * secs

    # phrases per section: notes between rests of a beat or more; ties merge; tiny phrases join their neighbour
    phrases_all: list[dict[str, Any]] = []
    for sec in sections:
        phrases: list[dict[str, Any]] = []
        run: dict[str, Any] | None = None
        tie_open = False
        for ev in sec["_events"]:
            if ev["kind"] == "rest":
                if ev["beats"] >= 1.0 and run:
                    phrases.append(run)
                    run = None
                tie_open = False
                continue
            if tie_open:
                tie_open = bool(ev.get("tie"))
                if run:
                    run["end_s"] = ev["start_s"] + ev["dur"] * 0
                continue
            if run is None:
                run = {"start_s": ev["start_s"], "notes": 0, "end_s": ev["start_s"]}
            run["notes"] += 1
            run["end_s"] = ev["start_s"]
            tie_open = bool(ev.get("tie"))
        if run:
            phrases.append(run)
        merged: list[dict[str, Any]] = []
        for p in phrases:
            if merged and (p["notes"] < 3 or merged[-1]["notes"] < 3):
                merged[-1]["notes"] += p["notes"]
                merged[-1]["end_s"] = p["end_s"]
            else:
                merged.append(dict(p))
        out: list[dict[str, Any]] = []
        for p in merged:
            while p["notes"] > 14:          # a run too long for one sung line: split it in two
                half = p["notes"] // 2
                out.append({"start_s": p["start_s"], "end_s": p["end_s"], "notes": half})
                p = dict(p, notes=p["notes"] - half)
            out.append(p)
        sec["lines"] = len(out)
        sec["syllables"] = [p["notes"] for p in out]
        sec["notes"] = sum(sec["syllables"])
        sec["phrases"] = [{"start_s": round(p["start_s"], 2), "end_s": round(p["end_s"], 2), "notes": p["notes"]} for p in out]
        for p in sec["phrases"]:
            phrases_all.append(dict(p, section=sec["tag"]))
        del sec["_events"]
    total_bars = bar_index
    return Sheet(key=key, meter=f"{meter[0]}/{meter[1]}", unit=unit, bpm=bpm, bar_s=round(bar_seconds(meter), 4), bars=total_bars,
                 duration_s=round(t, 2), has_chords=has_chords, melody_voice=melody_voice, sections=sections, phrases=phrases_all,
                 voices=voices_declared or ["Vocal", "Ins"])


# --------------------------------------------------------------------------- the budget
def budget(sheet: Sheet) -> list[dict[str, Any]]:
    """One entry per section: {tag, lines, syllables[], bars, seconds, sung}. Sections with no notes in the melody
    voice get lines 0 and a bare tag (an intro, a solo)."""
    out = []
    for s in sheet["sections"]:
        if not s["tag"]:
            continue
        out.append({"tag": s["tag"], "lines": s["lines"], "syllables": list(s["syllables"]), "bars": s["bars"],
                    "seconds": round(s["end_s"] - s["start_s"], 1), "sung": s["lines"] > 0 and s["tag"] in SUNG | {"Interlude", "Intro", "Outro"}})
    return out


def budget_text(sheet: Sheet) -> str:
    """The budget as the writer reads it."""
    rows = []
    for b in budget(sheet):
        if b["lines"]:
            rows.append(f"[{b['tag']}] {b['lines']} line{'s' if b['lines'] != 1 else ''}: " + " / ".join(str(n) for n in b["syllables"]) + " syllables")
        else:
            rows.append(f"[{b['tag']}] no singing ({b['bars']} bars of music)")
    return "\n".join(rows)


def budget_from_words(words: str) -> list[dict[str, Any]]:
    """The budget the original words set: per sung section, its lines and the syllables each carries. This is what
    YuE2 itself asks for in a cover (the same syllables line by line as the original), so when an arrangement has
    the words it was sung with, they outrank the note count of the sheet."""
    out = []
    for tag, lines in _lyric_sections(words):
        if not lines:
            out.append({"tag": tag, "lines": 0, "syllables": [], "sung": False})
            continue
        out.append({"tag": tag, "lines": len(lines), "syllables": [line_syllables(l) for l in lines], "sung": True})
    return out


def budget_text_of(rows: list[dict[str, Any]]) -> str:
    out = []
    for b in rows:
        if b["lines"]:
            out.append(f"[{b['tag']}] {b['lines']} line{'s' if b['lines'] != 1 else ''}: " + " / ".join(str(n) for n in b["syllables"]) + " syllables")
        else:
            out.append(f"[{b['tag']}] no singing")
    return "\n".join(out)


def skeleton(sheet: Sheet) -> str:
    """Just the section tags, in order (what an instrumental sings)."""
    return "\n".join(f"[{b['tag']}]" for b in budget(sheet))


# --------------------------------------------------------------------------- syllables
_VOWELS = re.compile(r"[aeiouy]+")


def syllables(word: str) -> int:
    """Syllables in one English word, by rule of thumb (vowel groups, silent e, -ed, -es). Within one of the real
    count nearly always, which is what the fit check tolerates."""
    w = re.sub(r"[^a-z']", "", (word or "").lower())
    w = re.sub(r"'s$|'$|'", "", w)
    if not w:
        digits = re.findall(r"\d", word or "")
        return len(digits)       # "3" sung as "three"; "2023" is four digits said one by one, near enough
    n = len(_VOWELS.findall(w))
    if n == 0:
        return 1
    if w.endswith("e") and not w.endswith(("le", "ee", "ye", "oe", "ie", "ue")) and n > 1:
        n -= 1                                   # make, home, there
    elif w.endswith("le") and len(w) > 2 and w[-3] not in "aeiouy" and n >= 1:
        pass                                     # table, little: the le is a syllable and was counted
    if w.endswith("ed") and n > 1 and w[-3] not in "td" and not w.endswith(("eed", "ied")):
        n -= 1                                   # walked, called (wanted and needed keep theirs)
    if w.endswith("es") and n > 1 and w[-3] not in "sxzcg" and not w.endswith(("ies", "oes", "ees")):
        n -= 1                                   # makes, homes (boxes, buses keep theirs)
    if re.search(r"[aeiou]y[aeiou]", w):
        n += 0
    if w.endswith(("ia", "io", "ium", "eo")) or re.search(r"[^aeiouy]i[ao]", w):
        n += 1                                   # radio, Maria, video
    return max(1, n)


def line_syllables(line: str) -> int:
    return sum(syllables(w) for w in re.findall(r"[A-Za-z0-9']+", line or ""))


# --------------------------------------------------------------------------- words under the sheet
def _lyric_sections(lyrics: str) -> list[tuple[str, list[str]]]:
    """[(tag, [lines])] from tagged lyrics; untagged lines at the top become a Verse."""
    out: list[tuple[str, list[str]]] = []
    for raw in (lyrics or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _TAG.match(line)
        if m:
            out.append((m.group(1).strip(), []))
            continue
        if not out:
            out.append(("Verse", []))
        out[-1][1].append(line)
    return out


def _family(tag: str) -> str:
    t = re.sub(r"\s*\d+$", "", (tag or "").strip().lower())
    return {"hook": "chorus", "refrain": "chorus", "final chorus": "chorus", "last chorus": "chorus"}.get(t, t)


def align_words(segments: list[dict[str, Any]], sheet: Sheet) -> str:
    """Whisper's segments ({start, end, text}) placed under the sheet's sections by when they were sung. Each
    segment is one line. Sections nobody sang in keep their bare tag."""
    secs = [s for s in sheet["sections"] if s["tag"]]
    buckets: list[list[str]] = [[] for _ in secs]
    for seg in segments or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        mid = (float(seg.get("start") or 0) + float(seg.get("end") or seg.get("start") or 0)) / 2
        where = None
        for i, s in enumerate(secs):
            if s["start_s"] <= mid < s["end_s"]:
                where = i
                break
        if where is None:
            where = len(secs) - 1 if secs and mid >= secs[-1]["end_s"] else 0
        if secs:
            buckets[where].append(text)
    out = []
    for s, lines in zip(secs, buckets):
        out.append(f"[{s['tag']}]")
        out.extend(lines)
        out.append("")
    return "\n".join(out).strip() + "\n"


def check_fit(lyrics: str, sheet: Sheet, tolerance: int = 1) -> dict[str, Any]:
    """Do these words sit on this melody? Pairs the lyrics' sung sections with the sheet's sung sections in order
    and compares syllables line by line. Returns {ok, off, lines[{section, n, text, has, want, diff}], missing,
    extra, instruction} where instruction is what to tell the rewriter."""
    rows = sheet if isinstance(sheet, list) else budget(sheet)     # a budget (from the original words) or a sheet
    want_secs = [b for b in rows if b["lines"] > 0]
    have_secs = _lyric_sections(lyrics)
    have_secs = [(t, l) for t, l in have_secs if l]
    rows: list[dict[str, Any]] = []
    missing = 0
    extra = 0
    pairs = list(zip(want_secs, have_secs))
    if len(have_secs) > len(want_secs):
        extra += sum(len(l) for _, l in have_secs[len(want_secs):])
    for want, (tag, lines) in pairs:
        syl = want["syllables"]
        for n, line in enumerate(lines):
            if n >= len(syl):
                extra += 1
                rows.append({"section": tag, "n": n + 1, "text": line, "has": line_syllables(line), "want": None, "diff": None})
                continue
            has = line_syllables(line)
            rows.append({"section": tag, "n": n + 1, "text": line, "has": has, "want": syl[n], "diff": has - syl[n]})
        if len(lines) < len(syl):
            missing += len(syl) - len(lines)
    for want in want_secs[len(pairs):]:
        missing += want["lines"]
    off = [r for r in rows if r["diff"] is not None and abs(r["diff"]) > tolerance]
    fixes = []
    for r in off[:12]:
        fixes.append(f"[{r['section']}] line {r['n']} \"{r['text']}\" has {r['has']} syllables and needs {r['want']}")
    instruction = ""
    if fixes:
        instruction = ("Change ONLY the lines listed below so each one has the number of syllables it needs (count them), keeping "
                       "what the line says; every other line stays exactly as it is, word for word:\n" + "\n".join(fixes))
    return {"ok": not off and not missing and not extra, "off": len(off), "lines": rows, "missing": missing, "extra": extra,
            "instruction": instruction}


# --------------------------------------------------------------------------- sheet text helpers
def strip_chords(abc: str) -> str:
    """The same sheet without chord symbols (what 'melody only' planning needs)."""
    return re.sub(r'"[^"]*"', "", abc or "")


def repeat_body(abc: str, times: int) -> str:
    """The sheet with its music repeated `times` times (a short piece made longer)."""
    times = max(1, int(times))
    text = (abc or "").replace("\r\n", "\n")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("K:"):
            head, body = lines[: i + 1], lines[i + 1:]
            return "\n".join(head + body * times).strip() + "\n"
    return text


def validate(abc: str) -> dict[str, Any]:
    """Can YuE2 take this? Parsed here, then through symusic (the same reader the engine uses to save the MIDI)."""
    problems: list[str] = []
    info: dict[str, Any] = {}
    try:
        sh = parse(abc)
        info = {"key": sh["key"], "meter": sh["meter"], "bpm": sh["bpm"], "bars": sh["bars"], "duration_s": sh["duration_s"],
                "has_chords": sh["has_chords"], "sections": len(sh["sections"]), "melody_voice": sh["melody_voice"]}
        if sh["bars"] == 0:
            problems.append("no bars of music")
        if not any(s["lines"] for s in sh["sections"]):
            problems.append("no melody notes in either voice")
        if sh["duration_s"] > 600:
            problems.append(f"{sh['duration_s']:.0f} s of music is more than the engine can sing (600 s)")
    except Exception as e:
        problems.append(str(e))
    try:
        from symusic import Score  # type: ignore
        Score.from_abc(abc)
    except ImportError:
        pass
    except Exception as e:
        problems.append(f"symusic cannot read it: {str(e)[:200]}")
    tokens = len(abc) // 3     # rough: the tokenizer gets about three characters a token on ABC
    info["tokens_est"] = tokens
    if tokens > 9000:
        problems.append(f"about {tokens} tokens of sheet; the engine's context holds the sheet, the words and the audio together, this is too much")
    return {"ok": not problems, "problems": problems, **info}


# --------------------------------------------------------------------------- sheets from files
def _sheetsage():
    """SheetSage2's own notation code, which writes the exact dialect."""
    if str(WANGP_ROOT) not in sys.path:
        sys.path.insert(0, str(WANGP_ROOT))
    from models.TTS.yue2.sheetsage2 import notation_sheetsage2 as notation  # type: ignore
    return notation


_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _guess_key(weights: list[float]) -> str:
    """Krumhansl-Schmuckler on pitch-class weights -> 'A:minor' / 'C:major'."""
    best, best_v = "C:major", -9.0

    def corr(a, b):
        ma, mb = sum(a) / 12, sum(b) / 12
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) or 1.0
        return num / den
    for root in range(12):
        prof_maj = _MAJ[-root:] + _MAJ[:-root] if root else _MAJ
        prof_min = _MIN[-root:] + _MIN[:-root] if root else _MIN
        for mode, prof in (("major", prof_maj), ("minor", prof_min)):
            v = corr(weights, prof)
            if v > best_v:
                best, best_v = f"{_NAMES_SHARP[root]}:{mode}", v
    return best


def _bar_chords(notes: list[tuple[float, float, int]], bars: list[tuple[float, float]], key: str) -> list[list[Any]]:
    """A chord per bar from whatever notes sound in it: the major or minor triad that covers the most of them,
    diatonic to the key preferred. Rows [start, end, 'A:min'] in SheetSage2's chord format."""
    rows = []
    root_name, mode = key.split(":")
    tonic = _NAMES_SHARP.index(root_name) if root_name in _NAMES_SHARP else 0
    scale = [0, 2, 4, 5, 7, 9, 11] if mode == "major" else [0, 2, 3, 5, 7, 8, 10]
    diatonic = {(tonic + d) % 12 for d in scale}
    for start, end in bars:
        w = [0.0] * 12
        for s, e, p in notes:
            o = max(0.0, min(e, end) - max(s, start))
            if o > 0:
                w[p % 12] += o
        if sum(w) <= 0:
            rows.append([start, end, "N"])
            continue
        best, bv = "N", -1.0
        for root in range(12):
            for q, third in (("maj", 4), ("min", 3)):
                tri = {root, (root + third) % 12, (root + 7) % 12}
                v = sum(w[pc] for pc in tri) + (0.15 * sum(w) if tri <= diatonic else 0.0) + (0.05 * sum(w) if root == tonic else 0.0)
                if v > bv:
                    best, bv = f"{_NAMES_SHARP[root]}:{q}", v
        rows.append([start, end, best])
    return rows


def _assemble(melody: list[tuple[float, float, int]], beats: list[list[Any]], chords: list[list[Any]] | None, key: str,
              structures: list[list[Any]], melody_only: bool = False) -> str:
    """Rows -> SheetSage2's assembler -> the dialect. melody: [(start_s, end_s, pitch)]."""
    import pretty_midi  # type: ignore
    notation = _sheetsage()
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0, name="Vocal")
    for s, e, p in melody:
        if e > s:
            inst.notes.append(pretty_midi.Note(velocity=90, pitch=int(p), start=float(s), end=float(e)))
    pm.instruments.append(inst)
    buf = io.BytesIO()
    pm.write(buf)
    end = beats[-1][0]
    keys = [[beats[0][0], end, key]]
    structures = [[max(beats[0][0], float(a)), min(end, float(b)), lab] for a, b, lab in structures if float(b) > beats[0][0] and float(a) < end] or [[beats[0][0], end, "verse"]]
    chords = chords if (chords and not melody_only) else []
    text, _score = notation.generate_abc_from_data(buf.getvalue(), beats, chords, keys, structures, melody_only=melody_only or not chords)
    return text


def _beats_from_grid(duration: float, bpm: float, meter: tuple[int, int], offset: float = 0.0) -> list[list[Any]]:
    """[time, beat_id, numerator, denominator] rows for a fixed tempo and meter."""
    beat_s = 60.0 / bpm * (4.0 / meter[1])
    rows = []
    t = offset
    n = 0
    while t <= duration + beat_s:
        rows.append([round(t, 6), n % meter[0] + 1, meter[0], meter[1]])
        n += 1
        t += beat_s
    return rows


def from_midi(path: str | os.PathLike, melody_track: int | None = None, chords_from_accompaniment: bool = True) -> dict[str, Any]:
    """A sheet from a MIDI file. The melody is the chosen track, or the highest-sounding one with enough notes;
    chords come from the other tracks (or the melody itself) bar by bar; the key from the file or from the notes.
    Returns {abc, key, bpm, meter, melody_track, tracks[]}."""
    import pretty_midi  # type: ignore
    pm = pretty_midi.PrettyMIDI(str(path))
    insts = [(i, ins) for i, ins in enumerate(pm.instruments) if not ins.is_drum and ins.notes]
    if not insts:
        raise ValueError("no notes in that MIDI file")
    tracks = []
    for i, ins in insts:
        pitches = sorted(n.pitch for n in ins.notes)
        tracks.append({"index": i, "name": ins.name or f"track {i}", "notes": len(ins.notes), "median_pitch": pitches[len(pitches) // 2]})
    if melody_track is None:
        cands = [t for t in tracks if t["notes"] >= 8] or tracks
        melody_track = max(cands, key=lambda t: (t["median_pitch"], t["notes"]))["index"]
    mel = pm.instruments[melody_track]
    # monophonic: at one onset keep the top note; a note ends where the next begins
    by_onset: dict[float, Any] = {}
    for n in sorted(mel.notes, key=lambda n: (n.start, -n.pitch)):
        k = round(n.start, 3)
        if k not in by_onset:
            by_onset[k] = n
    mono = sorted(by_onset.values(), key=lambda n: n.start)
    melody = []
    for a, b in zip(mono, mono[1:] + [None]):
        end = a.end if b is None else min(a.end, b.start)
        melody.append((a.start, max(end, a.start + 0.05), a.pitch))
    tempi_t, tempi = pm.get_tempo_changes()
    bpm = float(tempi[0]) if len(tempi) else 120.0
    ts = pm.time_signature_changes
    meter = (ts[0].numerator, ts[0].denominator) if ts else (4, 4)
    duration = max(pm.get_end_time(), melody[-1][1] if melody else 0.0)
    try:
        bt = list(pm.get_beats())
        db_ = set(round(float(x), 6) for x in pm.get_downbeats())
        beats = []
        bid = 0
        for t in bt:
            if round(float(t), 6) in db_:
                bid = 0
            bid += 1
            beats.append([round(float(t), 6), bid, meter[0], meter[1]])
        last = beats[-1][0]
        step = beats[-1][0] - beats[-2][0] if len(beats) > 1 else 60.0 / bpm
        while last < duration + step:
            last += step
            bid = bid % meter[0] + 1
            beats.append([round(last, 6), bid, meter[0], meter[1]])
        if len(beats) < 4:
            raise ValueError
    except Exception:
        beats = _beats_from_grid(duration, bpm, meter)
    # key
    key = None
    if pm.key_signature_changes:
        kn = pm.key_signature_changes[0].key_number
        key = f"{_NAMES_SHARP[kn % 12]}:{'major' if kn < 12 else 'minor'}"
    if not key:
        w = [0.0] * 12
        for ins in pm.instruments:
            if ins.is_drum:
                continue
            for n in ins.notes:
                w[n.pitch % 12] += n.end - n.start
        key = _guess_key(w)
    # chords: from everything that is not the melody, else from the melody
    acc = [(n.start, n.end, n.pitch) for i, ins in insts if i != melody_track for n in ins.notes] if chords_from_accompaniment else []
    source = acc or melody
    bars = []
    for i, b in enumerate(beats):
        if b[1] == 1:
            nxt = next((c[0] for c in beats[i + 1:] if c[1] == 1), beats[-1][0])
            if nxt > b[0]:
                bars.append((b[0], nxt))
    chords = _bar_chords(source, bars, key)
    structures = []
    for ev in list(getattr(pm, "text_events", []) or []) + list(getattr(pm, "lyrics", []) or []):
        lab = str(getattr(ev, "text", "")).strip().lower()
        if lab in TAG_FOR:
            structures.append([float(ev.time), None, lab])
    for i, row in enumerate(structures):
        row[1] = structures[i + 1][0] if i + 1 < len(structures) else beats[-1][0]
    structures = [r for r in structures if r[1] > r[0]]
    abc = _assemble(melody, beats, chords, key, structures)
    return {"abc": abc, "key": key, "bpm": bpm, "meter": f"{meter[0]}/{meter[1]}", "melody_track": melody_track, "tracks": tracks, "words": ""}


# ---- MusicXML
_KIND_TO_Q = {"major": "maj", "minor": "min", "dominant": "7", "major-seventh": "maj7", "minor-seventh": "min7", "diminished": "dim",
              "augmented": "aug", "diminished-seventh": "dim7", "half-diminished": "hdim7", "suspended-fourth": "sus4",
              "suspended-second": "sus2", "major-sixth": "maj6", "minor-sixth": "min6", "dominant-ninth": "7", "major-ninth": "maj7",
              "minor-ninth": "min7", "power": "maj", "": "maj", "none": "N"}
_FIFTHS_MAJOR = ["C", "G", "D", "A", "E", "B", "F#", "C#"]
_FIFTHS_MAJOR_FLAT = ["C", "F", "A#", "D#", "G#", "C#", "F#", "B"]
_FIFTHS_MINOR = ["A", "E", "B", "F#", "C#", "G#", "D#", "A#"]
_FIFTHS_MINOR_FLAT = ["A", "D", "G", "C", "F", "A#", "D#", "G#"]


def _xml_root(path: str | os.PathLike):
    import xml.etree.ElementTree as ET
    p = Path(path)
    data = None
    if p.suffix.lower() == ".mxl" or zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            root_file = None
            try:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                rf = c.find(".//{*}rootfile")
                root_file = rf.get("full-path") if rf is not None else None
            except Exception:
                root_file = None
            if not root_file:
                root_file = next((n for n in z.namelist() if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF")), None)
            if not root_file:
                raise ValueError("no MusicXML inside that .mxl")
            data = z.read(root_file)
    else:
        data = p.read_bytes()
    root = ET.fromstring(data)
    if root.tag.endswith("score-timewise"):
        raise ValueError("timewise MusicXML is not supported; export partwise (every editor does by default)")
    return root


def _pitch_midi(step: str, alter: float, octave: int) -> int:
    return 12 * (int(octave) + 1) + _PC.get(step.upper(), 0) + int(round(alter))


def from_musicxml(path: str | os.PathLike, part_index: int | None = None) -> dict[str, Any]:
    """A sheet from MusicXML (.musicxml / .xml / .mxl): the melody part (the one with lyrics, else the highest),
    its chord symbols if the file has them, its key, meter and tempo, the section marks (rehearsal letters or words
    like Verse / Chorus), and the words under the notes, lined up by section. Returns {abc, words, key, bpm, meter,
    part, parts[]}."""
    root = _xml_root(path)
    parts = root.findall("part")
    if not parts:
        raise ValueError("no parts in that MusicXML")
    names = {}
    for sp in root.findall(".//part-list/score-part"):
        nm = sp.find("part-name")
        names[sp.get("id")] = (nm.text or "").strip() if nm is not None and nm.text else sp.get("id")

    def read_part(part):
        divisions = 1
        bpm = None
        meter = (4, 4)
        key = None
        t = 0.0            # seconds at the measure start
        notes = []         # (start, end, pitch)
        words = []         # (start, syllabic, text)
        harm = []          # (start, 'A:min')
        marks = []         # (start, label)
        bars = []          # (start, end)
        lyric_count = 0
        for measure in part.findall("measure"):
            attrs = measure.find("attributes")
            if attrs is not None:
                d = attrs.find("divisions")
                if d is not None and d.text:
                    divisions = int(float(d.text))
                tm = attrs.find("time")
                if tm is not None:
                    b, bt = tm.find("beats"), tm.find("beat-type")
                    if b is not None and bt is not None:
                        meter = (int(b.text), int(bt.text))
                k = attrs.find("key")
                if k is not None and k.find("fifths") is not None:
                    f = int(k.find("fifths").text or 0)
                    mode = (k.find("mode").text if k.find("mode") is not None and k.find("mode").text else "major").lower()
                    if mode == "minor":
                        key = (_FIFTHS_MINOR[f] if f >= 0 else _FIFTHS_MINOR_FLAT[-f]) + ":minor"
                    else:
                        key = (_FIFTHS_MAJOR[f] if f >= 0 else _FIFTHS_MAJOR_FLAT[-f]) + ":major"
            for snd in measure.iter("sound"):
                if snd.get("tempo"):
                    try:
                        bpm = float(snd.get("tempo"))
                    except ValueError:
                        pass
            for d in measure.findall("direction"):
                pm_ = d.find(".//per-minute")
                if pm_ is not None and pm_.text:
                    try:
                        bpm = float(pm_.text)
                    except ValueError:
                        pass
                for w in d.findall(".//words") + d.findall(".//rehearsal"):
                    lab = (w.text or "").strip().lower()
                    lab = re.sub(r"\s*\d+$", "", lab)
                    if lab in TAG_FOR:
                        marks.append((t, lab))
            cur_bpm = bpm or 120.0
            q = 60.0 / cur_bpm          # seconds per quarter
            bar_len = meter[0] * (4.0 / meter[1]) * q
            cursor = 0                  # divisions from the measure start
            last_pitch_at: dict[int, Any] = {}
            for el in measure:
                if el.tag == "backup":
                    cursor -= int(float(el.find("duration").text))
                elif el.tag == "forward":
                    cursor += int(float(el.find("duration").text))
                elif el.tag == "harmony":
                    rs = el.find("root/root-step")
                    ra = el.find("root/root-alter")
                    kind = el.find("kind")
                    if rs is not None and rs.text:
                        pc = (_PC.get(rs.text.strip().upper(), 0) + int(float(ra.text)) if ra is not None and ra.text else _PC.get(rs.text.strip().upper(), 0)) % 12
                        kq = _KIND_TO_Q.get((kind.text or "").strip().lower() if kind is not None and kind.text else "major", "maj")
                        off = el.find("offset")
                        o = int(float(off.text)) if off is not None and off.text else 0
                        harm.append((t + (cursor + o) / divisions * q, "N" if kq == "N" else f"{_NAMES_SHARP[pc]}:{kq}"))
                elif el.tag == "note":
                    dur_el = el.find("duration")
                    dur = int(float(dur_el.text)) if dur_el is not None and dur_el.text else 0
                    voice = el.find("voice")
                    if el.find("grace") is not None:
                        continue
                    is_chord = el.find("chord") is not None
                    start_div = cursor if not is_chord else cursor - dur
                    if voice is not None and voice.text and voice.text.strip() not in ("1", ""):
                        if not is_chord:
                            cursor += dur
                        continue
                    start = t + start_div / divisions * q
                    end = start + dur / divisions * q
                    if el.find("rest") is None:
                        p = el.find("pitch")
                        if p is not None:
                            alter = p.find("alter")
                            pitch = _pitch_midi(p.find("step").text, float(alter.text) if alter is not None and alter.text else 0.0, int(p.find("octave").text))
                            tie_stop = any(x.get("type") == "stop" for x in el.findall("tie")) or any(x.get("type") == "stop" for x in el.findall(".//tied"))
                            if is_chord and notes and abs(notes[-1][0] - start) < 1e-6:
                                if pitch > notes[-1][2]:
                                    notes[-1] = (start, end, pitch)
                            elif tie_stop and notes and notes[-1][2] == pitch and abs(notes[-1][1] - start) < 0.02:
                                notes[-1] = (notes[-1][0], end, pitch)
                            else:
                                notes.append((start, end, pitch))
                            for ly in el.findall("lyric"):
                                txt = ly.find("text")
                                syl = ly.find("syllabic")
                                if txt is not None and txt.text:
                                    words.append((start, (syl.text if syl is not None and syl.text else "single"), txt.text))
                                    lyric_count += 1
                    if not is_chord:
                        cursor += dur
            bars.append((t, t + bar_len))
            t += bar_len
        return {"notes": notes, "words": words, "harm": harm, "marks": marks, "bars": bars, "bpm": bpm or 120.0, "meter": meter, "key": key,
                "lyrics": lyric_count, "end": t}

    read = [(i, read_part(p)) for i, p in enumerate(parts)]
    parts_info = []
    for i, r in read:
        ps = sorted(p for _, _, p in r["notes"])
        parts_info.append({"index": i, "name": names.get(parts[i].get("id"), f"part {i + 1}"), "notes": len(r["notes"]), "lyrics": r["lyrics"],
                           "median_pitch": ps[len(ps) // 2] if ps else 0})
    if part_index is None:
        with_notes = [p for p in parts_info if p["notes"]]
        if not with_notes:
            raise ValueError("no notes in that MusicXML")
        part_index = max(with_notes, key=lambda p: (p["lyrics"] > 0, p["median_pitch"], p["notes"]))["index"]
    r = dict(read)[part_index]
    if not r["notes"]:
        raise ValueError("that part has no notes")
    # monophonic melody, no overlaps
    mono = sorted(r["notes"])
    melody = []
    for a, b in zip(mono, mono[1:] + [None]):
        end = a[1] if b is None else min(a[1], b[0])
        melody.append((a[0], max(end, a[0] + 0.05), a[2]))
    meter = r["meter"]
    bpm = r["bpm"]
    duration = max(r["end"], melody[-1][1])
    beats = _beats_from_grid(duration, bpm, meter)
    key = r["key"]
    if not key:
        w = [0.0] * 12
        for s, e, p in melody:
            w[p % 12] += e - s
        key = _guess_key(w)
    harm = sorted(r["harm"])
    chords = None
    if harm:
        chords = []
        for i, (s, c) in enumerate(harm):
            e = harm[i + 1][0] if i + 1 < len(harm) else beats[-1][0]
            if e > s:
                chords.append([s, e, c])
    else:
        # no chord symbols in the file: chords from all the parts' notes, bar by bar
        allnotes = [n for _, rr in read for n in rr["notes"]]
        chords = _bar_chords(allnotes, r["bars"], key)
    structures = []
    for i, (s, lab) in enumerate(sorted(r["marks"])):
        e = sorted(r["marks"])[i + 1][0] if i + 1 < len(r["marks"]) else beats[-1][0]
        if e > s:
            structures.append([s, e, lab])
    abc = _assemble(melody, beats, chords, key, structures)
    # the words, lined up by section of the finished sheet
    words_text = ""
    if r["words"]:
        sh = parse(abc)
        secs = [s for s in sh["sections"] if s["tag"]]
        lines_by_sec: list[list[str]] = [[] for _ in secs]
        buf = ""
        buf_start = None
        for start, syl, txt in sorted(r["words"], key=lambda x: x[0]):
            if syl in ("begin", "middle"):
                buf = (buf + txt) if buf else txt
                buf_start = buf_start if buf_start is not None else start
                continue
            word = (buf + txt) if buf else txt
            wstart = buf_start if buf_start is not None else start
            buf, buf_start = "", None
            where = next((i for i, s in enumerate(secs) if s["start_s"] <= wstart < s["end_s"]), len(secs) - 1 if secs else None)
            if where is None:
                continue
            sec = secs[where]
            # one line per phrase of the melody: the phrase whose time window holds this word
            ph = sec.get("phrases") or []
            k = next((j for j, p in enumerate(ph) if p["start_s"] - 0.05 <= wstart <= p["end_s"] + 0.05), None)
            if k is None:
                k = max(0, len(ph) - 1) if ph else 0
            while len(lines_by_sec[where]) <= k:
                lines_by_sec[where].append("")
            lines_by_sec[where][k] = (lines_by_sec[where][k] + " " + word).strip()
        out = []
        for s, lines in zip(secs, lines_by_sec):
            out.append(f"[{s['tag']}]")
            out.extend(l for l in lines if l)
            out.append("")
        words_text = "\n".join(out).strip() + "\n"
    return {"abc": abc, "words": words_text, "key": key, "bpm": bpm, "meter": f"{meter[0]}/{meter[1]}", "part": part_index, "parts": parts_info}


def audiveris_path() -> str | None:
    """Audiveris (optical music recognition, Java) if it is installed: TF_AUDIVERIS, or the usual places."""
    cand = [os.environ.get("TF_AUDIVERIS") or ""]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            cand += [str(p) for p in Path(base).glob("Audiveris*/Audiveris.exe")] + [str(p) for p in Path(base).glob("Audiveris*/bin/Audiveris.bat")]
    cand += ["audiveris"]
    for c in cand:
        if c and (Path(c).exists() or c == "audiveris"):
            try:
                if c == "audiveris" and not any((Path(d) / "audiveris").exists() or (Path(d) / "audiveris.bat").exists() for d in os.environ.get("PATH", "").split(os.pathsep)):
                    continue
            except Exception:
                continue
            return c
    return None


def from_pdf(path: str | os.PathLike) -> dict[str, Any]:
    """Printed sheet music (PDF or an image) through Audiveris to MusicXML, then from_musicxml. Audiveris is a
    separate install (open source, Java); without it this says so."""
    exe = audiveris_path()
    if not exe:
        raise RuntimeError("Reading printed sheet music needs Audiveris (free, https://github.com/Audiveris/audiveris). "
                           "Install it, or set TF_AUDIVERIS to its program, then try again.")
    out = Path(tempfile.mkdtemp(prefix="tf_omr_"))
    cmd = [exe, "-batch", "-export", "-output", str(out), str(path)]
    LOG.info("sheet: audiveris %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    found = list(out.rglob("*.mxl")) + list(out.rglob("*.xml")) + list(out.rglob("*.musicxml"))
    if not found:
        raise RuntimeError("Audiveris could not read that page" + (f": {proc.stderr[-300:]}" if proc.stderr else ""))
    res = from_musicxml(found[0])
    res["omr"] = str(found[0])
    return res


def from_file(path: str | os.PathLike) -> dict[str, Any]:
    """A sheet from whatever file this is: .abc as it is (checked), .mid/.midi, .musicxml/.xml/.mxl, .pdf/.png/.jpg via Audiveris."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".abc" or ext == ".txt":
        abc = p.read_text(encoding="utf-8-sig")
        v = validate(abc)
        if not v["ok"]:
            raise ValueError("; ".join(v["problems"]))
        return {"abc": abc, "words": "", "key": v.get("key"), "bpm": v.get("bpm"), "meter": v.get("meter")}
    if ext in (".mid", ".midi"):
        return from_midi(p)
    if ext in (".musicxml", ".xml", ".mxl"):
        return from_musicxml(p)
    if ext in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        return from_pdf(p)
    raise ValueError(f"not a sheet I can read: {ext}")


SHEET_EXTS = (".abc", ".mid", ".midi", ".musicxml", ".xml", ".mxl", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
