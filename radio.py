"""radio.py — stations, lyric sources and keep-ahead generation for Ten Forward's radio mode.

A station is a SOUND (style prompts) plus a set of allowed THEMES (themes.py decides what songs are about).
When the radio plays a station it serves songs already made for it (unplayed first, then the least recently
played, never one of the last few) and, when auto-generate is on, keeps `keep_ahead` unplayed songs ready by
queueing generation jobs, so the next song is being written and rendered while the current one plays.

Lyrics for new songs come from the station's own lyrics folder (lyrics/<folder>, .txt or .md,
each used once, anywhere) or are invented by the LLM from a theme + singer perspective + content rules
(no politics, no sports) and saved back into that folder. The owner's lyric notes (personal.json) feed ONLY "My Songs".
Instrumental stations get a freshly mutated style line every song (themes.mutate_instrumental_style).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Any

import themes
from db import DB, new_id

LOG = logging.getLogger("tenforward.radio")
TF_ROOT = Path(__file__).resolve().parent
LYRICS_ROOT = TF_ROOT / "lyrics"
_VAULT = os.environ.get("TF_VAULT_DIR") or themes.personal().get("vault_dir")
AI_SONGS_VAULT = Path(_VAULT) if _VAULT else None  # the owner's folder of lyric notes (personal.json); none in a release
INST_LORA = {"ar_lora_inst_v3abc.bf16.safetensors": 1.0}  # available in Studio; not used by radio stations (see below)
SEED_VERSION = 11  # bump when DEFAULT_STATIONS changes and existing default rows should pick up the new sound/themes
# Which default stations a seed version touches. A version listed here upgrades ONLY those rows, so the owner's own edits on the
# other stations (Bank Roll's description, Sunday Porch's themes ...) survive. A version not listed upgrades every default row.
SEED_CHANGED: dict[int, set[str]] = {10: {"summer-haze", "blue-hour"}, 11: {"blue-hour"}}

DEFAULT_STATIONS: list[dict[str, Any]] = [
    {
        "id": "favorites", "name": "Favorites", "color": "red", "sort": 5, "kind": "favorites",
        "description": "Every song you put a heart on, wherever it lives. Hearting one adds it here and leaves it on its own channel; taking the heart off takes it out. This channel never writes songs of its own.",
        "style_prompts": [], "themes": [], "lyrics_folder": "favorites", "mode": 0, "duration_s": 300,
        "auto_generate": 0, "keep_ahead": 0, "lyric_policy": "files", "replay_policy": "loop", "retention_days": 0,
    },
    {
        "id": "late-shift", "name": "Late Shift", "color": "lav", "sort": 10,
        "description": "Small-town late night: gas stations, second jobs, headlights on a two lane road. Country and heartland rock, plainspoken, weary but not hopeless.",
        "style_prompts": [
            "English, country rock, warm male baritone, acoustic rhythm guitar, pedal steel, upright bass, brushed drums, laid back groove, 88 BPM",
            "English, heartland rock, gritty male vocal, electric guitar, Hammond organ, bass, steady drums, mid tempo, 104 BPM",
            "English, modern country, clear female alto, acoustic guitar, fiddle, light drums, open sound, 96 BPM",
            "English, country ballad, husky female vocal, piano, pedal steel, soft drums, slow, 72 BPM",
            "English, outlaw country, low gravel male vocal, baritone electric guitar, upright bass, snare with brushes, harmonica, sparse, 84 BPM",
            "English, red dirt country, raspy male vocal, telecaster twang, bass, driving drums, organ, 112 BPM",
            "English, americana, smoky female vocal, dobro, acoustic guitar, upright bass, tambourine, slow shuffle, 80 BPM",
        ],
        "themes": ["grind", "late-shift", "night-drive", "hometown", "breakup", "love-deep", "love-far", "waiting", "reunion", "self-made"],
        "lyrics_folder": "late-shift", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "warp-core", "name": "Warp Core", "color": "orange", "sort": 20,
        "description": "Dark synth and synthwave, instrumental only, never slow: darksynth and cyberpunk mashed with 808 bass, metal guitars, EDM drops and modern pop synths. A different mash every track.",
        "style_prompts": [
            "Instrumental, dark synthwave, aggressive analog bass synth, fast arpeggiated leads, hard gated drums",
            "Instrumental, darksynth, distorted saw leads, pounding kick, palm muted metal guitar chugs",
            "Instrumental, cyberpunk synthwave, 808 sub bass, trap hi hats, neon synth leads, big drop",
            "Instrumental, synthwave pop, bright modern pop synths, side chain bass, huge chorus drop, punchy drums",
            "Instrumental, retro electro, driving sequencer bass, laser leads, four on the floor kick, crash cymbals",
        ],
        "fusion_set": "darksynth", "variation": 1,
        "lyrics_folder": "warp-core", "mode": 0, "duration_s": 190, "auto_generate": 1, "keep_ahead": 6, "lyric_policy": "invent", "instrumental": 1,
        "loras": {},
    },
    {
        "id": "ten-forward-lounge", "name": "Lounge", "color": "teal", "sort": 30,
        "description": "After hours lounge: jazz, soul and slow funk. Smoky rooms, a piano, a drink that has been sitting too long. About half the tracks are instrumental.",
        "style_prompts": [
            "English, jazz ballad, warm female alto, piano, upright bass, brushed drums, tenor saxophone, sparse and late, 72 BPM",
            "English, neo soul, smooth male vocal, Rhodes piano, electric bass, soft drums, warm horns, 84 BPM",
            "English, slow funk, husky female vocal, wah guitar, bass, tight drums, organ, 92 BPM",
            "English, soul, rich male baritone, piano, strings, bass, soft drums, late night, 76 BPM",
            "English, lounge jazz, breathy female vocal, nylon guitar, vibraphone, upright bass, bossa nova brushes, 96 BPM",
            "English, smoky blues, low male vocal, hollow body electric guitar, organ, bass, slow drums, 66 BPM",
            "Instrumental, jazz piano trio, piano, upright bass, brushed drums, late night, 80 BPM",
            "Instrumental, soul jazz, tenor saxophone lead, Rhodes piano, electric bass, soft drums, warm, 88 BPM",
        ],
        "themes": ["love-new", "love-deep", "breakup", "reunion", "waiting", "rainy-day", "morning-after", "night-drive"],
        "fusion_set": "lounge", "variation": 1, "instrumental_chance": 0.45,
        "lyrics_folder": "lounge", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "sunday-porch", "name": "Sunday Porch", "color": "gold", "sort": 40,
        "description": "Acoustic morning music. Coffee, kids in the yard, folk and bluegrass, light and unhurried. Mostly instrumental; a song with words now and then.",
        "style_prompts": [
            "Instrumental, folk, fingerpicked acoustic guitar, mandolin, upright bass, light percussion, 100 BPM",
            "Instrumental, bluegrass, banjo rolls, fiddle, acoustic guitar, upright bass, quick and clean, 130 BPM",
            "Instrumental, acoustic americana, dobro, acoustic guitar, harmonica, light brushes, 96 BPM",
            "Instrumental, indie folk, acoustic guitar, piano, cello, sparse, 92 BPM",
            "English, folk, gentle male vocal, fingerpicked acoustic guitar, mandolin, upright bass, light percussion, 100 BPM",
            "English, bluegrass, bright female vocal, banjo, fiddle, acoustic guitar, upright bass, quick and clean, 130 BPM",
            "English, indie folk, soft female vocal, acoustic guitar, piano, cello, sparse, 92 BPM",
        ],
        "themes": ["sunday", "hometown", "love-deep", "first-place", "crew", "rainy-day", "road-trip", "summer-fun", "love-new"],
        "fusion_set": "porch", "variation": 1, "instrumental_chance": 0.8,
        "lyrics_folder": "sunday-porch", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "summer-haze", "name": "Summer Haze", "color": "peach", "sort": 45,
        "description": "Summer pop with the windows down: strummed acoustic guitar over bright pop production, now and then with a country twang or a Yellowcard-style pop punk kick (power chords and a violin), and always a big singalong chorus. Songs about the best weeks of the year: friends, dares, road trips and the one you like.",
        "style_prompts": [
            "English, summer pop, bright male vocal, strummed acoustic guitar, pop bass, clap driven pop drums, big singalong chorus, sunny, 112 BPM",
            "English, summer pop, clear female vocal, strummed acoustic guitar, pop bass, hand claps, bright pop drums, big singalong chorus, 108 BPM",
            "English, country pop, warm male vocal, strummed acoustic guitar, banjo licks, pop bass, punchy pop drums, big singalong chorus, 110 BPM",
            "English, country pop, confident female vocal, strummed acoustic guitar, telecaster twang, pop bass, punchy pop drums, big singalong chorus, 116 BPM",
            "English, pop punk, energetic male vocal, strummed acoustic guitar intro, power chords, melodic violin lead, driving drums, big singalong chorus, 148 BPM",
            "English, pop punk, bright female vocal, strummed acoustic guitar intro, power chords, melodic violin lead, driving drums, big singalong chorus, 144 BPM",
            "English, acoustic pop rock, raspy male vocal, strummed acoustic guitar, jangly electric guitar, pop bass, upbeat drums, big singalong chorus, 124 BPM",
        ],
        "themes": ["summer-love", "summer-fun", "summer-night", "summer-end", "heat-wave", "summer-job", "vacation", "summer-crush", "party", "road-trip", "love-new", "crew"],
        "mood": "carefree, loud and funny, always out doing something with friends or with the one they like, never watching from a distance and never pining; every song sounds like the best week of the year; one real detail from their life per verse, the kind you would say out loud (my cousin's jet ski we were not supposed to take, your dad's truck with no AC, the ten bucks we split on gas, the fireworks tent out by the highway, the graduation party that got shut down, the tan line from your watch)",
        "fusion_set": "summer", "variation": 1,
        "lyrics_folder": "summer-haze", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "blue-hour", "name": "Blue Hour", "color": "blue", "sort": 46,
        "description": "Moody pop for headphones after dark, in the lane of Justin Bieber, Joji, Billie Eilish, The Weeknd, Lauv, Khalid, Conan Gray, Tate McRae, Olivia Rodrigo and Post Malone: hushed close mic vocals, sub bass, sparse trap drums, lo-fi pianos and dark synths. The one who is bad for you, being fine in public, growing up too fast, the small hours.",
        "style_prompts": [
            "English, dark bedroom pop, hushed breathy female vocal, close mic whisper, sub bass, sparse trap hi hats, minimal synth, 88 BPM",
            "English, lo-fi alternative pop, soft melancholic male vocal, detuned piano, vinyl crackle, slow trap drums, deep 808, 74 BPM",
            "English, pop, smooth light male tenor, falsetto hook, acoustic guitar, tropical synth plucks, soft trap drums, 96 BPM",
            "English, alt pop, airy female vocal, plucked synth, finger snaps, sub bass, minimal beat, quiet verse loud chorus, 100 BPM",
            "English, moody electropop, breathy female alto, dark synth bass, glitchy percussion, whispered ad libs, 108 BPM",
            "English, sad indie pop, tender male vocal, clean electric guitar, soft pads, slow drums, 80 BPM",
            "English, dark pop, smooth male vocal, electric piano, 808 bass, soft trap hi hats, moody, 92 BPM",
            "English, bedroom pop, lo-fi female vocal, jangly guitar, tape hiss, laid back drums, 84 BPM",
            "English, dark pop ballad, fragile female vocal, felt piano, cinematic strings, no drums until the last chorus, 66 BPM",
            "English, melodic trap pop, autotuned male vocal, dreamy guitar loop, hard 808, rolling hi hats, 140 BPM",
            "English, alt pop, cool female vocal, muted guitar loop, sub bass, finger snaps, quiet verse big chorus, 104 BPM",
        ],
        "themes": ["toxic-love", "ghosted", "three-am", "almost", "growing-up", "fake-smile", "hoodie", "city-lonely",
                   "love-new", "breakup", "breakup-better", "waiting", "love-far", "night-drive", "morning-after"],
        "mood": "hushed and close to the mic, cool on the surface with the hurt underneath; dry humor; one real detail from their life per verse, the kind you would say out loud (a key I never gave back, your toothbrush still in my cup, your mom still asking how I am doing, the party where you left with her, the song you ruined for me); phones and texting in one song out of four at most; never dramatic, never a big word",
        "fusion_set": "bluehour", "variation": 1,
        "male_ratio": 0.65,  # 65% male vocal songs, 35% female
        "lyrics_folder": "blue-hour", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "bank-roll", "name": "Bank Roll", "color": "green", "sort": 47,
        "description": "Gangster rap only, male voices only, no R&B, explicit: street stories (the block, opps, shootings, the set, trap houses, jail, fallen homies) and drip charisma (money, cars, chains, women, wins), in the classic west coast / east coast / hardcore / boom bap / southern / horrorcore / Chicago drill / melodic trap sounds.",
        "style_prompts": [
            "English, west coast hip hop, deep confident male rapper, G funk synth lead, heavy bass, boom bap drums, talk box, 92 BPM",
            "English, hardcore east coast hip hop, aggressive raspy male rapper, hard drums, dark piano loop, scratches, 96 BPM",
            "English, boom bap hip hop, smooth storytelling male rapper, jazzy sample, dusty drum break, upright bass, 90 BPM",
            "English, gangsta rap, laid back male rapper with a growl, whistling synth, thick bass, hard snare, 94 BPM",
            "English, southern hip hop, playful raspy male rapper, autotune ad libs, 808 bass, snappy snares, half time, 76 BPM",
            "English, horrorcore hip hop, intense fast male rapper, eerie piano, heavy 808, aggressive drums, 88 BPM",
            "English, chicago drill, cold monotone male rapper, sliding 808s, rapid hi hats, dark bells, 140 BPM",
            "English, mumble rap trap, melodic autotuned male vocal, ad libs, 808s, rolling hi hats, 150 BPM",
            "English, melodic trap, confident male rapper with light autotune, 808 bass, trap hi hats, dark piano, sparse, 140 BPM",
            "English, hardcore hip hop, growling male rapper, DJ scratches, thick bass, hard drums, sirens, 100 BPM",
        ],
        "themes": ["money-up", "drip", "self-made", "party", "crew", "night-drive",
                   "the-block", "opps", "shootout", "fallen-homie", "gang", "trap-house", "locked-up", "paranoia", "my-city", "no-love"],
        "mood": "cold, confident, unbothered; menace in the gangster songs, humor and swagger in the flex songs",
        "explicit": 1,
        "lyrics_folder": "bank-roll", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "heartbreak-motel", "name": "Heartbreak Motel", "color": "salmon", "sort": 48,
        "description": "Breakup songs in every style: country, pop punk, piano ballads, indie pop, emo rock and alt country. The day it ends, the weeks after, and the night you almost call.",
        "style_prompts": [
            "English, country, aching male baritone, acoustic guitar, pedal steel, piano, slow drums, 74 BPM",
            "English, pop ballad, breathy female vocal, piano, strings, soft drums, 68 BPM",
            "English, pop punk, raw male vocal, power chords, driving bass, fast drums, 168 BPM",
            "English, indie pop, clear female vocal, electric piano, synth bass, tight drums, bittersweet, 108 BPM",
            "English, piano ballad, cracked male tenor, grand piano, cello, soft brushes, slow, 64 BPM",
            "English, alt country, tired female vocal, jangly electric guitar, pedal steel, bass, mid tempo, 96 BPM",
            "English, emo rock, strained male vocal, clean then distorted guitars, bass, dynamic drums, 150 BPM",
        ],
        "themes": ["breakup", "breakup-better", "reunion", "waiting", "love-far"],
        "mood": "honest and a little wrecked; some songs angry, some funny, some already over it; one real detail from their life per verse, the kind you would say out loud (your half of the rent still on the counter, the dog we could not split, your sister unfollowing me, the ring I am still paying off, your stuff riding in my trunk for a month)",
        "lyrics_folder": "heartbreak-motel", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "mixed",
    },
    {
        "id": "engineering", "name": "Engineering", "color": "blue", "sort": 50,
        "description": "Instrumental focus music: cinematic electronic, post rock and downtempo. No vocals. The sound shifts every track.",
        "style_prompts": [
            "Instrumental, cinematic electronic, warm pads, analog bass, slow drums, piano motif",
            "Instrumental, post rock, clean electric guitars, bass, drums, slow build",
            "Instrumental, downtempo, Rhodes, dusty drums, deep bass, vinyl texture",
            "Instrumental, ambient techno, soft kick, evolving pads, sub bass, granular textures",
        ],
        "fusion_set": "focus", "variation": 1,
        "lyrics_folder": "engineering", "mode": 0, "duration_s": 180, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "invent", "instrumental": 1,
        "loras": {},  # the AR instrumental LoRA needs the legacy LM engine: ~30 min per song, far too slow for a radio (use it in Studio)
    },
    {
        "id": "neon-static", "name": "Neon Static", "color": "pink", "sort": 52,
        "description": "Synthwave after midnight, instrumental only: the moodier, mid-tempo cousin of Warp Core. Retrowave, vaporwave, 80s horror score, trip hop and dreamwave fused into the neon.",
        "style_prompts": [
            "Instrumental, dark synthwave, analog bass synth, arpeggiated leads, gated drums, wide pads",
            "Instrumental, synthwave, pulsing bass, retro drum machine, shimmering pads, lead synth",
            "Instrumental, dreamwave, soft analog pads, slow arpeggios, reverb drums, warm bass",
            "Instrumental, vaporwave, slowed chords, tape wobble, soft drums, deep bass",
        ],
        "fusion_set": "synthwave", "variation": 1,
        "lyrics_folder": "neon-static", "mode": 0, "duration_s": 200, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "invent", "instrumental": 1,
        "loras": {},
    },
    {
        "id": "quartet-hall", "name": "Quartet Hall", "color": "gold", "sort": 54,
        "description": "String quartet, instrumental, never a word: two violins, viola and cello in a different era each piece, sometimes joined by a wordless choir (oohs and aahs) or an 808 thump underneath to bring it up to date.",
        "style_prompts": [
            "Instrumental, classical, string quartet, violin, violin, viola, cello",
            "Instrumental, chamber music, string quartet, expressive violin, warm cello",
            "Instrumental, classical strings, quartet, legato bowing, rich harmony",
            "Instrumental, neoclassical, string quartet, wordless choir oohs and aahs, no lyrics",
            "Instrumental, modern classical crossover, string quartet, 808 sub bass pulse, soft electronic kick, upbeat",
        ],
        "fusion_set": "classical", "variation": 1,
        "lyrics_folder": "quartet-hall", "mode": 0, "duration_s": 180, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "invent", "instrumental": 1,
        "loras": {},
    },
    {
        "id": "sleeping-sounds", "name": "Sleeping Sounds", "color": "lav", "sort": 58,
        "description": "Sleep station, never a word. Slow ambient pads, soft piano, rain, waves and space, tuned to 432 Hz, with the relaxation frequencies people sleep to (binaural delta and theta beats plus a soft noise bed) mixed quietly under every track. Tune in, set the sleep timer, lights out.",
        "style_prompts": [
            "Instrumental, ambient sleep music, warm soft synth pads, very slow, long sustained chords, 432 Hz tuning, no drums, no vocals",
            "Instrumental, sleep piano, soft felt piano, sparse slow notes, gentle rain behind it, no drums, no vocals",
            "Instrumental, deep space ambient drone, slowly evolving pads, sub bass hum, weightless, no drums, no vocals",
            "Instrumental, meditation music, singing bowls, soft pads, long decays, healing frequency, no drums, no vocals",
            "Instrumental, new age sleep music, ocean waves, warm synth pad, slow harp, no drums, no vocals",
            "Instrumental, lullaby ambient, music box and glass pads, very quiet, slow, no drums, no vocals",
        ],
        "fusion_set": "sleep", "variation": 1,
        "lyrics_folder": "sleeping-sounds", "mode": 0, "duration_s": 300, "auto_generate": 1, "keep_ahead": 5, "lyric_policy": "invent", "instrumental": 1,
        "retention_days": 30,
        "post_fx": {"binaural": 1, "carrier_hz": [140, 230], "beat_hz": [1.5, 6.5], "level_db": -21, "noise": "brown", "noise_db": -36},
        "loras": {},
    },
    {
        "id": "my-songs", "name": "My Songs", "color": "peach", "sort": 60,
        "description": "Your own songs: the tracks you already made (imported) and your own lyrics (a Google Keep export, a folder of lyric notes, anything dropped into lyrics/my-songs) sung fresh in country and roots styles. Never auto-deleted.",
        "style_prompts": [
            "English, country, warm male baritone, acoustic guitar, pedal steel, bass, drums, steady, 92 BPM",
            "English, outlaw country, gritty male vocal, electric guitar, bass, drums, harmonica, 100 BPM",
            "English, country rock, strong male vocal, electric guitar, acoustic guitar, bass, punchy drums, 108 BPM",
            "English, southern rock, raspy male vocal, slide guitar, organ, bass, driving drums, 116 BPM",
            "English, acoustic country, plain honest male vocal, acoustic guitar, dobro, light percussion, 88 BPM",
        ],
        "lyrics_folder": "my-songs", "mode": 0, "duration_s": 300, "auto_generate": 1, "lyric_policy": "files", "replay_policy": "loop", "keep_ahead": 3, "retention_days": 0,
    },
]

MY_SONGS_STATION = "my-songs"  # the private channel: off when the personal feature is off (the release build)
FAVORITES_STATION = "favorites"  # the hearted-songs channel: a view over songs, not a place they live
FAVORITES_KIND = "favorites"
# what a Favorites channel is always set to, whoever saves it and from whichever screen
FAVORITES_LOCK: dict[str, Any] = {
    "kind": FAVORITES_KIND, "auto_generate": 0, "keep_ahead": 0, "style_prompts": [], "themes": [],
    "banned_topics": [], "instrumental": 0, "instrumental_chance": 0, "replay_policy": "loop",
    "retention_days": 0, "lyric_policy": "files", "mood": None, "fusion_set": None, "voice_id": None, "cover_chance": 0,
}


def is_favorites(st: dict[str, Any] | None) -> bool:
    """A channel that holds hearted songs instead of its own. Falls back to the id so a row written before the
    kind column existed still counts."""
    if not st:
        return False
    return st.get("kind") == FAVORITES_KIND or st.get("id") == FAVORITES_STATION


def station_where(db: DB, station_id: str | None) -> tuple[str, list[Any]]:
    """The SQL for 'this song is on that channel'. Favorites asks about the heart instead, because a hearted song
    stays on its own channel and is only shown here as well."""
    if not station_id or station_id == "all":
        return "", []
    if is_favorites(db.get("stations", station_id)):
        return "liked=1", []
    return "station_id=?", [station_id]


STARTER_FILE = Path(__file__).resolve().with_name("starter_channels.json")


def _starter() -> dict[str, Any] | None:
    """starter_channels.json next to this file (the release build ships one): {"keep": [stock ids], "add": [full channel
    definitions]}. None when there is no such file, which is every install that is not a release."""
    if not STARTER_FILE.exists():
        return None
    try:
        data = json.loads(STARTER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        LOG.warning("radio: could not read %s; every stock channel is a default", STARTER_FILE, exc_info=True)
        return None


def default_stations() -> list[dict[str, Any]]:
    """The channels a fresh install gets. My Songs holds your own finished songs and your own lyric files, so the
    release build (TF_PERSONAL=0) leaves it out. A starter file narrows the stock list and may add channels of its
    own; it never removes a channel that is already in the database (seeding only inserts)."""
    try:
        import config
        personal = bool(config.cfg("personal_enabled"))
    except Exception:
        personal = True
    base: list[dict[str, Any]] = DEFAULT_STATIONS
    starter = _starter()
    if starter is not None:
        keep = set(starter.get("keep") or [])
        base = [st for st in DEFAULT_STATIONS if st["id"] in keep]
        base += [dict(st) for st in (starter.get("add") or []) if isinstance(st, dict) and st.get("id") and st.get("lyrics_folder")]
    return [st for st in base if personal or st["id"] != MY_SONGS_STATION]


# fields a seed upgrade may overwrite on a default station (user knobs like keep_ahead / voice / enabled are kept)
SEED_FIELDS = ("name", "description", "style_prompts", "keep_ahead", "themes", "fusion_set", "variation", "instrumental", "instrumental_chance", "loras", "lyric_policy", "duration_s", "color", "sort", "replay_policy", "lyrics_folder", "retention_days", "mood", "explicit", "post_fx", "male_ratio", "place_chance", "kind")


def ensure_folders():
    LYRICS_ROOT.mkdir(parents=True, exist_ok=True)
    for st in default_stations():
        if is_favorites(st):
            continue  # nothing is ever written for Favorites, so it gets no lyrics folder
        (LYRICS_ROOT / st["lyrics_folder"]).mkdir(parents=True, exist_ok=True)


def seed_stations(db: DB):
    ensure_folders()
    version = int(db.setting("stations_seed_version", 1) or 1)
    for st in default_stations():
        row = db.get("stations", st["id"])
        if row is None:
            row = dict(st)
            row.setdefault("loras", {})
            row.setdefault("voice_id", None)
            row.setdefault("variation", 1)
            row["created"] = time.time()
            row["enabled"] = 1
            db.insert("stations", row)
            LOG.info("radio: seeded station %s", st["id"])
        elif version < SEED_VERSION and _seed_touches(st["id"], version):
            upd = {k: st.get(k) for k in SEED_FIELDS if k in st}
            db.update("stations", st["id"], **upd)
            LOG.info("radio: upgraded station %s to seed v%d", st["id"], SEED_VERSION)
    if version < SEED_VERSION:
        db.set_setting("stations_seed_version", SEED_VERSION)
    apply_personal_flag(db)


def apply_personal_flag(db: DB) -> None:
    """My Songs holds your own finished songs and your own lyric files. With the personal feature off it is hidden,
    never deleted: switch the feature back on and the channel comes back with everything in it."""
    row = db.get("stations", MY_SONGS_STATION)
    if not row:
        return
    try:
        import config
        on = bool(config.cfg("personal_enabled"))
    except Exception:
        on = True
    if not on and row.get("enabled"):
        db.update("stations", MY_SONGS_STATION, enabled=0)
        db.set_setting("my_songs_auto_disabled", True)
        LOG.info("radio: My Songs is hidden (the personal feature is off)")
    elif on and not row.get("enabled") and db.setting("my_songs_auto_disabled"):
        db.update("stations", MY_SONGS_STATION, enabled=1)
        db.set_setting("my_songs_auto_disabled", False)
        LOG.info("radio: My Songs is back")


def _seed_touches(station_id: str, from_version: int) -> bool:
    """True when any seed version after `from_version` changed this station (or is not listed in SEED_CHANGED, meaning all)."""
    for v in range(from_version + 1, SEED_VERSION + 1):
        changed = SEED_CHANGED.get(v)
        if changed is None or station_id in changed:
            return True
    return False


def relocate_vault_songs(db: DB) -> int:
    """One-time fix: songs made from the vault while another station's LLM was down belong to My Songs."""
    if db.setting("vault_relocated_v1"):
        return 0
    moved = 0
    for job in db.query("jobs", "type='song' AND status='done'"):
        p = job.get("params") or {}
        path = str(p.get("lyrics_path") or "")
        if "imported-ai-songs" in path.replace("/", "\\") and p.get("station_id") != "my-songs" and job.get("song_id"):
            song = db.get("songs", job["song_id"])
            if song and song.get("station_id") != "my-songs":
                db.update("songs", song["id"], station_id="my-songs")
                moved += 1
    db.set_setting("vault_relocated_v1", time.time())
    if moved:
        LOG.info("radio: moved %d vault-lyric songs to My Songs", moved)
    return moved


