"""llm.py — lyric and style writer on this computer's OWN Ollama (127.0.0.1:11434, qwen2.5:14b Q4_K_M, ~9.5 GB VRAM).

The music app lives entirely on this computer. The model is asked for with a short keep_alive so it leaves the
graphics card a few minutes after planning; the radio planner waits for VRAM when a render is in flight (see
server.py). Follows the owner's lyric framework (plainspoken, 4–8 words a line, section tags, no emotion words).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
import random
import threading
import time
import urllib.request
from typing import Any

LOG = logging.getLogger("tenforward.llm")

try:
    import craft  # distilled songwriting notes (craft/craft_notes.json); optional
except Exception:  # pragma: no cover
    craft = None


def craft_block(style: str = "", theme_text: str = "", station_text: str = "", instrumental: bool = False) -> str:
    """Core craft cards + a few matching topic cards, or '' when the notes are not installed."""
    if craft is None:
        return ""
    try:
        return craft.notes(style=style, theme_text=theme_text, station_text=station_text, instrumental=instrumental)
    except Exception as e:
        LOG.warning("craft notes failed: %s", e)
        return ""

BASE_URL = os.environ.get("TF_LLM_URL", "http://127.0.0.1:11434/v1")  # this computer's Ollama (tray app), local only
MODELS = [m for m in os.environ.get("TF_LLM_MODELS", "qwen2.5:14b").split(",") if m]
TIMEOUT = int(os.environ.get("TF_LLM_TIMEOUT", "180"))
# One request at a time: Ollama serves a single 20B model, so parallel planners would only queue up behind each
# other and trip the quick_ok probe. _last_ok lets quick_ok skip the probe when the model answered recently.
_LOCK = threading.Lock()
NUM_CTX = int(os.environ.get("TF_LLM_NUM_CTX", "4096"))  # Ollama defaults to 32k context (about 3 GB of KV cache); lyrics need 4k
KEEP_ALIVE = os.environ.get("TF_LLM_KEEP_ALIVE", "10m")  # shares the graphics card with YuE2: leave VRAM a few minutes after planning
# Optional second brain on another box; empty by default because everything for this app stays on this computer.
FALLBACK_URL = os.environ.get("TF_LLM_FALLBACK_URL", "")
FALLBACK_MODEL = os.environ.get("TF_LLM_FALLBACK_MODEL", "")
_last_ok = 0.0
_primary_down_until = 0.0  # set when both probes on the primary fail; _chat skips the primary until then


def configure(url=None, models=None, num_ctx=None, keep_alive=None, timeout=None, fallback_url=None, fallback_model=None) -> None:
    """Point the writer at another server or model while it runs (the Settings tab writes these). Clears the cooldown."""
    global BASE_URL, MODELS, NUM_CTX, KEEP_ALIVE, TIMEOUT, FALLBACK_URL, FALLBACK_MODEL, _last_ok, _primary_down_until
    if url:
        BASE_URL = str(url).rstrip("/")
    if models is not None:
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        MODELS = list(models) or MODELS
    if num_ctx:
        NUM_CTX = int(num_ctx)
    if keep_alive:
        KEEP_ALIVE = str(keep_alive)
    if timeout:
        TIMEOUT = int(timeout)
    FALLBACK_URL = str(fallback_url or "").rstrip("/")
    FALLBACK_MODEL = str(fallback_model or "")
    _last_ok = 0.0
    _primary_down_until = 0.0
    LOG.info("LLM: writer is %s on %s%s", MODELS[0] if MODELS else "(none)", BASE_URL,
             f", fallback {FALLBACK_MODEL} on {FALLBACK_URL}" if (FALLBACK_URL and FALLBACK_MODEL) else "")

LYRIC_RULES = """You write song lyrics for a lyrics-to-song AI (YuE2). Follow these rules exactly:
- Plainspoken, first person. Every line reads as spoken thought: something you would actually say out loud to a friend while telling them what happened. Show, do not name emotions (never write sad, happy, lonely, love as a feeling word).
- No poetic, abstract or metaphor-heavy lines. Say the plain thing: not "the silence screams your name" but "you never called me back".
- Name real things from a real life: the job, the truck, the bill, the bar, somebody's mama. Not set dressing. People are you, she, he, or what they are to the singer; real songs rarely call anybody by name. Never give a street, a road or a town a name, real or made up: name a place only when the song is given one. Never describe light (streetlights, the fridge light, light through the blinds, lights that flicker), and do not lean on echoes, whispers, shadows, ghosts, the silence, the air, faded memories, empty rooms, a clock ticking, or a sky being painted. Those are how machine lyrics sound.
- Never use these worn rhyme pairs: night/light, fire/desire, heart/apart, pain/rain, love/above, sky/high, forever/together, dreams/seems, true/you.
- Every line is a whole sentence somebody could say, in normal word order. No lists of nouns, no line that is only a picture, and never twist the word order to land a rhyme.
- The difference, line by line:
  NOT "Morning light bleeds through the glass"     SAY "I slept in my truck outside your place"
  NOT "The store's lights glow, a quiet buzz"      SAY "I stock the cooler at the Shell till two"
  NOT "Autumn's breath is cold"                    SAY "It's October and you still ain't called"
  NOT "Her laugh, her touch, her words"            SAY "You laughed at every dumb thing I said"
  NOT "We splash in the water cool"                SAY "We jumped off the dock in our clothes"
