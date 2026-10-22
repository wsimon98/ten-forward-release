# CRAFT — songwriting cards for the Ten Forward lyric writer

Compact, prompt-ready craft notes for the local lyric model (qwen2.5:14b, 4k context).
Everything here is meant to be pasted verbatim into a prompt; nothing is prose for humans except this file.

The source texts are not distributed with Ten Forward: only these distilled cards, which are written
for the model to read.

## What was distilled from which source

| Source (craft/sources/) | Went into |
|---|---|
| Dummy Guide to Lyricism and Rhymes (27 chapters) | craft_notes.json: all 7 core cards; the structure, hook, rhyme, imagery, storytelling, vocal-layering, genre (hip-hop, pop, country, rock, post-hardcore, folk, blues, EDM, jazz) and process cards |
| suno-genres (853 styles + co-existing styles) | genres.json: 137 radio-usable genres, 12 families, up to 6 pairings each |
| riffusion prompt | sound-prompt, sound-design, instrumental, tempo cards; the recipe below |
| Music-Writing-Project-Orientation | core-format and core-voice cards (labels, [cues], (backing vocals), ad-libs only when natural, places spelled the way they are said), exclude-prompt card, the seven-part sound prompt |

Skipped on purpose: the guide's novelty rhyme types (holorime, amphisbaenic, macaronic, rhyming slang), its cultural-borrowing and visual-rhyme chapters, and the AI chapter (it describes how to prompt a model, which the app already does).
Skipped from suno-genres: non-music entries (gong, hypnosis, language, sound, rain, scratch, asmr, 432hz, sleep...), instrument-only entries as headline genres, and anything a radio could not program.

## Counts

- craft_notes.json: 7 core cards (227 words total, always injected) + 68 topic cards (each 80 words or fewer; longest is 64).
- genres.json: 137 genres in 12 families: pop 13, rock 17, hip-hop 12, country 9, electronic 15, rnb-soul 12, jazz-blues 12, folk-world 15, classical 7, metal-punk 12, ambient-chill 8, other 5.
- pairs_with is ranked by co-occurrence count weighted by family closeness (same family x3, neighbouring family x2, other x1, instrument x0.6); low-count pairings outside the genre's own family (honky tonk + grindcore, mariachi + emo) are dropped, and sub-genres that merely contain the name (deep house under house) are demoted. Thin genres list fewer than 6 (outlaw country and contemporary country only pair with country; broadway has no usable data and lists none); the app should fall back to the family list in that case.

## Tag vocabulary (all lowercase)

structure, hook, chorus, verse, bridge, prechorus, intro, outro, rhyme, slant-rhyme, internal-rhyme, imagery, metaphor, cliche, storytelling, pacing, repetition, vocal-cues, adlibs, backing-vocals, dynamics, hip-hop, rap, pop, country, rock, metal, punk, folk, gospel, worship, rnb, soul, blues, edm, synthwave, lo-fi, jazz, love, breakup, heartbreak, longing, party, summer, money, hustle, struggle, nostalgia, small-town, faith, satire, humor, grief, anger, confidence, instrumental, sound-prompt, tempo, mood, process (added).

## How a lyric brief should pick cards

1. Always inject every `core` card, in order (about 300 tokens).
2. Add up to 2 topic cards whose tags match the brief's theme words (love, breakup, small-town, faith, party, anger...). If only one theme card matches, the second slot can take a craft card the brief implies (storytelling for a story song, satire for a parody, call-response for a hymn).
3. Add 1 topic card matching the genre. Map the genres.json family to a tag: country -> country; hip-hop -> hip-hop (or rap-flow for a second verse pass); rock -> rock; metal-punk -> metal or punk or emo-hardcore; rnb-soul -> rnb-soul or gospel-worship; jazz-blues -> jazz or blues; electronic -> edm; folk-world -> folk; pop -> pop; classical and ambient-chill -> instrumental.
4. Instrumental briefs: skip theme cards, inject instrumental + sound-design instead.
5. Sound prompt generation: inject sound-prompt, tempo and mood-words, plus the chosen genre's pairs_with list from genres.json.
Budget: core + 3 cards is roughly 500 words / 700 tokens, leaving the 4k window for the brief and the lyric itself.

## Ten rules from the guide (one line each, paraphrased, chapter noted)

1. Ch.6 — Chorus and bridge are 4-8 lines, treated as non-negotiable; verses 4-8; pre-chorus 2-4.
2. Ch.6 — The guide's own three words on an overlong chorus: "Edit. Down. Relentlessly."
3. Ch.8 — Rhyme serves meaning and flow, never the other way round; if you must contort the sentence, drop the rhyme.
4. Ch.10 — Near (slant) rhyme is the secret weapon for natural, modern lyrics; favour it over perfect rhyme.
5. Ch.10 — Perfect rhymes to use sparingly or not at all: fire/desire, love/above, pain/rain, heart/apart, true/you, sky/high, forever/together, night/light, dreams/seems.
6. Ch.3 — Provide the evidence of the emotion and let the listener be the detective; never state the feeling outright.
7. Ch.9 — Do not run one rhyme scheme for the whole song; switch between sections, and use ABCB for story verses.
8. Ch.7 — Pick one point of view and one tense and stay with them unless the switch is the point.
9. Ch.15 — Notate response vocals in parentheses and suggest specific ad-libs and BGVs (type, place, effect), not a vague "add harmony".
10. Ch.25 — Read every line aloud; if it sounds odd spoken, it will sound odder sung.

## Sound prompt recipe (riffusion prompt + orientation)

- Genre: one or two names from genres.json, optionally a pairing from pairs_with.
- Era or reference: a decade or a scene (90s Nashville, 2020s London drill), never an artist name the singer would imitate.
- Vocal tone: gender/range plus texture (gravelly baritone, breathy alto, deadpan rap, none for instrumentals).
- Instruments: 3-5, the ones that define the sound (pedal steel, 808s, Hammond organ).
- Tempo: BPM plus feel (96 BPM dragging; 128 driving).
- Production texture: room and processing (dry live room, glossy radio mix, warm tape, lo-fi cassette).
- Mood: two or three words, one feeling plus one texture.

One line, under 30 words, no lyrics, no commentary. For long-form prompts use the riffusion block (STYLE / MOOD / TEMPO / KEY / PRODUCTION / DYNAMICS, then one sound-design line per instrument, then bracketed section cues with timestamps). Exclude prompt: plain words separated by spaces, under a dozen.

## Six example sound prompts

1. Country: 90s outlaw country, gravelly baritone, telecaster, pedal steel, upright bass, brushed snare, 96 BPM, dry live room, defiant and weary.
2. Hip-hop / drill: 2020s UK drill, cold deadpan male rap, sliding 808s, sparse minor piano, hi-hat triplets, 142 BPM half-time, dark and menacing.
3. Pop: 2010s dance pop, bright female lead with stacked harmonies, synth bass, side-chained pads, four-on-the-floor kick, 122 BPM, glossy radio mix, euphoric late-night.
4. Rock: late-70s heartland rock, raspy male lead, crunchy telecaster, Hammond organ, driving bass, big live drums, 118 BPM, warm analog tape, restless and hopeful.
5. Synthwave instrumental: instrumental 80s synthwave, no vocals, analog arpeggios, gated-reverb drums, FM bass, shimmering pads, 105 BPM, neon cinematic, nostalgic midnight drive.
6. String quartet instrumental: instrumental string quartet, two violins, viola, cello, no vocals, 68 BPM, close warm chamber recording, long legato lines, wistful and tender, small-town dawn.