MY_SONGS_FOLDER = "my-songs"


def migrate_my_songs_folder() -> int:
    """One-time: lyrics/imported-ai-songs -> lyrics/my-songs (same content hashes, so 'used' marks survive)."""
    old = LYRICS_ROOT / "imported-ai-songs"
    new = LYRICS_ROOT / MY_SONGS_FOLDER
    if not old.exists():
        return 0
    new.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in list(old.iterdir()):
        if not src.is_file():
            continue
        dst = new / src.name
        if dst.exists():
            if dst.read_bytes() == src.read_bytes():
                src.unlink()
            continue
        shutil.move(str(src), str(dst))
        moved += 1
    try:
        old.rmdir()
    except OSError:
        pass
    if moved:
        LOG.info("radio: moved %d lyric files from imported-ai-songs to %s", moved, MY_SONGS_FOLDER)
    return moved


def import_vault(db: DB) -> int:
    """Copy the owner's lyric notes (.md files in personal.json's vault_dir) into lyrics/my-songs (once per file)."""
    target = LYRICS_ROOT / MY_SONGS_FOLDER
    target.mkdir(parents=True, exist_ok=True)
    if not AI_SONGS_VAULT or not AI_SONGS_VAULT.exists():
        return 0
    copied = 0
    for src in sorted(AI_SONGS_VAULT.glob("*.md")):
        dst = target / src.name
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
        except Exception as e:
            LOG.warning("import %s failed: %s", src, e)
    return copied