- 4 to 8 words per line, keep line length even inside a section. 4 to 8 lines per section.
- Rhyme (AABB or ABAB) only when the whole line still reads as something a person would say. NEVER tack a word onto the end of a line to make a rhyme (no 'I wait, light' or 'the grind feels phone'); if the rhyme does not come naturally, do not rhyme that line.
- Every line is one complete thought or image. Read each line alone: if it is not a sentence or a natural phrase, rewrite it.
- Eighth-grade language, clear and direct. One tone per song. No ellipses, no dashes, no filler syllables.
- Section tags on their own line, in square brackets, in this style: [Intro] [Verse 1] [Chorus] [Verse 2] [Bridge] [Chorus] [Outro]. Optional: [Pre-Chorus], [Verse 3].
- Repeat the chorus words when the chorus returns. Leave one blank line between sections.
- No production notes, no BPM, no key, no sound effect words, no instrument names inside the lyrics.
- Capitalize the first word of each line. No stage directions.
Output ONLY the lyrics with their section tags. No title line, no commentary, no code fences."""

RAP_RULES = """You write rap lyrics for a lyrics-to-song AI (YuE2). Follow these rules exactly:
- First person, one persona, one voice the whole song. Confident, specific, streetwise; humor and menace both fine.
- Two lanes, pick one or blend them: gangster storytelling (the block, the come-up, loyalty and snakes, survival) or drip and charisma (cars, chains, designer, women, wins, the flex with wit). No R&B crooning, no love-ballad softness, no lessons at the end.
- Do not lean on the props every rap song reaches for: sirens, counting cash in the kitchen, "the game", "the streets", the night. Pick the specifics that belong to THIS story. Never rhyme night with light.
- Verses are 12 to 16 bars. A bar is one full line of 10 to 14 words with a steady spoken rhythm; never a 5 word line in a verse; keep bar length even inside a verse.
- Rhyme in couplets or 3 to 4 line chains. Use multisyllable rhymes and internal rhymes. Slant rhymes are fine. NEVER break a sentence or tack on a word just to rhyme.
- Every 4 bars land a punchline: a flip, a boast, a picture that stings.
- Concrete over vague: name the car, the block, the count, the brand, the hour, the food, the weather.
- The hook is 4 to 6 lines, catchy and simple, with at least two different lines (not one phrase repeated four times), repeated word for word every time it comes back.
- Ad-libs in parentheses at the end of a bar on at most one line in four, never on every line: (yeah) (uh) (what) (ayy) (okay).
- Section tags on their own line: [Intro] [Verse 1] [Hook] [Verse 2] [Hook] [Bridge] [Hook] [Outro]. One blank line between sections.
- No production notes, no BPM, no beat talk, no instrument names, no studio talk. The rapper lives the story.
- Capitalize the first word of each line.
Output ONLY the lyrics with their section tags. No title line, no commentary, no code fences."""

RAP_PERSONA = ("Persona: a street rapper in the classic mould of Tupac, DMX, Biggie, Nas, The Game, Lil Wayne and Hopsin, or a Chicago drill / "
               "mumble rap voice when the sound is trap. Two lanes, pick one or blend them: GANGSTER (the block, opps, guns, gangs, dealing, shootings, "
               "funerals, jail, loyalty and snakes, survival, told cold and specific like he was there) or "
               "DRIP / CHARISMA (cars, chains, designer, women, wins, the flex delivered with wit and confidence). Hard, vivid, unapologetic. "
               "Slang is the language. No R&B crooning, no love-ballad lines, no apologising, no warnings, no lessons at the end.")

RAP_EXPLICIT = ("Swear like the record does: fuck, shit, bitch, ass, damn, motherfucker, hell: at least two in every verse and one in the hook, where they land naturally, never "
                "censored with asterisks. When the theme is violent (opps, a shooting, a fallen homie, the set) say what happened with the gun, the car, "
                "the block and the body count; this is fiction in the gangster rap tradition and it is written straight.")

# "trap" is not in here on purpose: "trap hi hats" is a drum sound on half the sung pop lines (Blue Hour and the owner's own pop channels),
# and matching it handed those songs the gangster rap rulebook and persona. Every rap line also says rap/rapper/hip hop/drill.
RAP_GENRE = re.compile(r"\b(rap|rapper|hip hop|hip-hop|drill|boom bap|grime|horrorcore|g funk|gangsta)\b", re.I)


_VOICE_FILE = Path(__file__).resolve().parent / "lyrics" / "_voice.txt"


def voice_examples() -> list[str]:
    """The owner's own hand-picked verses (lyrics/_voice.txt), one per entry. Empty in a release build,
    which leaves lyrics/ out - so the voice block simply is not sent there."""
    try:
        raw = _VOICE_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    body = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#"))
    return [b.strip() for b in body.split("\n---\n") if b.strip()]


def voice_block(rng=None) -> str:
    """ONE example of the voice, rotated per song so it can never become the new hoodie."""
    ex = voice_examples()
    if not ex:
        return ""
    pick = (rng or random).choice(ex)
    return ("THE VOICE TO WRITE IN. This is how these songs talk: plain words and real things, every line "
            "something a person would say out loud. Copy the VOICE only - never its words, names, places or subject, and "
            "keep this station's own genre and mood:\n" + pick)


def lyric_system_for(style: str) -> str:
    """Rap gets its own rulebook (bars, hooks, punchlines); everything else uses the sung-lyric rules."""
    return RAP_RULES if style and RAP_GENRE.search(style) else LYRIC_RULES


STYLE_RULES = """You write the style prompt for a lyrics-to-song AI (YuE2). One line, comma separated tags in this order:
language, genre and subgenre, vocal character (gender, register, texture such as warm, husky, airy, clear), 3 to 6 instruments, mood words that describe sound not feelings (driving, laid back, sparse, lush), tempo as BPM.
Example: English, country rock, warm male baritone, acoustic rhythm guitar, pedal steel, upright bass, brushed drums, laid back groove, 92 BPM
Output ONLY that one line."""

TITLE_RULES = ("Give this song its title the way real songs get theirs: a short phrase, 2 to 5 words and never more, taken from "
               "the lyrics - the hook from the chorus if it has one. Lead with the thing in that phrase, not "
               "with She, He, I, I'm, You, We or They, and never a street name. Plain English words only: nothing made up, no symbols, no emoji. Output "
               "only the title, no quotes.")

_SMALL = {"the", "a", "an", "on", "in", "of", "to", "and", "at", "my", "your", "our", "her", "his", "with", "for", "from", "by", "that", "is"}


def _short_title(title: str) -> str:
    """Five words at most, cut where a phrase would end rather than mid-way ("...on the")."""
    words = title.split()
    if len(words) <= 5:
        return title
    words = words[:5]
    while len(words) > 2 and words[-1].lower().strip("',.!?") in _SMALL:
        words.pop()
    return " ".join(words)


def _fallback_base() -> str:
    # the same /v1 trap the primary avoids: Ollama's native API is one level above the OpenAI-compatible root
    return re.sub(r"/v1/?$", "", (FALLBACK_URL or "").rstrip("/"))


def _fallback_resident() -> dict[str, Any] | None:
    """The fallback model's entry in the other box's /api/ps, or None if it is not loaded there.

    That box may be shared: other apps can depend on the model it keeps loaded. Asking it for a model that is not
    loaded, or at a different num_ctx, makes Ollama unload and reload it (minutes on an older card), and a caller
    that gives up sooner aborts that load for EVERYONE else waiting on it.
    So Ten Forward only ever uses the model exactly as it is already running there."""
    if not (FALLBACK_URL and FALLBACK_MODEL):
        return None
    try:
        with urllib.request.urlopen(_fallback_base() + "/api/ps", timeout=4) as r:
            for m in json.loads(r.read()).get("models") or []:
                if FALLBACK_MODEL in (m.get("name"), m.get("model")):
                    return m
    except Exception:
        pass
    return None


def _fallback_body_options(resident: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """(options, keep_alive) that match the running model, so the request reuses it rather than reloading it."""
    opts: dict[str, Any] = {}
    if resident.get("context_length"):
        opts["num_ctx"] = int(resident["context_length"])
    keep: Any = None
    exp = str(resident.get("expires_at") or "")
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(re.sub(r"[.][0-9]+", "", exp).replace("Z", "+00:00"))   # Ollama sends nanoseconds
        left = (when - datetime.now(timezone.utc)).total_seconds()
        keep = -1 if left > 365 * 86400 else max(60, int(left))   # pinned stays pinned; otherwise its own expiry
    except Exception:
        keep = -1 if exp[:4].isdigit() and int(exp[:4]) > 2100 else None
    return opts, keep


def _native_base() -> str:
    # BASE_URL is the OpenAI-compatible root (.../v1); Ollama's native API lives one level up.
    return re.sub(r"/v1/?$", "", BASE_URL.rstrip("/"))


def _chat(messages: list[dict[str, str]], max_tokens: int = 1200, temperature: float = 0.9, model: str | None = None, timeout: int | None = None, max_models: int | None = None) -> str:
    """Ollama native /api/chat. gpt-oss models get think="low" so the answer lands in content
    (the OpenAI-compatible endpoint leaks their reasoning into the reply)."""
    global _last_ok
    last_err: Exception | None = None
    models = [model] if model else MODELS[: max_models or len(MODELS)]
    with _LOCK:
        return _chat_locked(models, messages, max_tokens, temperature, timeout)


def _chat_locked(models, messages, max_tokens, temperature, timeout) -> str:
    global _last_ok
    last_err: Exception | None = None
    targets = [(_native_base(), m) for m in models]
    if time.time() < _primary_down_until and FALLBACK_URL and FALLBACK_MODEL:
        LOG.info("LLM: primary brain in cooldown for %ds more; using the fallback directly", int(_primary_down_until - time.time()))
        targets = []
    if FALLBACK_URL and FALLBACK_MODEL:
        targets.append((_fallback_base(), FALLBACK_MODEL))
    for base, m in targets:
        body: dict[str, Any] = {"model": m, "messages": messages, "stream": False, "keep_alive": KEEP_ALIVE, "options": {"num_predict": max_tokens, "temperature": temperature, "num_ctx": NUM_CTX}}
        if base != _native_base():
            resident = _fallback_resident()
            if resident is None:
                LOG.warning("LLM: primary brain failed (%s); fallback %s is not loaded on %s, so it is left alone "
                            "(waking a shared model makes everyone on that box wait for the load)", last_err, m, base)
                continue
            opts, keep = _fallback_body_options(resident)
            body["options"] = {"num_predict": max_tokens, "temperature": temperature, **opts}
            if keep is None:
                body.pop("keep_alive", None)
            else:
                body["keep_alive"] = keep
            LOG.warning("LLM: primary brain failed (%s); using fallback %s on %s as it is running (num_ctx %s)",
                        last_err, m, base, opts.get("num_ctx", "its own"))
        if "gpt-oss" in m.lower():
            body["think"] = "low"
        req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
                data = json.loads(r.read())
            msg = data.get("message", {})
            content = (msg.get("content") or "").strip()
            if content:
                _last_ok = time.time()
                return _strip_thinking(content)
            if data.get("done_reason") == "length" and max_tokens < 6000:
                # gpt-oss spent the whole budget thinking: try once more with room to answer
                LOG.warning("LLM %s ran out of tokens while thinking (budget %d); retrying with %d", m, max_tokens, max_tokens * 3)
                body["options"]["num_predict"] = max_tokens * 3
                req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=(timeout or TIMEOUT) * 2) as r:
                    data = json.loads(r.read())
                content = ((data.get("message") or {}).get("content") or "").strip()
                if content:
                    _last_ok = time.time()
                    return _strip_thinking(content)
            last_err = RuntimeError(f"{m}: empty reply (done_reason={data.get('done_reason')})")
        except Exception as e:  # try next model
            last_err = e
            LOG.warning("LLM %s failed: %s", m, e)
    raise RuntimeError(f"all LLM models failed: {last_err}")


def _strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<\|channel\|>.*?<\|message\|>", "", text, flags=re.S)
    text = text.replace("```", "").strip()
    return text


_CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]+")


def tidy_title(title: str) -> str:
    """Only real letters, digits, spaces and ordinary punctuation. The model has handed back titles like
    'Gone Leaving' followed by subscript and currency symbols, and CJK-stripping alone let them through."""
    import unicodedata
    t = (title or "").replace("\u2019", "'").replace("\u2018", "'")
    keep = []
    for ch in t:
        cat = unicodedata.category(ch)
        if cat in ("Lu", "Ll", "Lt", "Nd") or ch == " " or ch in "'-&!?,.":
            keep.append(ch)
    return re.sub(r"\s{2,}", " ", "".join(keep)).strip(" -,.")


def strip_cjk(text: str) -> str:
    """qwen2.5 sometimes slips a Chinese word into an English line; drop it and tidy the spacing."""
    out = _CJK.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def clean_lyrics(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = strip_cjk(raw).rstrip()
        if not line.strip():
            lines.append("")
            continue
        if re.match(r"^\s*(title|lyrics)\s*[:\-]", line, re.I):
            continue
        m = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if m:
            tag = m.group(1).strip()
            tag = re.sub(r"\s+", " ", tag)
            tag = tag[:1].upper() + tag[1:]
            lines.append(f"[{tag}]")
            continue
        line = line.strip().strip("*").strip()
        lines.append(line[:1].upper() + line[1:] if line else line)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def write_lyrics(topic: str, mood: str = "", style: str = "", length: str = "standard", extra_rules: str = "", instrumental: bool = False) -> str:
    if instrumental:
        return "[Intro]\n[Verse]\n[Chorus]\n[Verse]\n[Chorus]\n[Bridge]\n[Chorus]\n[Outro]"
    structure = {
        "short": "[Verse 1] [Chorus] [Verse 2] [Chorus], about 90 seconds of singing",
        "standard": "[Intro] [Verse 1] [Chorus] [Verse 2] [Chorus] [Bridge] [Chorus] [Outro], about 3 minutes",
        "long": "[Intro] [Verse 1] [Pre-Chorus] [Chorus] [Verse 2] [Pre-Chorus] [Chorus] [Bridge] [Chorus] [Outro], about 4 minutes (needs a 300 s duration)",
    }.get(length, "[Intro] [Verse 1] [Chorus] [Verse 2] [Chorus] [Bridge] [Chorus] [Outro]")
    user = f"Topic or idea: {topic.strip() or 'a night that felt like it mattered'}\n"
    if mood:
        user += f"Mood of the song (do not use these words literally, show them): {mood}\n"
    if style:
        user += f"Musical style it will be sung in: {style}\n"
    user += f"Structure: {structure}\n"
    if extra_rules:
        user += f"Extra instructions: {extra_rules}\n"
    system = lyric_system_for(style)
    notes = craft_block(style, f"{topic} {mood}")
    if notes:
        system += "\n\n" + notes
    text = _chat([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=2500)
    return clean_lyrics(text)


def improve_lyrics(lyrics: str, instruction: str) -> str:
    user = f"Rewrite these lyrics. Instruction: {instruction}\nKeep the section tags. Keep what works.\n\n{lyrics}"
    text = _chat([{"role": "system", "content": LYRIC_RULES}, {"role": "user", "content": user}], max_tokens=2500, temperature=0.7)
    return clean_lyrics(text)


def suggest_style(description: str, lyrics: str = "") -> str:
    user = f"Description of the song wanted: {description.strip() or 'something that fits the lyrics'}"
    if craft is not None:
        try:
            words = re.findall(r"[a-z&\- ]{3,}", (description or "").lower())
            pairs = []
            for g in craft.genre_names():
                if g in (description or "").lower():
                    pairs = craft.genre_pairs(g)
                    if pairs:
                        user += f"\nStyles that pair well with {g}: {', '.join(pairs)} (use at most one)."
                        break
        except Exception:
            pass
    if lyrics:
        user += f"\n\nLyrics:\n{lyrics[:1500]}"
    text = _chat([{"role": "system", "content": STYLE_RULES}, {"role": "user", "content": user}], max_tokens=120, temperature=0.8)
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line.strip().strip('"').strip()


def ask_json(system: str, user: str, max_tokens: int = 1600, temperature: float = 0.8, timeout: int = 150) -> str:
    """One question, one json answer, asked of the pinned model. The caller parses and repairs it: this is used
    by the channel wizard, where a person is waiting, not by anything the radio does on its own."""
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                 max_tokens=max_tokens, temperature=temperature, timeout=timeout, max_models=2)


def title_for(lyrics: str, fallback: str = "Untitled", avoid: list[str] | None = None, not_starting: list[str] | None = None) -> str:
    try:
        user = lyrics[:1200]
        if avoid:
            user = "Titles already taken on this station, do not use any of them or a near copy: " + "; ".join(a for a in avoid[:20] if a) + "\n\n" + user
        if not_starting:
            user = ("This station's recent titles keep starting the same way. Do not start this title with any of these words: "
                    + ", ".join(not_starting) + "\n\n" + user)
        text = _chat([{"role": "system", "content": TITLE_RULES}, {"role": "user", "content": user}], max_tokens=80, temperature=0.8 if (avoid or not_starting) else 0.6)
        title = strip_cjk(text.strip().splitlines()[0]).strip().strip('"').strip("*").strip()
        title = tidy_title(re.sub(r"[\\/:*?\"<>|]", "", title))
        if len(title.split()) > 5:
            # asked for 2 to 5 and got a sentence: once more, then cut it cleanly
            again = _chat([{"role": "system", "content": TITLE_RULES},
                           {"role": "user", "content": f"That title is too long: {title}\nGive a shorter one, 2 to 5 words, "
                                                       f"from these lyrics.\n\n{lyrics[:1200]}"}],
                          max_tokens=80, temperature=0.6)
            shorter = tidy_title(strip_cjk((again or "").strip().splitlines()[0] if (again or "").strip() else "").strip('"*'))
            title = shorter if 2 <= len(shorter.split()) <= 5 else _short_title(title)
        if len(title) < 2:
            first = next((l.strip() for l in lyrics.splitlines() if l.strip() and not l.strip().startswith("[")), "")
            title = strip_cjk(first)[:40] or fallback
        return title[:60] or fallback
    except Exception:
        return fallback


STORY_IDEA_RULES = ("You invent what happens in one song, in two or three sentences, written in third person. Something has to HAPPEN: "
                    "people doing something with each other or to each other - something said, a plan, a dare, a mistake, a choice - "
                    "and how it turned out. Tell it the way the singer would tell a friend what happened, with a detail or two a person "
                    "would really mention (a job, a car, a bill, who said what). People are he, she, or what they are to each other, "
                    "not first names. Never a still picture: nobody just stands, sits, "
                    "leans, stares or watches; nothing is seen from across a street or a room or through a window; no glow, nothing dimly "
                    "lit, no light, no appliance humming. Never name a street, a road or a town. No emotion words. The idea is about a "
                    "life, never about music: do not mention songs, beats, verses, studios, instruments, tempo, BPM or performing. "
                    "Output only the idea, no title, no commentary.")


def invent_song(station: dict[str, Any], recent_titles: list[str] | None = None, brief: dict[str, str] | None = None, style: str = "",
                recent_ideas: list[str] | None = None, idea_block: str = "", lyric_block: str = "",
                idea_check=None, classic: bool = False, story_turn: str = "", idea_score=None) -> dict[str, str]:
    """For radio: returns {topic, lyrics, style, title} for a station. `brief` (from themes.song_brief) carries the
    theme idea, the singer perspective and the content rules; without it the station description alone is used.
    `recent_ideas` are the scenes of the station's last songs: the writer is told to go somewhere else.
    `idea_block` / `lyric_block` come from lyric_intel: what this channel has been leaning on lately and where to go instead.
    `idea_check` is handed the scene once and may ask, once, for a different one.
    `classic` keeps the pre-1.3 idea writer (a scene, not a story) for a channel that is listed as keeping it;
    `story_turn` is this song's one suggested turn of events (radio.story_turn rotates them per channel)."""
    desc = station.get("description") or station.get("name") or "a song"
    prompts = station.get("style_prompts") or []
    style = style or (prompts[0] if prompts else "")
    avoid = ""
    if recent_titles:
        avoid = "Titles already used on this station (do not reuse them or their subjects): " + "; ".join(t for t in recent_titles[:14] if t) + "\n"
    if recent_ideas:
        avoid += ("Scenes already written for this station. Do NOT reuse their setting, their objects or their images; put this song somewhere else, "
                  "with different things in it:\n" + "\n".join("- " + str(t)[:220] for t in recent_ideas[:8]) + "\n")
    avoid += ("The theme text and the station description both list example images: they only show the KIND of thing this "
              "station is about. Use at most ONE of them in a song, and most songs should use none - a station that says "
              "'headlights on a two lane road' does not want headlights in every song. Invent the rest. "
              "Do not open the idea with the sun, the sky or the weather.")
    brief = brief or {}
    rules = brief.get("rules", "Never mention politics or sports.")
    perspective = brief.get("perspective", "")
    idea_seed = brief.get("idea_prompt", "")
    mood = brief.get("mood") or ""
    explicit = bool(brief.get("explicit") or station.get("explicit"))
    nl = "\n"
    # Radio planning runs unattended: short timeouts, the pinned model only, so a slow second box never stalls the station.
    system_idea = ("You invent one specific, concrete song idea in two or three sentences: a scene, a person, a place, a small event, "
                   "a detail or two that a person would actually mention if they were telling this story to a friend (a name, a job, a car, "
                   "a bill, who said what) - never set dressing like light through a window or an appliance humming, and never an invented "
                   "street or town name - written in third person. No emotion words. The idea is about a life, never about music: do not mention "
                   "songs, beats, verses, studios, instruments, tempo, BPM or performing. Output only the idea, no title, no commentary." + nl + rules)
    if not classic:
        system_idea = STORY_IDEA_RULES + nl + rules
    genre = (style.split(",")[1].strip() if style and "," in style else style) or "pop"
    user_idea = f"Radio station mood: {desc}{nl}Genre it will be sung in (for tone only, do not mention it): {genre}{nl}"
    if RAP_GENRE.search(style or ""):
        user_idea += RAP_PERSONA + (" The idea is a street story or a flex scene with names, streets, cars and amounts." if classic else
                                    " The idea is a street story or a flex scene with cars, amounts and who did what, and something that happens in it.") + nl
        if explicit:
            user_idea += RAP_EXPLICIT + nl
    if mood:
        user_idea += mood + nl
    if idea_seed:
        user_idea += f"Theme to build the idea on: {idea_seed}{nl}"
    if perspective:
        user_idea += perspective + nl
    if brief.get("place"):
        user_idea += f"Place for this song (the one real place allowed): {brief['place']}{nl}"
    if story_turn and not classic:
        user_idea += f"What happens in this song (a suggestion: use it if it fits the theme, bend it if it does not): {story_turn}{nl}"
    user_idea += avoid
    if idea_block:
        user_idea += nl + idea_block
    topic_text = _chat([{"role": "system", "content": system_idea}, {"role": "user", "content": user_idea}],
                       max_tokens=500, temperature=1.0, timeout=60, max_models=2)
    if idea_check:
        # the scene is compared with the ones this channel has already written; at most one more try, or two when
        # the caller can score ideas (idea_score: lower is better) - a retry can come back worse than the first,
        # so the best one seen is kept
        again = idea_check(topic_text)
        tries = 2 if idea_score else 1
        best, best_s = topic_text, (idea_score(topic_text) if idea_score else 0)
        while again and tries > 0:
            tries -= 1
            cand = _chat([{"role": "system", "content": system_idea},
                          {"role": "user", "content": user_idea + nl + again}],
                         max_tokens=500, temperature=1.05, timeout=60, max_models=2)
            if idea_score is None:
                best = cand
                break
            s = idea_score(cand)
            if s <= best_s:          # a tie goes to the new one: the memory may have asked for a different scene
                best, best_s = cand, s
            again = idea_check(cand)
        topic_text = best
    if station.get("instrumental"):
        lyrics = write_lyrics("", instrumental=True)
    else:
        user = f"Topic or idea: {topic_text.strip()}{nl}"
        if RAP_GENRE.search(style or ""):
            user += RAP_PERSONA + nl
            if explicit:
                user += RAP_EXPLICIT + nl
        if mood:
            user += mood + nl
        if perspective:
            user += perspective + nl
        if brief.get("place"):
            user += f"Place for this song (the one real place allowed; name it once or twice, naturally): {brief['place']}{nl}"
        user += (f"Sound of the station (do not use these words literally): {desc}{nl}Genre it will be sung in: {genre}{nl}"
                 f"Structure: [Intro] [Verse 1] [Chorus] [Verse 2] [Chorus] [Bridge] [Chorus] [Outro], about 3 minutes{nl}{rules}{nl}"
                 "The chorus is 4 to 6 lines. Every line is a complete natural phrase a person would say out loud. Never end a line on a word just to rhyme; "
                 "if the rhyme does not come naturally, do not rhyme. The singer lives the scene: never mention the song, the beat, "
                 "writing lyrics, the studio, instruments or tempo. Vary the lines; do not reuse the same image more than twice. "
                 "The first line of Verse 1 is an action or an object, never the sun, the sky, the weather or the time of day. "
                 "Stay inside the one scene of the idea; do not add a lake, a truck, a beach or a bonfire unless the idea has one.")
        if not classic:
            user += (" The singer is in the middle of what happens, doing things and saying things, never watching it from across "
                     "a street or a room; tell what happened, not what the place looked like.")
        if lyric_block:
            user += nl + lyric_block
        rap = bool(RAP_GENRE.search(style or ""))
        notes = craft_block(style, f"{brief.get('theme_name', '')} {brief.get('theme_id', '')} {idea_seed} {topic_text[:300]}", desc)
        if rap:
            user = user.replace("Structure: [Intro] [Verse 1] [Chorus] [Verse 2] [Chorus] [Bridge] [Chorus] [Outro], about 3 minutes",
                                "Structure: [Intro] [Verse 1] [Hook] [Verse 2] [Hook] [Bridge] [Hook] [Outro], about 3 minutes; verses 12 to 16 bars")
        system = lyric_system_for(style) + ("\n\n" + notes if notes else "")
        if not rap:
            voice = voice_block()
            if voice:
                system += "\n\n" + voice
        lyrics = clean_lyrics(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=2500, temperature=0.7 if not rap else 0.8, timeout=150, max_models=2))
    title = title_for(lyrics if not station.get("instrumental") else topic_text, fallback=strip_cjk(topic_text)[:40] or "Untitled")
    return {"topic": topic_text.strip(), "lyrics": lyrics, "style": style, "title": title}


def write_around_chorus(chorus: list[str], style: str = "", brief: dict[str, Any] | None = None,
                        station_desc: str = "", lyric_block: str = "") -> str:
    """An interpolation: a real song's chorus stays word for word and new verses and a bridge are written around it.
    `brief` carries a channel's rules (content rules, mood, perspective, explicit), the same ones its own songs get;
    without a channel it carries the plain defaults. The caller puts the chorus back word for word afterwards."""
    brief = brief or {}
    nl = "\n"
    rap = bool(RAP_GENRE.search(style or ""))
    tag = "Hook" if rap else "Chorus"
    genre = (style.split(",")[1].strip() if style and "," in style else style) or ""
    user = (f"Here is the {tag.lower()} of a real song. It stays EXACTLY as it is, word for word, every time it comes back:{nl}{nl}"
            f"[{tag}]{nl}{nl.join(chorus)}{nl}{nl}"
            f"Write brand new verses and a bridge around it in your own words, so the whole thing reads as one song: the verses "
            f"tell what happened that makes this {tag.lower()} true. Do not quote or paraphrase any other line of the original "
            f"song, and do not name the song or who sang it.{nl}")
    if rap:
        user += RAP_PERSONA + nl
        if brief.get("explicit"):
            user += RAP_EXPLICIT + nl
    if brief.get("mood"):
        user += brief["mood"] + nl
    if brief.get("perspective"):
        user += brief["perspective"] + nl
    if station_desc:
        user += f"Sound of the station (do not use these words literally): {station_desc}{nl}"
    if genre:
        user += f"Genre it will be sung in: {genre}{nl}"
    structure = ("[Intro] [Verse 1] [Hook] [Verse 2] [Hook] [Bridge] [Hook] [Outro]; verses 12 to 16 bars" if rap else
                 "[Intro] [Verse 1] [Chorus] [Verse 2] [Chorus] [Bridge] [Chorus] [Outro]")
    user += f"Structure: {structure}, about 3 minutes. Write the {tag.lower()} out in full every time it comes back.{nl}"
    if brief.get("rules"):
        user += brief["rules"] + nl
    user += ("Every line is a complete natural phrase a person would say out loud. Never end a line on a word just to rhyme; "
             "if the rhyme does not come naturally, do not rhyme. The singer lives the story: never mention the song, the beat, "
             "writing lyrics, the studio, instruments or tempo. The first line of Verse 1 is an action or an object, never the "
             "sun, the sky, the weather or the time of day.")
    if lyric_block:
        user += nl + lyric_block
    notes = craft_block(style, " ".join(chorus), station_desc)
    system = lyric_system_for(style) + ("\n\n" + notes if notes else "")
    if not rap:
        voice = voice_block()
        if voice:
            system += "\n\n" + voice
    return clean_lyrics(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              max_tokens=2500, temperature=0.8 if rap else 0.75, timeout=150, max_models=2))