def read_lyrics_file(path: Path) -> tuple[str, str]:
    """Returns (title, lyrics) from a .txt/.md file; a leading '# Title' line becomes the title."""
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    title = path.stem
    lines = text.splitlines()
    if lines and lines[0].startswith("#"):
        title = lines[0].lstrip("#").strip() or title
        text = "\n".join(lines[1:]).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


def file_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def lyrics_text_hash(lyrics: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", (lyrics or "").strip().lower()).encode()).hexdigest()


def list_lyrics(folder: str) -> list[dict[str, Any]]:
    d = LYRICS_ROOT / folder
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".txt", ".md") and p.is_file():
            title, lyrics = read_lyrics_file(p)
            out.append({"file": p.name, "title": title, "chars": len(lyrics), "hash": file_hash(p)})
    return out


def pick_lyrics_file(db: DB, station: dict[str, Any]) -> dict[str, Any] | None:
    """An unused lyric file from the station's OWN folder (used = hash in lyrics_used or on any song)."""
    folder = station.get("lyrics_folder") or station["id"]
    # files already claimed by a job still in the line count as used too (three plans in one tick used to pick the same file)
    pending = {(j.get("params") or {}).get("lyrics_hash") for j in db.query("jobs", "type='song' AND status IN ('queued','running')")}
    # the same words under a different file header (the writer's meta lines) must not be sung twice either
    sung_text = {lyrics_text_hash(s.get("lyrics") or "") for s in db.query("songs", "station_id=?", (station["id"],), limit=400)}
    candidates = []
    for item in list_lyrics(folder):
        if item["hash"] in pending:
            continue
        if db.get("lyrics_used", item["hash"]) is None and db.count("songs", "lyrics_hash=?", (item["hash"],)) == 0:
            candidates.append(item)
    random.shuffle(candidates)
    for item in candidates:
        path = LYRICS_ROOT / folder / item["file"]
        title, lyrics = read_lyrics_file(path)
        if lyrics_text_hash(_strip_meta(lyrics)) in sung_text:
            db.insert("lyrics_used", {"hash": item["hash"], "station_id": station["id"], "path": str(path), "song_id": None, "used_at": time.time()})
            LOG.info("radio: %s already sung on %s under another file, marked used", path.name, station["id"])
            continue
        return {"title": title, "lyrics": lyrics, "hash": item["hash"], "path": str(path)}
    return None


def save_lyrics_file(folder: str, title: str, lyrics: str, meta: dict[str, Any] | None = None) -> Path:
    d = LYRICS_ROOT / folder
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|]+", "", title).strip().rstrip(".").strip() or "song"
    path = d / f"{safe}.txt"
    n = 2
    while path.exists():
        path = d / f"{safe} v{n}.txt"
        n += 1
    head = f"# {title}\n"
    if meta:
        head += "".join(f"<!-- {k}: {v} -->\n" for k, v in meta.items() if v)
    path.write_text(f"{head}\n{lyrics.strip()}\n", encoding="utf-8")
    return path


def _strip_meta(lyrics: str) -> str:
    return re.sub(r"^<!--.*?-->\s*$", "", lyrics, flags=re.M).strip()


def _cover_for(db: DB, station: dict[str, Any], style: str) -> dict[str, Any] | None:
    """Now and then a channel sings a real song instead of writing one. Off unless the owner switches on the
    outside lyric source AND gives the channel (or the dial) a covers share."""
    try:
        import lyric_intel
        chance = lyric_intel.cover_chance(station)
        if not lyric_intel.covers.rolled(station, chance):
            return None
        return lyric_intel.cover_plan(db, station, style, float(station.get("duration_s") or 180),
                                      writer=interpolation_writer(db, station, style))
    except Exception:
        LOG.warning("radio: could not roll a cover for %s", station.get("id"), exc_info=True)
        return None


def interpolation_writer(db: DB, station: dict[str, Any], style: str):
    """Verses around a real chorus, written under this channel's rules: its content rules (swearing, banned topics),
    its mood, its worn-out words, its streets-and-names budget, rap rules on a rap line; then the same review its
    own songs get. The review only ever sees the new lines, and the real chorus goes back in word for word."""
    def write(hook: list[str], original: list[str], track: str, artist: str) -> str | None:
        from lyric_intel import covers
        budget = people_places_budget(db, station, None, style)
        brief = {"rules": themes.content_rules(station) + " " + people_places_rule(budget),
                 "mood": themes.mood_line(station), "explicit": bool(station.get("explicit")),
                 "perspective": covers.PLAIN_PERSPECTIVE, "theme_name": track}
        prep = _prepare_guidance(db, station, brief)
        prep.update(budget, place="")
        topic = f"built around the chorus of '{track}' by {artist}"

        def draft() -> tuple[str | None, int]:
            text = covers.around_chorus(hook, original, style, brief=brief,
                                        station_desc=station.get("description") or station.get("name") or "",
                                        lyric_block=prep.get("lyric_block", ""))
            if not text:
                return None, 0
            verses = covers.without_chorus(text)
            reviewed, _ = _review_lyrics(db, station, verses, topic, prep, style)
            if reviewed.strip() != verses.strip():
                text = covers.with_chorus(text, reviewed, hook) or text
            return text, _dirt(covers.without_chorus(text), prep)

        text, left = draft()
        if text and left:
            # like the channel's own songs: still carrying banned things after its rewrites, one fresh draft, the cleaner kept
            again, left2 = draft()
            if again and left2 < left:
                LOG.info("radio: %s wrote fresh verses around '%s' (the first still had %d banned things; these %d)",
                         station["id"], track, left, left2)
                text = again
        return text
    return write


# --------------------------------------------------------------------------- new words on a real melody (1.6)
# An arrangement is a saved lead sheet (sheet.py): read once from a recording, a library song or a sheet file, kept
# with the words it was sung with. A channel with a rewrite share now and then takes one and writes its own words to
# that melody, under its own rules, to the syllable count the melody wants.
REWRITE_MEMORY = 4


def _plain_review(text: str, style: str) -> str:
    """No channel behind the words: the radio-wide worn-out words, tired rhymes and made-up streets still go."""
    import llm
    from lyric_intel import novelty
    worn = novelty.worn_found(text)
    pp = novelty.people_places(text, True, True, "")
    rhymes = novelty.cliche_rhymes(text)
    if not (worn or novelty.people_places_flat(pp) or rhymes):
        return text
    fix = novelty.focused_instruction(worn, pp)
    if rhymes:
        fix = ((fix[:-1] + ". Also, ") if fix else "Change ONLY the lines that contain the things listed below; every other "
               "line stays exactly as it is. ") + "these rhymes are worn out, so end those lines differently: " + ", ".join(rhymes) + "."
    return llm.rewrite_lines(text, fix, style)