def write_to_sheet(budget_text: str, style: str = "", brief: dict[str, Any] | None = None, station_desc: str = "",
                   lyric_block: str = "", topic: str = "", original_words: str = "") -> str:
    """New words to a melody that already exists (1.6). The sheet fixes the sections and how many syllables each line
    carries; the writer gets that budget and must land on it, line for line. `brief` carries a channel's rules the way
    its own songs get them; `original_words` (when the tune was sung before) show how the lines fall and are not to be
    reused. The caller checks the syllable fit afterwards and sends the lines that miss back."""
    brief = brief or {}
    nl = "\n"
    rap = bool(RAP_GENRE.search(style or ""))
    genre = (style.split(",")[1].strip() if style and "," in style else style) or ""
    user = ("Write NEW lyrics to a melody that already exists. The tune is fixed, so the words have to fit it. These are its "
            "sections in order and, for every line, the number of syllables that line must carry:" + nl + nl + budget_text + nl + nl +
            "Keep every section tag exactly as written above and in that order, write exactly that many lines under each, and give "
            "each line exactly that many syllables (count them; one more or one fewer is the most it can be off). A section marked "
            "'no singing' gets its tag and nothing under it. Repeat a chorus word for word every time it comes back only if its "
            "lines have the same counts; otherwise write it to the counts given." + nl)
    if topic:
        user += f"What the song is about: {topic}{nl}"
    else:
        user += "What it is about is yours to choose: something that happens to one person, told plainly." + nl
    if original_words.strip():
        user += ("These are the words the tune was sung with before. They are here ONLY so you can hear where the lines fall "
                 "and where the stresses sit; this is a new song on the old tune, so use none of their lines, images, names or "
                 "subject:" + nl + original_words.strip() + nl)
    if rap:
        user += RAP_PERSONA + nl
        if brief.get("explicit"):
            user += RAP_EXPLICIT + nl
    if brief.get("mood"):
        user += brief["mood"] + nl
    if brief.get("perspective"):
        user += brief["perspective"] + nl
    if station_desc:
        user += f"Sound of the station (do not use these words literally): {station_desc}{nl}"
    if genre:
        user += f"Genre it will be sung in: {genre}{nl}"
    if brief.get("rules"):
        user += brief["rules"] + nl
    user += ("Every line is a complete natural phrase a person would say out loud. Never end a line on a word just to rhyme; "
             "if the rhyme does not come naturally, do not rhyme. The singer lives the story: never mention the song, the tune, "
             "the melody, writing lyrics, the studio, instruments or tempo. Output only the tagged lyrics.")
    if lyric_block:
        user += nl + lyric_block
    notes = craft_block(style, topic or budget_text[:200], station_desc)
    system = lyric_system_for(style) + ("\n\n" + notes if notes else "")
    if not rap:
        voice = voice_block()
        if voice:
            system += "\n\n" + voice
    return clean_lyrics(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              max_tokens=2500, temperature=0.8 if rap else 0.75, timeout=150, max_models=2))