def write_words(db: DB, station: dict[str, Any] | None, style: str, arrangement: dict[str, Any], topic: str = "",
                tries: int = 2) -> tuple[str | None, dict[str, Any] | None]:
    """New lyrics to an arrangement's melody. With a channel: its content rules, mood, worn-out words, streets-and-names
    budget, rap rules, and the review its own songs get. Without one: the plain rules. Then the syllable fit is
    checked line by line against the budget (the original words' counts when the tune was sung before, the melody's
    note counts otherwise) and the lines that miss are rewritten once; a draft that is still far off gets one fresh
    try, the closer kept. Returns (lyrics, fit) or (None, None)."""
    import llm
    import sheet
    from lyric_intel import covers, features
    info = arrangement.get("info") or {}
    rows = info.get("budget") or []
    if not any(b.get("lines") for b in rows):
        return None, None
    budget_text = sheet.budget_text_of(rows)
    original = arrangement.get("words") or ""

    # The writer never sees the original words: shown them "so you can hear where the stresses fall", qwen2.5
    # handed back the same song with the pronouns swapped (She left her coat -> He left his watch). The budget
    # carries the line lengths; the words it must not copy are checked for afterwards, pronoun swaps included.
    def _norm(line: str) -> str:
        return re.sub(r"\b(?:he|she|his|her|hers|him|they|them|their|i|i'm|me|my|you|your|we|our|us)\b", "x", covers._plain(line))

    orig_norm = [n for n in (_norm(l) for l in features.lyric_lines(original)) if n]

    def copied_lines(text: str) -> list[str]:
        out = []
        for l in features.lyric_lines(text):
            n = _norm(l)
            if not n:
                continue
            if n in orig_norm:
                out.append(l)
                continue
            words = set(n.split())
            if len(words) >= 4 and any(len(words & set(o.split())) / len(words) >= 0.7 for o in orig_norm):
                out.append(l)
        return out
    if station:
        pp = people_places_budget(db, station, None, style)
        brief = {"rules": themes.content_rules(station) + " " + people_places_rule(pp), "mood": themes.mood_line(station),
                 "explicit": bool(station.get("explicit")), "perspective": "First person throughout, one singer, one story.",
                 "theme_name": arrangement.get("name") or ""}
        prep = _prepare_guidance(db, station, brief)
        prep.update(pp, place="")
        desc = station.get("description") or station.get("name") or ""
        lyric_block = prep.get("lyric_block", "")
    else:
        brief = {"rules": themes.content_rules({}), "perspective": "First person throughout, one singer, one story."}
        prep, desc, lyric_block = {}, "", ""
    what = topic or ""

    def miss(f: dict[str, Any]) -> int:
        return int(f.get("off", 0)) + int(f.get("missing", 0)) + int(f.get("extra", 0)) + 2 * int(f.get("copied", 0))

    def draft() -> tuple[str | None, dict[str, Any] | None]:
        text = llm.write_to_sheet(budget_text, style, brief, desc, lyric_block, what, "")
        if not text or len(features.lyric_lines(text)) < 4:
            return None, None
        if station:
            text, _ = _review_lyrics(db, station, text, what or f"new words to the tune of {arrangement.get('name')}", prep, style)
        else:
            text = _plain_review(text, style)
        copied = copied_lines(text)
        if copied:
            LOG.info("radio: %d line(s) on '%s' were the old song's; asking for new ones", len(copied), arrangement.get("name"))
            again = llm.rewrite_lines(text, "These lines are taken from a song that already exists; replace each with a new line of the "
                                      "same length (same number of syllables) that says something else, about something else; every "
                                      "other line stays exactly as it is:\n" + "\n".join(copied[:10]), style)
            if len(copied_lines(again)) < len(copied):
                text = again
        fit = sheet.check_fit(text, rows)
        if fit.get("instruction"):
            better = llm.rewrite_lines(text, fit["instruction"], style)
            fit2 = sheet.check_fit(better, rows)
            if miss(fit2) < miss(fit):
                text, fit = better, fit2
        fit["copied"] = len(copied_lines(text))
        return text, fit

    text, fit = draft()
    if text and fit and miss(fit) > 2 and tries > 1:
        again, fit2 = draft()
        if again and fit2 and miss(fit2) < miss(fit):
            LOG.info("radio: a fresh draft sat better on '%s' (%d lines off, was %d)", arrangement.get("name"), miss(fit2), miss(fit))
            text, fit = again, fit2
    return text, fit