def rewrite_lines(lyrics: str, instruction: str, style: str = "") -> str:
    """One pass over lyrics that lean on words this channel has worn out. The instruction names exactly what to
    change and says everything else stays word for word; if the answer comes back a different shape, the original
    is kept. Called at most once per song, and never when the channel is running low."""
    if not (instruction or "").strip() or not (lyrics or "").strip():
        return lyrics
    system = lyric_system_for(style)
    user = (f"{instruction}\n\nHere are the lyrics. Return the WHOLE song with the same section tags and the same "
            f"number of lines, changing only what was asked for.\n\n{lyrics}")
    try:
        out = clean_lyrics(_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                 max_tokens=2500, temperature=0.75, timeout=120, max_models=2))
    except Exception as e:
        LOG.warning("lyric rewrite failed (%s); keeping the first draft", e)
        return lyrics
    a = len([l for l in lyrics.splitlines() if l.strip()])
    b = len([l for l in out.splitlines() if l.strip()])
    if not out or b < a * 0.7 or b > a * 1.3:
        LOG.info("lyric rewrite came back a different shape (%d lines for %d); keeping the first draft", b, a)
        return lyrics
    return out


def quick_ok(timeout: int = 15, cold_timeout: int = 150) -> bool:
    """True if the preferred model answers a one-token prompt quickly (used before unattended radio planning).
    Skips the probe when the model answered within the last 3 minutes; waits for any in-flight request first."""
    global _last_ok, _primary_down_until
    if time.time() - _last_ok < 180:
        return True
    body = {"model": MODELS[0], "prompt": "Say OK", "stream": False, "keep_alive": KEEP_ALIVE, "options": {"num_predict": 4, "num_ctx": NUM_CTX}}
    if time.time() < _primary_down_until:
        return _fallback_ok(cold_timeout)
    if "gpt-oss" in MODELS[0].lower():
        body["think"] = "low"
    with _LOCK:
        if time.time() - _last_ok < 180:
            return True
        for attempt, t in enumerate((timeout, cold_timeout)):
            req = urllib.request.Request(_native_base() + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=t) as r:
                    ok = bool(json.loads(r.read()).get("done"))
                if ok:
                    _last_ok = time.time()
                    return True
            except Exception as e:
                if attempt == 0:
                    LOG.warning("LLM quick check slow (%s); the model is probably cold, waiting up to %ds for it to load", e, cold_timeout)
                else:
                    LOG.warning("LLM quick check failed: %s", e)
        _primary_down_until = time.time() + 600
        LOG.warning("LLM: primary brain marked down for 10 min; songs will use the fallback brain")
        return _fallback_ok(cold_timeout)