def rewrite_params(station: dict[str, Any], arrangement: dict[str, Any], title: str, lyrics: str, style: str,
                   mode: int | None = None, fit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Job params for new words on a saved melody: the sheet goes with the job (`abc_text`), so nothing is
    transcribed again. Direct planning cannot take a sheet, so it becomes melody and chords."""
    mode = int(station.get("mode", 0)) if mode is None else int(mode)
    if mode not in (0, 1):
        mode = 0
    abc = arrangement.get("abc_melody") if mode == 1 and arrangement.get("abc_melody") else arrangement.get("abc")
    length = float(arrangement.get("duration_s") or 0) or float(station.get("duration_s") or 180)
    return {
        "title": title, "lyrics": lyrics, "style": style, "mode": mode,
        "duration": int(min(ENGINE_MAX_S, max(60, length * 1.3 + 15))), "target_duration": int(length),
        "steps": station_steps(station), "seed": -1, "cfg": 1.0,
        "voice_id": station.get("voice_id"), "loras": station.get("loras") or {}, "station_id": station.get("id"),
        "source": "radio" if station.get("id") else "studio", "topic": f"new words to the tune of {arrangement.get('name')}", "theme": None, "singer": None,
        "lyrics_hash": lyrics_text_hash(lyrics), "lyrics_path": None, "instrumental": False,
        "tags": ["rewrite"], "place": None, "abc_text": abc,
        "cover_of": {"kind": "rewrite", "track": arrangement.get("name") or "", "artist": "", "arrangement": arrangement.get("id")},
        "fit": {k: fit[k] for k in ("off", "missing", "extra") if fit and k in fit} if fit else None,
    }


def _rewrite_for(db: DB, station: dict[str, Any], style: str) -> dict[str, Any] | None:
    """Now and then a channel sings its own words to a saved melody: one of the arrangements the owner put on the
    radio, the one this channel has sung least recently."""
    try:
        import llm
        chance = float(station.get("rewrite_chance") or 0)
        if chance <= 0 or random.random() >= chance:
            return None
        rows = db.query("arrangements", "on_radio=1", order="COALESCE(last_used, 0) ASC, created ASC", limit=40)
        if not rows:
            return None
        key = f"rewrites:{station['id']}"
        recent = [x for x in (db.setting(key, []) or []) if x]
        fresh = [a for a in rows if a["id"] not in recent[:REWRITE_MEMORY]] or rows
        arr = random.choice(fresh[:3])
        lyrics, fit = write_words(db, station, style, arr)
        if not lyrics:
            return None
        title = unique_title(db, station, llm.title_for(lyrics, fallback=arr.get("name") or "Untitled"), lyrics)
        db.set_setting(key, ([arr["id"]] + [x for x in recent if x != arr["id"]])[:REWRITE_MEMORY])
        db.update("arrangements", arr["id"], used=int(arr.get("used") or 0) + 1, last_used=time.time())
        LOG.info("radio: %s is singing new words to '%s' (%s): '%s'", station["id"], arr.get("name"), (fit or {}).get("off", "?"), title)
        return rewrite_params(station, arr, title, lyrics, style, fit=fit)
    except Exception:
        LOG.warning("radio: could not roll a rewrite for %s", station.get("id"), exc_info=True)
        return None


def cover_params(station: dict[str, Any], plan: dict[str, Any], style: str | None = None) -> dict[str, Any]:
    """Job params for a cover: the channel's own sound, somebody else's words, never written to a lyrics folder."""
    prompts = style_lines(station.get("style_prompts")) or ["English, pop, clear vocal, guitar, bass, drums, 100 BPM"]
    style = style or random.choice(prompts)
    return {
        "title": plan["title"], "lyrics": plan["lyrics"], "style": style, "mode": int(station.get("mode", 0)),
        "duration": int(station.get("duration_s") or 180), "target_duration": int(station.get("duration_s") or 180),
        "steps": station_steps(station), "seed": -1, "cfg": 1.0,
        "voice_id": station.get("voice_id"), "loras": station.get("loras") or {}, "station_id": station["id"],
        "source": "radio", "topic": plan.get("topic"), "theme": None, "singer": None,
        "lyrics_hash": lyrics_text_hash(plan["lyrics"]), "lyrics_path": None, "instrumental": False,
        "tags": ["cover"], "place": None, "cover_of": plan.get("cover_of"),
    }


def _prepare_guidance(db: DB, station: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    """What the channel has been leaning on lately, turned into a short block for the writer. The station's own
    mood line comes back with ONE of its example objects instead of the whole list."""
    try:
        import lyric_intel
        prep = lyric_intel.prepare(db, station, brief)
        if prep.get("mood"):
            brief["mood"] = prep["mood"]
        return prep
    except Exception:
        LOG.warning("radio: no lyric guidance for %s", station.get("id"), exc_info=True)
        return {}


def classic_writer(station: dict[str, Any]) -> bool:
    """True for a channel the owner listed as keeping the pre-1.3 idea writer (Settings: classic_writer_channels)."""
    try:
        import config
        raw = str(config.cfg("classic_writer_channels") or "")
    except Exception:
        return False
    return (station or {}).get("id") in {s.strip() for s in raw.replace(";", ",").split(",") if s.strip()}


# One suggested turn of events per song, rotated per channel like a mood's example objects. The ideas used to be
# pictures (somebody standing across the street, watching); a turn of events gives the writer something that HAPPENS,
# and rotating it means two songs in a row cannot be built on the same one.
STORY_TURNS = [
    "a dare somebody actually took", "a plan that fell apart halfway through", "the first time they did something together",
    "the last time they saw each other", "a stupid fight about something small", "a favor that cost more than it should have",
    "a secret that finally came out", "a promise somebody kept", "a promise somebody broke",
    "a car that broke down at the worst time", "somebody showing up without calling first",
    "getting caught somewhere they should not have been", "a bet somebody lost", "spending the first real paycheck",
    "leaving town, or coming back to it", "meeting somebody's family for the first time", "a night that ran all the way to sunrise",
    "sneaking out, or sneaking back in", "a detour that turned into the best part", "saying the thing out loud at last",
    "a second chance somebody asked for", "a gift that meant more than it cost", "a job lost, or a job started",
    "moving out, boxes and all", "somebody teaching somebody else how to do something", "a phone call that changed the whole night",
    "a party that got shut down", "running into somebody at the worst possible moment", "an argument in the car on the way home",
    "a birthday that did not go to plan", "doing something for the very last time", "keeping a secret for somebody",
]
STORY_TURN_MEMORY = 10


def story_turn(db: DB, station: dict[str, Any], rng: random.Random | None = None) -> str:
    """This song's turn of events: one the channel has not used in its last STORY_TURN_MEMORY songs."""
    rng = rng or random
    key = f"story_turns:{station['id']}"
    used = [u for u in (db.setting(key, []) or []) if u]
    fresh = [t for t in STORY_TURNS if t not in used] or STORY_TURNS
    pick = rng.choice(fresh)
    db.set_setting(key, ([pick] + [u for u in used if u != pick])[:STORY_TURN_MEMORY])
    return pick


# Streets and first names (1.4). Real songs name a person now and then and say "street" now and then; this radio did
# both in most songs (see lyric_intel.novelty.people_places). A song may say the street family only when the channel's
# last STREET_WINDOW songs left room for it (rap, where the street is the subject, gets one of them), and may call one
# person by name only when none of the last NAME_WINDOW did. A made-up street name is never kept.
STREET_WINDOW = 4
NAME_WINDOW = 5


def people_places_budget(db: DB, station: dict[str, Any], pending: list[dict[str, Any]] | None = None,
                         style: str = "") -> dict[str, bool]:
    """{streets_ok, names_ok} for the next song, from the words of the channel's recent ones (render line included)."""
    try:
        from lyric_intel import novelty
        import llm
        recent = [p.get("lyrics") or "" for p in (pending or []) if p.get("lyrics") and not p.get("cover_of")]
        recent += [s.get("lyrics") or "" for s in db.query("songs", "station_id=? AND COALESCE(source,'')='radio' AND cover_of IS NULL",
                                                          (station["id"],), limit=max(STREET_WINDOW, NAME_WINDOW))]
        streety = sum(1 for t in recent[:STREET_WINDOW] if novelty.street_words(t) or novelty.invented_streets(t))
        named = sum(1 for t in recent[:NAME_WINDOW] if novelty.first_names(t))
        rap = bool(llm.RAP_GENRE.search(style or ""))
        return {"streets_ok": streety <= (1 if rap else 0), "names_ok": named == 0}
    except Exception:
        LOG.warning("radio: could not measure streets and names for %s", station.get("id"), exc_info=True)
        return {"streets_ok": False, "names_ok": False}


# Violence and the ex (1.5). A channel that asks for hatred got 18 of 30 songs about a man wanting the woman who
# left him dead. Some blood is the channel; every song is a rut. A song may be violent only when fewer than two of the
# channel's last TEMPER_WINDOW songs were, and may be about a partner or an ex only when fewer than two were, unless
# the theme itself is a love theme. The anger then gets pointed somewhere else, rotated like the story turns.
TEMPER_WINDOW = 4
VIOLENT_SONG = 3          # this many blood words in one song
LOVER_SONG = 6            # this many lines about him or her
ANGER_TARGETS = [
    "the boss who cut his hours", "the landlord and the eviction notice", "the bank and the letter it sent", "his own brother",
    "the town he cannot get out of", "the job he lost", "the cop who pulled him over again", "the man who sold him the truck",
    "the church that turned its back", "the factory that closed", "himself, and the years he wasted", "the crowd at the bar that laughed",
    "the doctor who would not listen", "the father who never showed up", "the friend who talked behind his back", "the phone that never rings",
    "the neighbour and the fence", "the company that took the pension", "the traffic, the heat and the whole damn day", "the judge and the paperwork",
]
TEMPER_TARGET_MEMORY = 8


def temper_budget(db: DB, station: dict[str, Any], pending: list[dict[str, Any]] | None, style: str,
                  theme: dict[str, Any] | None) -> dict[str, bool]:
    """{violence_ok, lover_ok} for the next song, from the channel's recent ones. An explicit rap channel keeps its
    violence (it is the genre); a love theme keeps its lover (it is the theme)."""
    try:
        from lyric_intel import novelty
        import llm
        recent = [p.get("lyrics") or "" for p in (pending or []) if p.get("lyrics") and not p.get("cover_of")]
        recent += [s.get("lyrics") or "" for s in db.query("songs", "station_id=? AND COALESCE(source,'')='radio' AND cover_of IS NULL",
                                                          (station["id"],), limit=TEMPER_WINDOW)]
        recent = recent[:TEMPER_WINDOW]
        violent = sum(1 for t in recent if novelty.violence_hits(t) >= VIOLENT_SONG)
        lovers = sum(1 for t in recent if novelty.about_a_lover(t) >= LOVER_SONG)
        rap_explicit = bool(llm.RAP_GENRE.search(style or "")) and bool(station.get("explicit"))
        love_theme = (theme or {}).get("kind") in ("love", "heartbreak") or bool(re.search(r"\b(love|ex|breakup|break-up|cheat|heart)\w*", str((theme or {}).get("name") or ""), re.I))
        # the lover rule is for the "she left, so he wants her dead" rut: a channel that is not violent sings about her as it likes
        return {"violence_ok": rap_explicit or violent < 2, "lover_ok": love_theme or lovers < 2 or violent == 0}
    except Exception:
        LOG.warning("radio: could not measure the temper of %s", station.get("id"), exc_info=True)
        return {"violence_ok": True, "lover_ok": True}


def anger_target(db: DB, station: dict[str, Any], rng: random.Random | None = None) -> str:
    rng = rng or random
    key = f"anger_targets:{station['id']}"
    used = [u for u in (db.setting(key, []) or []) if u]
    fresh = [t for t in ANGER_TARGETS if t not in used] or ANGER_TARGETS
    pick = rng.choice(fresh)
    db.set_setting(key, ([pick] + [u for u in used if u != pick])[:TEMPER_TARGET_MEMORY])
    return pick


def temper_rule(budget: dict[str, bool], target: str = "") -> str:
    out = []
    if not budget.get("violence_ok", True):
        out.append("Nobody in this song is hurt, hit, cut, shot, burned, buried or killed, and nothing gets smashed: "
                   "the anger is in what he says, what he refuses to do and what he walks away from.")
    if not budget.get("lover_ok", True):
        out.append("This song is not about a girlfriend, a wife, an ex or a breakup; no she, no her. "
                   + (f"The anger is aimed at {target}." if target else "The anger is aimed at something else in his life."))
    return " ".join(out)


def _temper_lines(lyrics: str, temper: dict[str, Any], style: str) -> str:
    """A draft that was told no blood and reached for it anyway: those lines, and only those, once more."""
    try:
        from lyric_intel import novelty
        import llm
        if temper.get("violence_ok", True) or novelty.violence_hits(lyrics) < VIOLENT_SONG:
            return lyrics
        bad = [l.strip() for l in lyrics.splitlines() if not l.strip().startswith("[") and novelty.VIOLENCE.search(l)]
        fix = ("Change ONLY these lines; every other line stays exactly as it is. Each one reaches for blood (hurting, hitting, "
               "cutting, shooting, burning, killing, smashing). Keep the anger and say what he says, refuses or walks out on instead:\n"
               + "\n".join(f"- {l}" for l in bad[:12]))
        better = llm.rewrite_lines(lyrics, fix, style)
        if better.strip() != lyrics.strip() and novelty.violence_hits(better) < novelty.violence_hits(lyrics):
            LOG.info("radio: rewrote %d blood line(s) (%d -> %d blood words)", len(bad), novelty.violence_hits(lyrics), novelty.violence_hits(better))
            return better
        return lyrics
    except Exception:
        LOG.warning("radio: could not temper the lines", exc_info=True)
        return lyrics


def people_places_rule(budget: dict[str, bool]) -> str:
    """The line the writer is given for this song (it goes into the hard rules, idea and lyrics alike)."""
    out = []
    if not budget.get("streets_ok"):
        out.append("This song never says street, streets, streetlight or sidewalk.")
    out.append("Nobody in this song is called by a first name: people are you, she, he, or what they are to the singer."
               if not budget.get("names_ok") else
               "At most one person in this song may be called by a first name, and only if it matters; most songs here use none.")
    return " ".join(out)


_GLUED = re.compile(r"(?<=\bBPM)[\s,;]+(?=[A-Z])")


def style_lines(prompts: list[str] | str | None) -> list[str]:
    """A channel's style lines, one sound each. Lines that lost their line breaks on the way in ("... 88 BPM English,
    post-grunge ...") arrived as ONE line holding every sound, and every song got all of them at once - one channel
    sang eight bands in every song for a week. A line ends at its tempo, so that is where they are split back."""
    if isinstance(prompts, str):
        prompts = prompts.splitlines()
    out = []
    for p in prompts or []:
        for part in _GLUED.split(str(p)):
            part = part.strip().strip(",;").strip()
            if part:
                out.append(part)
    return out


def _style_parts(style: str) -> set[str]:
    return {p.strip().lower() for p in (style or "").split(",") if p.strip() and not re.search(r"\d{2,3}\s*bpm", p, re.I)}


def pick_style(db: DB, station: dict[str, Any], candidates: list[str], pending: list[dict[str, Any]] | None = None,
               rng: random.Random | None = None) -> str:
    """A style line the channel has not used in its last few songs, chosen at random among the rest.

    random.choice alone gave one channel the same line 4 times in 15 songs while others never came up. A line
    counts as used when all of its tags are in a recent song's style, so a line that was crossed with a fusion
    or had its singer tag added still counts."""
    rng = rng or random
    if len(candidates) < 3:
        return rng.choice(candidates)
    recent = [p.get("style") for p in (pending or []) if p.get("style")] + db.recent_values("style", station["id"], 8)
    window = [_style_parts(r) for r in recent[:min(len(candidates) // 2, 4)]]
    fresh = [c for c in candidates if not any(_style_parts(c) <= w for w in window)]
    return rng.choice(fresh or candidates)


_LEAD_PRONOUN = re.compile(r"^(she's|he's|i'm|you're|we're|they're|it's|she|he|i|you|we|they)\s+(.+)$", re.I)
_PRONOUN_WORDS = ["She", "She's", "He", "He's", "I", "I'm", "You", "You're", "We", "They"]


def _first_word(t: str) -> str:
    return ((t or "").split() or [""])[0].lower().strip("'’")


def _title_shape(db: DB, station: dict[str, Any], title: str, lyrics: str, pending_titles: list[str] | None = None) -> str:
    """Titles that do not all start the same way. One channel had 7 of 15 starting with "She's".

    A title that opens on a pronoun, or on the same word as two of the channel's last eight titles, is asked for
    once more with those words ruled out. If the writer still opens on a contraction, it is dropped ("She's Not
    Coming Back" -> "Not Coming Back"); a bare pronoun is left alone, since "Smiles I Smile" is worse than the original."""
    try:
        import llm
        recent = [t for t in (pending_titles or []) if t] + [s["title"] for s in db.query("songs", "station_id=?", (station["id"],), limit=8) if s.get("title")]
        firsts: dict[str, int] = {}
        for t in recent[:8]:
            firsts[_first_word(t)] = firsts.get(_first_word(t), 0) + 1
        worn = sorted(w for w, n in firsts.items() if n >= 2 and w)
        first = _first_word(title)
        pronoun = bool(_LEAD_PRONOUN.match((title or "").strip()))
        out = title
        if pronoun or first in worn:
            ruled_out = _PRONOUN_WORDS + [w for w in worn if w not in {p.lower() for p in _PRONOUN_WORDS}]
            again = llm.title_for(lyrics, fallback="", avoid=recent[:8], not_starting=ruled_out) or ""
            if again and not _LEAD_PRONOUN.match(again.strip()) and _first_word(again) not in worn:
                out = again
        m = _LEAD_PRONOUN.match(out.strip())
        if m and "'" in m.group(1) and len(m.group(2).split()) >= 2:
            out = m.group(2).strip()[:1].upper() + m.group(2).strip()[1:]
        if out != title:
            LOG.info("radio: %s title %r -> %r", station["id"], title, out)
        return out
    except Exception:
        LOG.warning("radio: could not reshape the title %r", title, exc_info=True)
        return title


def _scene_check(db: DB, station: dict[str, Any], prep: dict[str, Any], classic: bool = False, place: str = "",
                 names_ok: bool = True, temper: dict[str, bool] | None = None):
    """Hands the invented scene to the memory once, which may ask for a different one. Since 1.3 an idea that is a
    still picture (somebody standing across the street, watching) or that invents a street name is sent back first;
    since 1.4 so is one that names its people when this song has no room for a name (the lyrics follow the idea)."""
    def check(topic: str) -> str:
        try:
            from lyric_intel import novelty
            t = temper or {}
            if not t.get("violence_ok", True) and novelty.violence_hits(topic) >= 2:
                LOG.info("radio: %s idea reached for blood again; asking for anger without it", station["id"])
                return ("Same anger, no blood: nobody is hurt, hit, cut, shot, burned or killed and nothing is smashed in this "
                        "idea. What does he say, refuse, or walk out on instead?")
            if not t.get("lover_ok", True) and novelty.about_a_lover(topic) >= 2:
                LOG.info("radio: %s idea was about her again; asking for a different target", station["id"])
                return ("Not about a woman, a girlfriend, a wife or an ex this time, and no she or her in it at all. "
                        + (f"Aim it at {t.get('target')}." if t.get("target") else "Aim it at something else in his life."))
            if not classic:
                pic = novelty.staging_found(topic)
                streets = novelty.invented_streets(topic, place)
                names = [] if names_ok else novelty.first_names(topic, place)
                if novelty.is_picture(pic) or streets:
                    LOG.info("radio: %s idea was a picture (%s)%s; asking for a story", station["id"], ", ".join(pic),
                             f", made-up streets {streets}" if streets else "")
                    return ("That idea is a still picture, not a story" + (f" ({', '.join(pic)})" if pic else "") +
                            (f", and it makes up a street name ({', '.join(streets)}); never do that" if streets else "") +
                            ". Write what HAPPENS instead: people doing something with each other or to each other, "
                            "something said, a choice, and how it turned out. Nobody stands, sits, leans or watches from "
                            "across a street or a room, and nothing glows." +
                            (" Nobody in it has a first name." if names else ""))
                if names:
                    LOG.info("radio: %s idea named people (%s); asking again without names", station["id"], ", ".join(names))
                    return ("Tell the same kind of story without first names (" + ", ".join(names) + "): people are he, "
                            "she, or what they are to each other.")
            import lyric_intel
            return lyric_intel.idea_again(db, station, topic, prep)
        except Exception:
            return ""
    return check


def _idea_score(place: str = "", names_ok: bool = True, temper: dict[str, bool] | None = None):
    """How much of a still picture an idea is (0 = a story). Lets the writer keep the best of its tries."""
    t = temper or {}

    def score(topic: str) -> int:
        try:
            from lyric_intel import novelty
            pic = novelty.staging_found(topic)
            return (len(pic) + 2 * len({"watching", "staring"} & set(pic))
                    + 3 * len(novelty.invented_streets(topic, place))
                    + (0 if names_ok else 2 * len(novelty.first_names(topic, place)))
                    + (0 if t.get("violence_ok", True) else 2 * novelty.violence_hits(topic))
                    + (0 if t.get("lover_ok", True) else 2 * novelty.about_a_lover(topic)))
        except Exception:
            return 0
    return score


def _has_stock(db: DB, station: dict[str, Any]) -> bool:
    """True when the channel has songs in hand, so one extra writing pass cannot make it run dry."""
    try:
        return unplayed_count(db, station["id"], rendered_only=True) + queued_count(db, station["id"]) >= 2
    except Exception:
        return False


def _pp(text: str, prep: dict[str, Any] | None) -> list[str]:
    """Streets and names this text carries that this song has no room for (see people_places_budget)."""
    from lyric_intel import novelty
    p = prep or {}
    return novelty.people_places_flat(novelty.people_places(text, p.get("streets_ok", True), p.get("names_ok", True), p.get("place", "")))


def _dirt(text: str, prep: dict[str, Any] | None = None) -> int:
    """How many banned words, cliche rhymes, made-up streets and unwanted names a draft still carries. Cheap: no LLM."""
    try:
        from lyric_intel import novelty
        return len(novelty.worn_found(text)) + len(novelty.cliche_rhymes(text)) + len(_pp(text, prep))
    except Exception:
        return 0


def _clean_title(title: str, lyrics: str, prep: dict[str, Any] | None = None) -> str:
    """The title came from the FIRST draft. If it carries a banned word, a made-up street or a name the song may not
    have, make a new one from the lyrics as they now are; if that one does too, use the song's first sung line."""
    try:
        from lyric_intel import novelty

        def bad(t: str) -> bool:
            return bool(novelty.worn_found(t or "") or _pp(t or "", {**(prep or {}), "streets_ok": True}))
        if not bad(title):
            return title
        import llm
        fresh = llm.title_for(lyrics, fallback=title)
        if fresh and not bad(fresh):
            LOG.info("radio: retitled %r -> %r (a banned word, a street or a name in the title)", title, fresh)
            return fresh
        small = {"the", "a", "an", "on", "in", "of", "to", "and", "at", "my", "your", "our", "her", "his", "with", "for", "from", "by"}
        for line in lyrics.splitlines():
            line = line.strip()
            if line and not line.startswith("[") and not bad(line):
                words = [w.strip(",.;:!?") for w in line.split()[:6]]
                while len(words) > 2 and words[-1].lower() in small:
                    words.pop()                              # "Standing here alone on the" -> "Standing here alone"
                return llm.tidy_title(" ".join(words))
    except Exception:
        LOG.warning("radio: could not check the title %r", title, exc_info=True)
    return title


def _review_lyrics(db: DB, station: dict[str, Any], lyrics: str, topic: str, prep: dict[str, Any], style: str) -> tuple[str, float | None]:
    """Score the draft against the channel's recent songs, and at most once ask for the repeating lines back."""
    try:
        import lyric_intel
        res = lyric_intel.review(db, station, lyrics, topic, prep)
        worn = res.get("worn") or []
        cliche = res.get("cliche") or []
        pp = res.get("pp") or []
        forced = len(worn) + len(cliche) + len(pp)
        # a banned word, a cliche rhyme, a made-up street or a name is rewritten even on a thin channel: one LLM call, not a render
        if res.get("verdict") == "rewrite" and res.get("instruction") and (forced or _has_stock(db, station)):
            import llm
            from lyric_intel import novelty
            better = llm.rewrite_lines(lyrics, res["instruction"], style)
            if better and better.strip() != lyrics.strip():
                second = lyric_intel.review(db, station, better, topic, prep)
                still = second.get("worn") or []
                still_pp = second.get("pp") or []
                if (worn and still) or (pp and still_pp):
                    # it kept the word, the street or the name. One more go, about nothing else.
                    again = llm.rewrite_lines(better, novelty.focused_instruction(still, second.get("people_places") or {}), style)
                    if again and again.strip() != better.strip():
                        third = lyric_intel.review(db, station, again, topic, prep)
                        if len(third.get("worn") or []) + len(third.get("cliche") or []) + len(third.get("pp") or []) < \
                                len(still) + len(second.get("cliche") or []) + len(still_pp):
                            better, second = again, third
                            still, still_pp = third.get("worn") or [], third.get("pp") or []
                left = len(still) + len(second.get("cliche") or []) + len(still_pp)
                # judged on what forced it: the novelty score cannot see a cliche rhyme at all
                if forced and left < forced:
                    LOG.info("radio: %s reached for %s; rewrote those lines%s", station["id"],
                             ", ".join(worn + cliche + pp), f" (still: {', '.join(still + (second.get('cliche') or []) + still_pp)})" if left else "")
                    return better, second.get("score")
                if second.get("score", 0) > res.get("score", 0):
                    LOG.info("radio: %s reached for the same words (%.2f); rewrote those lines (%.2f)",
                             station["id"], res.get("score", 0), second.get("score", 0))
                    return better, second.get("score")
        return lyrics, res.get("score")
    except Exception:
        LOG.warning("radio: could not review the lyrics for %s", station.get("id"), exc_info=True)
        return lyrics, None


def plan_song_for_station(db: DB, station: dict[str, Any], *, force_theme: str | None = None, force_singer: str | None = None,
                          force_style: str | None = None, lyrics_file: str | None = None) -> dict[str, Any]:
    """Decide lyrics + style (+ theme, singer) for the next radio song. Returns job params for a 'song' job.
    force_* pin the theme / singer / style ("More like this"); lyrics_file renders one specific file from the station's folder."""
    import llm

    policy = station.get("lyric_policy") or "mixed"
    prompts = style_lines(station.get("style_prompts")) or ["English, pop, clear vocal, guitar, bass, drums, 100 BPM"]
    # songs still in the render line count as "recent" too: three plans in ten minutes used to land on the same theme
    pending_jobs = db.query("jobs", "type='song' AND status IN ('queued','running') AND params LIKE ?", (f'%"station_id": "{station["id"]}"%',))
    pending = [j.get("params") or {} for j in pending_jobs]
    style = force_style or pick_style(db, station, prompts, pending)
    classic = classic_writer(station)
    lyrics_hash = None
    source_path = None
    theme_id = None
    singer = None
    style_label = None
    novelty = None
    tags: list[str] = []
    place = None

    inst = bool(station.get("instrumental"))
    chance = float(station.get("instrumental_chance") or 0)
    if not inst and not lyrics_file and not force_theme and chance > 0 and random.random() < chance:
        inst = True  # a station that is "sometimes instrumental" rolled an instrumental track this time
    if not inst and not lyrics_file and not force_theme and not force_style:
        rewrite = _rewrite_for(db, station, style)
        if rewrite:
            return rewrite
        cover = _cover_for(db, station, style)
        if cover:
            c = cover.get("cover_of") or {}
            LOG.info("radio: %s is singing '%s' by %s (%s)", station["id"], c.get("track"), c.get("artist"), c.get("kind"))
            return cover_params(station, cover, style)
    if inst:
        if int(station.get("variation") if station.get("variation") is not None else 1):
            mut = themes.mutate_instrumental_style(station, db.recent_values("style", station["id"], 8))
            style, style_label = mut["style"], mut["label"]
        else:
            style = random.choice(prompts)
        title = themes.instrumental_title(station)
        lyrics = "[Intro]\n[Verse]\n[Chorus]\n[Verse]\n[Chorus]\n[Bridge]\n[Chorus]\n[Outro]"
        topic = style_label or "instrumental"
    else:
        picked = None
        if lyrics_file:
            fp = LYRICS_ROOT / (station.get("lyrics_folder") or station["id"]) / Path(lyrics_file).name
            if not fp.exists():
                raise RuntimeError(f"lyric file not found: {lyrics_file}")
            t, ly = read_lyrics_file(fp)
            picked = {"title": t, "lyrics": ly, "hash": file_hash(fp), "path": str(fp)}
            policy = "files"
        elif policy in ("files", "mixed") and not force_theme:
            picked = pick_lyrics_file(db, station)
            if picked is None and policy == "files":
                # folder exhausted: My Songs style stations loop what they have instead of inventing
                raise RuntimeError(f"no unused lyric files left in '{station.get('lyrics_folder')}'")
        if picked is not None and (policy == "files" or random.random() < 0.7):
            title, lyrics, lyrics_hash, source_path = picked["title"], _strip_meta(picked["lyrics"]), picked["hash"], picked["path"]
            topic = "from lyrics folder"
        else:
            ratio = station.get("male_ratio")
            singer = force_singer or _voice_singer(db, station.get("voice_id"))
            if not singer and ratio is not None and not force_style:
                # the station sets its own male/female split: roll the gender, then take a style line written for that voice
                singer = "male" if random.random() < float(ratio) else "female"
                matching = [s for s in prompts if themes.singer_from_style(s) == singer]
                if matching:
                    style = pick_style(db, station, matching, pending)
            singer = singer or themes.singer_from_style(style) or random.choice(["male", "female"])
            style = themes.force_singer(style, singer)
            if not force_style and int(station.get("variation") if station.get("variation") is not None else 1):
                crossed = themes.mutate_vocal_style(station, style, db.recent_values("style", station["id"], 8))
                if crossed.get("label"):
                    style, style_label = crossed["style"], crossed["label"]
                    tags.append(style_label)
            recent_themes = [p.get("theme") for p in pending if p.get("theme")] + db.recent_values("theme", station["id"], 6)
            theme = themes.theme_for(station, force_theme) or themes.pick_theme(station, recent_themes)
            theme_id = theme["id"]
            brief = themes.song_brief(station, theme, singer)
            if not force_theme:
                place = themes.pick_place(station, style)
                if place:
                    brief["place"] = place["prompt"]
                    tags.append(place["name"])
            recent = [p.get("title") for p in pending if p.get("title")] + [s["title"] for s in db.query("songs", "station_id=?", (station["id"],), limit=12) if s.get("title")]
            recent_ideas = [p.get("topic") for p in pending] + db.recent_values("topic", station["id"], 10)
            recent_ideas = [re.sub(r"\s*\(more like '.*?'\)\s*$", "", str(t)).strip() for t in recent_ideas
                            if t and str(t) not in ("from lyrics folder", "fallback lyrics file", "instrumental") and len(str(t)) > 40][:8]
            budget = people_places_budget(db, station, pending, style)
            temper = temper_budget(db, station, pending, style, theme)
            if not temper["lover_ok"]:
                temper["target"] = anger_target(db, station)
            brief["rules"] = (brief.get("rules") or "") + " " + people_places_rule(budget) + " " + temper_rule(temper, temper.get("target", ""))
            if not temper["violence_ok"] or not temper["lover_ok"]:
                LOG.info("radio: %s temper: %s", station["id"], temper_rule(temper, temper.get("target", "")))
            prep = _prepare_guidance(db, station, brief)
            prep.update(budget, place=brief.get("place", ""))
            turn = "" if classic else story_turn(db, station)
            check = _scene_check(db, station, prep, classic, brief.get("place", ""), budget["names_ok"], temper)
            scorer = None if classic else _idea_score(brief.get("place", ""), budget["names_ok"], temper)
            try:
                if not llm.quick_ok():
                    raise RuntimeError("LLM not answering within 15 s")
                made = llm.invent_song(station, recent, brief=brief, style=style, recent_ideas=recent_ideas,
                                       idea_block=prep.get("idea_block", ""), lyric_block=prep.get("lyric_block", ""),
                                       idea_check=check, classic=classic, story_turn=turn, idea_score=scorer)
            except Exception as e:
                # LLM slow or down: only the station's own unused lyric files are acceptable (never another station's)
                LOG.warning("radio: LLM unavailable for %s (%s); trying the station's own lyric files", station["id"], e)
                fallback = picked or pick_lyrics_file(db, station)
                if fallback is None:
                    raise RuntimeError("LLM down and no unused lyric files in this station's folder") from e
                title, lyrics, lyrics_hash, source_path = fallback["title"], _strip_meta(fallback["lyrics"]), fallback["hash"], fallback["path"]
                topic = "fallback lyrics file"
                theme_id = None
                made = None
            if made:
                title, lyrics, topic = made["title"], made["lyrics"], made["topic"]
                first_draft = lyrics
                lyrics, novelty = _review_lyrics(db, station, lyrics, topic, prep, style)
                lyrics = _temper_lines(lyrics, temper, style)
                left = _dirt(lyrics, prep)
                if left:
                    # still carrying banned words or a cliche rhyme after its rewrites: this draft is machine
                    # poetry all the way down, and a fresh one is cleaner than patching it again. Once.
                    try:
                        again = llm.invent_song(station, recent, brief=brief, style=style, recent_ideas=recent_ideas,
                                                idea_block=prep.get("idea_block", ""), lyric_block=prep.get("lyric_block", ""),
                                                idea_check=check, classic=classic, story_turn=turn, idea_score=scorer)
                        l2, n2 = _review_lyrics(db, station, again["lyrics"], again["topic"], prep, style)
                        if _dirt(l2, prep) < left:
                            LOG.info("radio: %s wrote a fresh draft (the first still had %d banned things; this one %d)",
                                     station["id"], left, _dirt(l2, prep))
                            title, lyrics, topic, novelty = again["title"], l2, again["topic"], n2
                    except Exception:
                        LOG.warning("radio: could not write a fresh draft for %s; keeping the first", station["id"], exc_info=True)
                if lyrics.strip() != first_draft.strip():
                    # the title was taken from the first draft; titles are phrases from the song, so re-take it
                    title = llm.title_for(lyrics, fallback=title) or title
                title = _clean_title(title, lyrics, prep)
                if not classic:
                    title = _title_shape(db, station, title, lyrics, [p.get("title") for p in pending if p.get("title")])
                title = unique_title(db, station, title, lyrics, [p.get("title") for p in pending if p.get("title")])
                saved = save_lyrics_file(station.get("lyrics_folder") or station["id"], title, lyrics,
                                         {"theme": theme["name"], "singer": singer, "style": style, "place": place["name"] if place else None, "idea": topic.replace("\n", " ")[:300]})
                lyrics_hash = file_hash(saved)
                source_path = str(saved)
        if lyrics_hash is None:
            lyrics_hash = lyrics_text_hash(lyrics)
    return {
        "title": title,
        "lyrics": lyrics,
        "style": style,
        "mode": int(station.get("mode", 0)),
        "duration": ask_seconds(db, station),
        "target_duration": int(station.get("duration_s") or 180),
        "steps": station_steps(station),
        "seed": -1,
        "cfg": 1.0,
        "voice_id": station.get("voice_id"),
        "loras": station.get("loras") or {},
        "station_id": station["id"],
        "source": "radio",
        "topic": topic,
        "theme": theme_id,
        "singer": singer,
        "lyrics_hash": lyrics_hash,
        "lyrics_path": source_path,
        "instrumental": inst,
        "tags": tags,
        "place": place["name"] if place else None,
        "novelty": novelty,
    }


# `duration` is a CEILING for YuE2, not a target. Measured over 196 radio songs the correlation
# between what was asked for and what came back is 0.036 - asking 240s and asking 300s both
# produced a 168s song. So the ask buys headroom against clipping and nothing else; inflating it
# only raises the worst case. What length actually follows is the lyrics, weakly: 16-23 lines
# gave 156s and 60-67 lines gave 201s, about 30% more song for four times the words.
ENGINE_MAX_S = 600          # the cap the engine itself will honour
HEADROOM = 1.35             # enough room that a song ends rather than being faded at the ceiling


def delivered_median(db: DB, station: dict[str, Any]) -> float | None:
    """The length this channel actually delivers, from its own recent songs. None if too few."""
    sid = station.get("id") or ""
    with db._lock:
        rows = db._conn.execute(
            "SELECT duration_s FROM songs WHERE station_id=? AND status='ready' AND duration_s > 0 "
            "AND COALESCE(source,'')='radio' ORDER BY created DESC LIMIT 12", [sid]).fetchall()
    lens = sorted(float(r[0]) for r in rows)
    return lens[len(lens) // 2] if len(lens) >= 3 else None


def ask_seconds(db: DB, station: dict[str, Any]) -> int:
    """The ceiling to give the engine: comfortably above what this channel really delivers.

    Not an attempt to make songs longer - that does not work (see above). This only makes sure a
    song is never cut off by its own ceiling, using the channel's own measured length where there
    is one and its configured length where there is not."""
    target = float(station.get("duration_s") or 180)
    real = delivered_median(db, station) or target
    return int(max(target, min(max(real, target) * HEADROOM, ENGINE_MAX_S)))


def station_steps(station: dict[str, Any]) -> int:
    """Sampling steps for this channel. The acoustic pass is linear in this, so it is the one
    real quality-for-speed knob - and until now it was hardcoded 32 everywhere with no way to
    vary it, which is why there is no evidence about what it is worth."""
    try:
        import config
        default = int(config.cfg("steps_default"))
    except Exception:
        default = 32
    try:
        v = station.get("steps")
        return max(4, min(int(v) if v not in (None, "") else default, 64))
    except Exception:
        return default


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def unique_title(db: DB, station: dict[str, Any], title: str, lyrics: str, pending_titles: list[str] | None = None) -> str:
    """No two songs on a station with the same name. On a clash the writer is asked once more with the used titles listed;
    if it insists, a chorus line becomes the title."""
    import llm

    used = {_norm_title(s.get("title")) for s in db.query("songs", "station_id=?", (station["id"],), limit=500)}
    used |= {_norm_title(i["title"]) for i in list_lyrics(station.get("lyrics_folder") or station["id"])}
    used |= {_norm_title(t) for t in (pending_titles or [])}
    used.discard("")
    if _norm_title(title) not in used:
        return title
    avoid = [title] + [s.get("title") for s in db.query("songs", "station_id=?", (station["id"],), limit=20) if s.get("title")]
    try:
        again = llm.title_for(lyrics, fallback="", avoid=avoid)
    except Exception:
        again = ""
    if again and _norm_title(again) not in used:
        LOG.info("radio: title '%s' already on %s, writer offered '%s'", title, station["id"], again)
        return again
    chorus = None
    lines = [l.strip() for l in (lyrics or "").splitlines()]
    for i, l in enumerate(lines):
        if re.match(r"^\[(chorus|hook)", l, re.I):
            for cand in lines[i + 1:i + 6]:
                if cand and not cand.startswith("[") and _norm_title(cand) not in used and _norm_title(cand) != _norm_title(title):
                    chorus = cand
                    break
        if chorus:
            break
    if chorus:
        words = re.sub(r"[^\w' ]+", "", chorus).split()
        cand = " ".join(words[:6]).strip()
        if cand and _norm_title(cand) not in used:
            LOG.info("radio: title '%s' already on %s, using the chorus line '%s'", title, station["id"], cand)
            return cand
    n = 2
    while _norm_title(f"{title} {n}") in used:
        n += 1
    return f"{title} {n}"


def _voice_singer(db: DB, voice_id: str | None) -> str | None:
    """A station that sings with a trained voice gets a singer of that voice's gender (measured from its recordings), so
    the AI vocal already sits near the right octave before conversion."""
    if not voice_id:
        return None
    try:
        v = db.get("voices", voice_id) or {}
        meta = v.get("train_meta")
        meta = json.loads(meta) if isinstance(meta, str) and meta else (meta or {})
        return (meta.get("pitch") or {}).get("gender") or None
    except Exception:
        return None


def unplayed_count(db: DB, station_id: str, rendered_only: bool = False) -> int:
    """Unplayed, unbanned, ready songs on a station. rendered_only leaves out imported recordings (a permanent pool of
    their own) so the keep-ahead planner still cooks fresh songs for My Songs."""
    extra = " AND COALESCE(source,'') != 'import'" if rendered_only else ""
    where, params = station_where(db, station_id)
    where = (where + " AND " if where else "") + "status='ready' AND plays=0 AND COALESCE(banned,0)=0" + extra
    return db.count("songs", where, params)


def queued_count(db: DB, station_id: str) -> int:
    return db.count("jobs", "type='song' AND status IN ('queued','running') AND params LIKE ?", (f'%"station_id": "{station_id}"%',))


def unplayed_seconds(db: DB, station_id: str, rendered_only: bool = True) -> float:
    """How much unplayed audio a channel is actually holding, in seconds.

    This is the honest unit for a radio buffer. A count of songs is not: five two-minute songs
    and five five-minute songs are the same number and less than half the cushion."""
    extra = " AND COALESCE(source,'') != 'import'" if rendered_only else ""
    where, params = station_where(db, station_id)
    where = (where + " AND " if where else "") + "status='ready' AND plays=0 AND COALESCE(banned,0)=0" + extra
    return db.total("songs", "duration_s", where, params)


def station_song_seconds(db: DB, station: dict[str, Any]) -> float:
    """What one more song on this channel is worth, in seconds of audio.

    Its own recent songs if it has any, because what a channel actually delivers and what it
    asks for are different numbers (YuE2 treats duration as a cap: ask 300 s, get about 170).
    A channel with no songs yet falls back to a conservative guess of its ask."""
    where, params = station_where(db, station.get("id") or "")
    where = (where + " AND " if where else "") + "status='ready' AND duration_s > 0 AND COALESCE(source,'') != 'import'"
    with db._lock:
        rows = db._conn.execute(f"SELECT duration_s FROM songs WHERE {where} ORDER BY created DESC LIMIT 10", params).fetchall()
    if rows:
        lens = sorted(float(r[0]) for r in rows)
        return lens[len(lens) // 2]
    return max(60.0, float(station.get("duration_s") or 180) * 0.6)


def buffer_seconds(db: DB, station: dict[str, Any]) -> tuple[float, float]:
    """(seconds on hand, seconds on the way) for a channel - rendered plus still being made."""
    sid = station.get("id") or ""
    have = unplayed_seconds(db, sid)
    coming = queued_count(db, sid) * station_song_seconds(db, station)
    return have, coming


def buffer_report(db: DB) -> dict[str, Any]:
    """One honest picture of whether the radio is winning: how much fresh audio is on hand
    across every channel, how much is on the way, and the thinnest channel of the lot.

    Nothing recorded this before, so "the radio ran dry at 3pm" left no trace at all."""
    out, total, coming_total, thin = [], 0.0, 0.0, None
    for st in db.query("stations", "enabled=1", order="sort ASC, created ASC"):
        if is_favorites(st):
            continue
        have, coming = buffer_seconds(db, st)
        total += have
        coming_total += coming
        row = {"id": st["id"], "name": st.get("name"), "minutes": round(have / 60, 1),
               "coming_minutes": round(coming / 60, 1), "target_minutes": round(target_seconds(st) / 60, 1)}
        out.append(row)
        if st.get("auto_generate") and (thin is None or have < thin["_raw"]):
            thin = {**row, "_raw": have}
    if thin:
        thin.pop("_raw", None)
    return {"minutes": round(total / 60, 1), "coming_minutes": round(coming_total / 60, 1),
            "hours": round(total / 3600, 2), "thinnest": thin, "channels": out}


def target_seconds(station: dict[str, Any]) -> float:
    """How much fresh audio this channel should be holding, in seconds.

    keep_ahead is still honoured and still means songs - it is the floor. The minutes target is
    what actually decides, because minutes are what runs out. 0 keep_ahead still means "stop"."""
    try:
        import config
        want_min = float(config.cfg("buffer_minutes"))
        default_keep = int(config.cfg("keep_ahead_default"))
    except Exception:
        want_min, default_keep = 45.0, 5
    keep = station.get("keep_ahead")
    keep = default_keep if keep is None else int(keep)
    if keep <= 0:
        return 0.0
    return want_min * 60.0


def station_next_up(db: DB, station_id: str) -> dict[str, Any] | None:
    """The song currently being made (or first in line) for a station, for the 'next up' display."""
    jobs = db.query("jobs", "type='song' AND status IN ('running','queued') AND params LIKE ?", (f'%"station_id": "{station_id}"%',), order="CASE status WHEN 'running' THEN 0 ELSE 1 END, created ASC", limit=1)
    if not jobs:
        return None
    j = jobs[0]
    p = j.get("params") or {}
    return {"job_id": j["id"], "title": p.get("title"), "status": j["status"], "progress": j.get("progress") or 0, "message": j.get("message"), "topic": p.get("topic"), "style": p.get("style")}


def next_song(db: DB, station_id: str | None, exclude: list[str] | None = None) -> dict[str, Any] | None:
    """Unplayed first (oldest first), then the least recently played, skipping banned songs and the last few played."""
    exclude = [e for e in (exclude or []) if e]
    st = db.get("stations", station_id) if station_id and station_id != "all" else None
    fav = is_favorites(st)
    where = "status='ready' AND COALESCE(banned,0)=0"
    params: list[Any] = []
    if fav:
        where += " AND liked=1"
    elif station_id and station_id != "all":
        where += " AND station_id=?"
        params.append(station_id)
    if exclude:
        where += " AND id NOT IN (%s)" % ",".join("?" for _ in exclude)
        params.extend(exclude)
    order = "RANDOM()" if (fav or (st and (st.get("replay_policy") or "") == "loop")) else "created ASC"
    unplayed = db.query("songs", where + " AND plays=0", params, order=order, limit=1)
    if unplayed:
        return unplayed[0]
    recent = [r for r in db.recent_played_ids(station_id, 5, liked_only=fav) if r not in exclude]
    if recent:
        where2 = where + " AND id NOT IN (%s)" % ",".join("?" for _ in recent)
        played = db.query("songs", where2, [*params, *recent], order="COALESCE(last_played,0) ASC, RANDOM()", limit=1)
        if played:
            return played[0]
    played = db.query("songs", where, params, order="COALESCE(last_played,0) ASC, RANDOM()", limit=1)
    return played[0] if played else None


NEW_CHANNEL_GRACE_S = 2 * 86400   # a channel made today has not been played yet, and belongs at the top anyway


def dial_sort(db: DB, stations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most listened to first, unless the setting says to keep the order set on the channels themselves.

    Each channel carries its own score (see db.note_play): a play is worth 1 and halves every week, so playing
    something else more moves it up within a day or two and stopping lets it drift back down.

    A channel made in the last two days AND NEVER PLAYED sits above all of them, newest first — otherwise
    something made a minute ago, the thing most likely to be wanted, would appear at the very bottom of the
    dial. The first play ends that: from then on it ranks on its plays like everything else, which is why a
    day-old channel already being listened to does not sit above the one being listened to most. Favorites is
    left out of it; it is built in, not something anybody just made."""
    try:
        import config
        if str(config.cfg("dial_order") or "played").lower() != "played":
            return stations
        scores = db.play_scores()
    except Exception:
        LOG.exception("dial: could not work out the play order; leaving the channels as they are")
        return stations
    t = time.time()
    for st in stations:
        s = scores.get(st["id"]) or {}
        st["plays"] = int(s.get("plays") or 0)
        st["last_heard"] = s.get("last")
        st["played_score"] = round(float(s.get("score") or 0.0), 3)
    def rank(st: dict[str, Any]):
        fresh = (not st.get("plays") and not is_favorites(st)
                 and (t - (st.get("created") or 0)) < NEW_CHANNEL_GRACE_S)
        return (0 if fresh else 1, -(st.get("created") or 0) if fresh else 0,
                -float(st.get("played_score") or 0.0), st.get("sort") or 0, st.get("created") or 0)
    return sorted(stations, key=rank)


# --------------------------------------------------------------------------- a new channel filling up
# A channel made a minute ago has nothing on it. Until it has its first songs it owns the planner: nothing else
# is queued, so the GPU spends every minute on the new channel instead of taking turns with whatever is playing.
# The screens say so, and say to go and listen to something else meanwhile.
PRIME_GIVE_UP_S = 2 * 3600      # a channel that cannot fill in two hours stops holding up the rest
PRIME_SHOW_READY_S = 30 * 60    # how long "it is ready" keeps being offered to a screen that missed it
PRIME_FALLBACK_SONG_S = 150.0   # what a song costs before this machine has rendered one


def prime_target(st: dict[str, Any]) -> int:
    """How many songs a new channel needs before it is worth tuning to: its own keep-ahead, so a channel asked
    to keep three ready is announced at three rather than made to cook five."""
    try:
        import config
        default = int(config.cfg("keep_ahead_default"))
    except Exception:
        default = 5
    try:
        return max(1, min(int(st.get("keep_ahead") or default), 12))
    except Exception:
        return default


def start_priming(db: DB, st: dict[str, Any]) -> None:
    """Called once, the moment a channel is created. A channel that has already filled up never primes again,
    however much it is edited afterwards — this is about being new, not about being short of songs."""
    if not st or is_favorites(st) or not st.get("auto_generate") or not st.get("enabled", 1):
        return
    if db.setting(f"station_primed:{st['id']}") or db.setting(f"station_priming:{st['id']}"):
        return
    db.set_setting(f"station_priming:{st['id']}", time.time())
    LOG.info("priming: %s is filling up; it wants %d songs before anything else is planned", st["id"], prime_target(st))


def _ready_songs(db: DB, station_id: str) -> int:
    return db.count("songs", "station_id=? AND status='ready' AND COALESCE(banned,0)=0", (station_id,))


def _song_seconds(db: DB, station_id: str) -> float:
    """What a song has been costing lately, for the 'about four minutes' line. This channel's own times first,
    then any channel's, then a guess."""
    for where, params in ((" AND params LIKE ?", (f'%"station_id": "{station_id}"%',)), ("", ())):
        rows = db.query("jobs", "type='song' AND status='done' AND started IS NOT NULL AND finished IS NOT NULL" + where,
                        params, order="finished DESC", limit=5)
        times = [float(r["finished"]) - float(r["started"]) for r in rows
                 if (r.get("finished") or 0) > (r.get("started") or 0)]
        if times:
            return sum(times) / len(times)
    return PRIME_FALLBACK_SONG_S


def _finish_priming(db: DB, station_id: str, songs: int, *, announce: bool = True) -> None:
    """Written down either way, so a channel never primes a second time. A channel that ran out of patience
    rather than out of work is stamped at 0, which is how the screens know not to say it is ready."""
    db.drop_setting(f"station_priming:{station_id}")
    db.set_setting(f"station_primed:{station_id}", {"at": time.time() if announce else 0.0, "songs": songs})


def priming(db: DB) -> dict[str, list[dict[str, Any]]]:
    """What every screen needs to know about channels being filled: the ones still building, and the ones that
    finished in the last half hour so a page opened a minute late still gets told.

    Whether a channel is *done* is decided here and nowhere else, so the planner, the web page and the phone
    cannot disagree about when a channel stopped being new. Called on every status poll; two small settings
    reads when nothing is building."""
    t = time.time()
    building: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    for sid, started in db.settings_prefix("station_priming:").items():
        st = db.get("stations", sid)
        if not st or is_favorites(st) or not st.get("enabled", 1) or not st.get("auto_generate"):
            db.drop_setting(f"station_priming:{sid}")     # deleted, hidden, or told to stop making songs
            continue
        want = prime_target(st)
        have = _ready_songs(db, sid)
        if have >= want:
            LOG.info("priming: %s is ready with %d songs", sid, have)
            _finish_priming(db, sid, have)
            ready.append({"id": sid, "name": st.get("name") or sid, "color": st.get("color") or "teal", "at": t, "songs": have})
            continue
        if t - float(started or 0) > PRIME_GIVE_UP_S:
            LOG.warning("priming: %s has been building two hours with %d of %d songs; letting the other channels move again", sid, have, want)
            _finish_priming(db, sid, have, announce=False)
            continue
        nu = station_next_up(db, sid)
        part = float(nu.get("progress") or 0) if (nu and nu.get("status") == "running") else 0.0
        building.append({
            "id": sid, "name": st.get("name") or sid, "color": st.get("color") or "teal",
            "ready": have, "target": want, "queued": queued_count(db, sid), "started": float(started or 0),
            "progress": round(min(1.0, (have + part) / want), 3),
            "eta_s": int(max(0.0, (want - have - part) * _song_seconds(db, sid))),
            "next_up": nu,
        })
    for sid, done in db.settings_prefix("station_primed:").items():
        at = float(done.get("at") or 0) if isinstance(done, dict) else 0.0
        if at <= 0 or t - at > PRIME_SHOW_READY_S:     # 0 = it gave up; there is nothing to celebrate
            continue
        if any(r["id"] == sid for r in ready):
            continue
        st = db.get("stations", sid)
        if not st or not st.get("enabled", 1):
            continue
        ready.append({"id": sid, "name": st.get("name") or sid, "color": st.get("color") or "teal",
                      "at": float(done.get("at") or 0), "songs": int(done.get("songs") or 0)})
    building.sort(key=lambda x: x["started"])
    ready.sort(key=lambda x: x["at"])
    return {"stations": building, "ready": ready}


def stations_status(db: DB) -> list[dict[str, Any]]:
    out = []
    for st in db.query("stations", "enabled=1", order="sort ASC, created ASC"):
        if is_favorites(st):
            st["ready"] = db.count("songs", "liked=1 AND status='ready' AND COALESCE(banned,0)=0")
            st["unplayed"] = unplayed_count(db, st["id"])
            st["queued"] = 0
            st["lyrics_files"] = 0
            st["next_up"] = None
            st["theme_names"] = []
            st["banned"] = []
            out.append(st)
            continue
        st["ready"] = db.count("songs", "station_id=? AND status='ready' AND COALESCE(banned,0)=0", (st["id"],))
        st["unplayed"] = unplayed_count(db, st["id"])
        st["queued"] = queued_count(db, st["id"])
        st["lyrics_files"] = len(list_lyrics(st.get("lyrics_folder") or st["id"]))
        st["next_up"] = station_next_up(db, st["id"])
        st["theme_names"] = [t["name"] for t in themes.allowed_themes(st)] if not st.get("instrumental") else []
        st["banned"] = themes.banned_list(st)
        out.append(st)
    return dial_sort(db, out)