def _fallback_ok(timeout: int) -> bool:
    """Probe the fallback brain (called with _LOCK held or from quick_ok's locked section)."""
    global _last_ok
    if not (FALLBACK_URL and FALLBACK_MODEL):
        return False
    # no generation, no options: asking the shared box to generate is what could make it load or resize a model
    if _fallback_resident() is not None:
        LOG.warning("LLM quick check: fallback %s is loaded and ready on %s", FALLBACK_MODEL, _fallback_base())
        _last_ok = time.time()
        return True
    LOG.warning("LLM fallback quick check: %s is not loaded on %s, so it is not used", FALLBACK_MODEL, _fallback_base())
    return False


def unload_model() -> bool:
    """Ask Ollama to drop the lyric model from VRAM now (power OFF, or before a tight render)."""
    body = {"model": MODELS[0], "keep_alive": 0}
    req = urllib.request.Request(_native_base() + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as e:
        LOG.warning("LLM unload failed: %s", e)
        return False


def loaded_models() -> list[str]:
    try:
        with urllib.request.urlopen(_native_base() + "/api/ps", timeout=5) as r:
            return [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


def craft_summary() -> dict[str, Any]:
    if craft is None:
        return {"installed": False}
    try:
        return {"installed": craft.available(), **craft.summary()}
    except Exception:
        return {"installed": False}


def health() -> dict[str, Any]:
    out: dict[str, Any]
    try:
        with urllib.request.urlopen(BASE_URL.rstrip("/") + "/models", timeout=5) as r:
            data = json.loads(r.read())
        ids = [m.get("id") for m in data.get("data", [])]
        out = {"ok": True, "models": ids, "preferred": next((m for m in MODELS if m in ids), None)}
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    try:
        with urllib.request.urlopen(_native_base() + "/api/ps", timeout=5) as r:
            out["loaded"] = [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception:
        out["loaded"] = None
    out["fallback"] = {"url": FALLBACK_URL, "model": FALLBACK_MODEL}
    out["last_ok_s_ago"] = round(time.time() - _last_ok) if _last_ok else None
    out["primary_cooldown_s"] = max(0, round(_primary_down_until - time.time()))
    return out
