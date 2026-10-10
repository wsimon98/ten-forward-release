# Ten Forward HTTP API (v2)

Plain JSON over HTTP. The server is only reachable on the LAN and the Tailscale tailnet, and until somebody adds a second
person or a password it signs every request in as the one admin, so a client needs no credentials (see *Who is listening*).
Every client, the web app
included, talks to the same endpoints, so an Android app can drive the whole thing. CORS is open, so a WebView or a native
HTTP client on another origin works without a proxy. Media is served with byte ranges (seek works).

Base URLs: `http://<this computer>:8410` on the wifi, `https://<your tailnet name>:8443` on the tailnet (`GET /api/hello` and
`GET /api/status` both list them under `urls`).

## Discovery and health

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/hello` | `{app, version, api_version, urls: {lan, https}, features[]}` — cheap, use it to find the server |
| GET | `/api/status` | engine + worker + power + counts + `ui_build` + `urls` + `import` progress + queue flags + `building` (0.9.7) |
| GET | `/api/power` / POST `/api/power {on: bool, force?: bool}` | GPU power switch (ON reclaims the graphics card for music, OFF releases it) |
| GET | `/api/llm/health` | lyric writer (this computer's Ollama) health + `craft` notes summary |
| GET | `/api/defaults` | style presets |
| GET | `/api/themes` | the song themes stations can use |

## Radio

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/stations` | every enabled station with `ready`, `unplayed`, `queued`, `next_up`, `theme_names`, `banned`, `instrumental`, `instrumental_chance`, `retention_days`; **in dial order** (0.9.6), plus `plays`, `last_heard` and `played_score` |
| POST | `/api/stations` | create or update a station (body = station fields; `id` to update) |
| POST | `/api/stations/quick` | plain English in, a whole channel back — **admin, nothing is saved** (0.9.4) |
| DELETE | `/api/stations/{id}` | remove a station (songs stay) |
| GET | `/api/radio/next?station={id|all}&exclude={song_id,...}` | the next song to play: `{song, station, pending_jobs, unplayed, next_up, power}`. Tuning records `station_last_tuned` so restocking favours the station being heard |
| POST | `/api/songs/{id}/skip {at, duration, mode}` | a skip on the radio, as a vote (0.9.9). `at` seconds in, `mode` must be `radio`. Returns `{skips, early, retired, limit}` |
| POST | `/api/radio/{id}/generate {count, singer?}` | "Make one": plan and queue `count` (max 5) songs ahead of the auto stock; `singer` `male`/`female` pins the voice (0.8.1). **409** on a Favorites channel, which writes nothing |
| GET | `/api/history?limit=30&station={id}` | what played most recently (newest first) |
| POST | `/api/settings {radio_autofill?, queue_paused?}` | auto stock on/off, pause the render line |

A client plays a song from `song.urls.mp3` (or `voiced` / `master`), and after ~10 s of playback calls
`POST /api/songs/{id}/played` so the song counts as heard and the station moves on. Send `{"station": "<the
channel being listened to>"}` with it (0.9.6): that is what orders the dial, and it is not always the channel
the song lives on — a song heard on Favorites should count for Favorites. With no body the song's own channel
is credited, so older clients still count for something.

**Skips as a vote (0.9.9).** `POST /api/songs/{id}/skip` counts a skip against a song on `songs.skips`, and
at the `skip_retire` setting (default 3, 0 = off) sets the same `banned` flag the *Never again* button sets.
It only counts when `mode` is `radio` and `at` is inside `max(20 s, duration/5)`; anything else is recorded as
`early: false` and nothing is written. A song with `liked=1` is never retired. A client should send this
whenever the listener skips, and say so when the reply comes back `retired: true`.

**A new channel filling up (0.9.7).** `GET /api/status` carries `building`:

```json
"building": {
  "stations": [{"id": "summer-haze", "name": "Summer Haze", "color": "lav", "ready": 2, "target": 5,
                "queued": 1, "started": 1789840305.6, "progress": 0.44, "eta_s": 430, "next_up": {...}}],
  "ready":    [{"id": "summer-haze", "name": "Summer Haze", "color": "lav", "at": 1789841000.0, "songs": 5}]
}
```

`stations` is every channel still making its first songs; while any of them is there **the server plans songs
for those channels and nothing else**, whatever is being listened to, and their jobs go in at priority 4.
`target` is the channel's own `keep_ahead`; `eta_s` is the remaining songs times what a song has actually been
costing lately. `ready` holds channels that finished in the last 30 minutes, so a client that was not running
at the moment is still told; announce each one once, keyed on `id` + `at`. Both lists are empty nearly always,
and cost two small settings reads. A channel that cannot fill within two hours is released so it cannot starve
the rest, and a channel primes only once in its life.

**Dial order (0.9.6).** `/api/stations` comes back in the order it should be shown: channels made in the last
two days first (newest first), then by a listening score — one point a play, halved every week. A client shows
the list as given and does not sort it. `played_score` is that number as of now, `plays` the lifetime count,
`last_heard` a timestamp. Settings `dial_order` = `played` (default) or `fixed`.

### Quick create (0.9.4)

`POST /api/stations/quick` takes `{text, instrumental?, explicit?, cover_chance?, minutes?, keep_ahead?,
enabled?, auto_generate?}` and answers

```json
{"station": { ...every field POST /api/stations takes, plus a free id... },
 "notes": ["It will draw on Nirvana, Bush and Foo Fighters."],
 "artists": ["Nirvana", "Bush", "Foo Fighters"],
 "source": "writer",
 "needs": "lrclib_enabled"}
```

`text` is somebody's own words about the channel; under 8 characters is a **400**. `source` is `writer` when the
model answered and `plain` when it did not (the draft is then built from the words themselves and `notes` says
so). `needs` only appears when a covers share was asked for while *Learn from real songs* is off. **Nothing is
saved**: the client shows the draft, lets it be changed, and posts it to `/api/stations` like any other channel.
The call holds one model question, so allow it three minutes — it waits its turn behind a song being planned.
A client must show progress while it waits (both of ours show a spinner with the seconds on it) and must make
plain that nothing has been saved: the answer is a draft, not a channel.
An instrumental channel comes back with no mood, no themes and no covers share.

### Lyric memory and covers (0.9.3)

| Method | Path | What it does |
|---|---|---|
| GET | `/api/lyrics/habits?station=<id>` | What a channel (or the whole dial, with no `station`) keeps reaching for: `words` with the share of recent songs each appears in, `phrases`, `rhymes`, `buckets` (image families), `openings`, `perspectives`, plus the `memory` block below |
| GET | `/api/lyrics/memory` | `level`, `window`, `retry`, `per_station`, how many songs are `remembered`, the `outside` corpus state and the `covers` count |
| POST | `/api/lyrics/corpus {station?}` | Measure real songs for a channel's lane in the background (admin). **409** when learning from real songs is off |
| GET | `/api/lyrics/covers` | Covers already sung, with what each one was |
| GET | `/api/lyrics/covers/search?q=` | Search lrclib by title, artist or album. Returns `id`, `track`, `artist`, `album`, `duration` and never the words. **409** when the outside source is off |
| GET | `/api/lyrics/covers/{track_id}?station=` | The words of that song cut into a shape this radio can sing: `{title, lyrics, cover_of}`. Queues nothing |
| POST | `/api/radio/{id}/cover {id, interpolate?}` | Sing that real song on this channel now (admin). `interpolate` keeps only its chorus and has the writer write new verses. **409** on Favorites or when the outside source is off |

A song row carries **`cover_of`** (`{id, track, artist, kind}`) when it is somebody else's song; `kind` is
`cover` or `interpolation`. A station row carries **`cover_chance`** (0 to 1, null = follow the setting). 1 means every song it can find
an unused real song for; when the pool for that channel runs dry it writes its own instead, so there is no
state in which a channel goes quiet waiting for a cover.

Repetition is caught in three places: the scene, once, before the words are written (a re-roll of the idea when
it overlaps a recent one by 40 % of its content words); the guidance given to both writing calls; and the score
of the finished draft, which can send the repeating lines back once.

The memory itself is not an API object: it is the `lyric_features` table, one row per finished song, written
after the song lands. Imports, covers and instrumentals are left out of it on purpose. Nothing in this section is
in the path of making a song: guidance is built in about a millisecond from rows already stored, and the corpus
only ever grows on a background thread.

### The Favorites channel (0.9.2)

A station row carries **`kind`**. `kind: "favorites"` (seeded as id `favorites`, sort 5, colour `red`) means the channel
holds no songs of its own: every query that asks "is this song on that channel" asks `liked=1` instead, so it is a live
view of the hearted songs and a song never leaves its own channel to be in it. `POST /api/songs/{id}/like` is the only
thing that adds or removes one — there is nothing else to call.

A client can treat it like any other channel (`/api/stations`, `/api/radio/next?station=favorites`,
`/api/songs?station=favorites`); the only differences a client should show are that `ready` is a count of hearted songs,
`unplayed` / `queued` / `next_up` are always 0 / 0 / null, and nothing about making songs applies. The server keeps it
that way whoever asks: `POST /api/stations` on a favorites-kind row forces `auto_generate` 0, `keep_ahead` 0 and empty
sound / themes whatever the body says, `DELETE` is refused with 409 (hide it with `enabled: 0`), imports onto it are
refused with 400, and the keep-ahead planner never sees it because it only walks `auto_generate=1`.

## Songs

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/songs?station=&q=&liked=1&limit=` | library list (`public_song` shape: `urls`, `expires_at`, `liked`, `station_id`, `source`, `lyrics`, `style`, ...) |
| GET | `/api/songs/{id}` | one song |
| POST | `/api/songs` | Create: `{lyrics?, style?, title?, duration, mode, steps, seed, cfg, voice_id?, instrumental?, write_lyrics?, topic?, mood?, count?, source_song_id?}` → `{jobs[], lyrics, style}` |
| POST | `/api/songs/{id}/like` | heart toggle; hearted songs are exported to `D:\songs\<Station>\<Title>.mp3` (+ .txt) and never expire |
| POST | `/api/songs/{id}/ban` | never again on the radio (deleted after 24 h unless hearted) |
| POST | `/api/songs/{id}/played` | count a play |
| POST | `/api/songs/{id}/title {title}` | rename (re-exports a hearted copy) |
| POST | `/api/songs/{id}/more {style?}` | "More like this": another song on the same station with the same theme, singer and style, ahead of the auto stock |
| POST | `/api/voices/stock {again?}` | make the AI singers (one male, one female, YuE2's own vocal lifted from the radio's songs): `{voices, jobs, have}`; owner only |
| POST | `/api/voices/from_song {song_id, name?}` | a voice out of that song's singer: `{voice, job}` (a `voice_prep` job lifts the vocal and keeps its loudest 30 s) |
| POST | `/api/songs/{id}/revoice {voice_id, semi_tone_shift?, blend?, key_safe?}` | sing it again in a saved voice: `blend` 0..1 (yours against the AI singer; 0 = the AI singer from the sample alone), `key_safe` false = no octave fit |
| POST | `/api/songs/{id}/stems` | split vocals / instrumental |
| DELETE | `/api/songs/{id}` | delete (also removes the exported copy) |
| GET | `/media/{id}/{kind}?dl=1` | the audio (`mp3`, `voiced`, `master`, `vocals`, `instrumental`, `abc`); Range requests honoured; `dl=1` forces a download name |

Retention: unhearted radio songs live `TF_RADIO_RETENTION_DAYS` (7) unless the station sets `retention_days` (0 = never, as
My Songs does); banned songs 24 h; imports, Studio songs and hearted songs forever. `expires_at` on every song says when.

## Sheets and new words on a real tune (1.6)

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/arrangements` | saved sheets: `{id, name, source, song_id, duration_s, on_radio, used, has_words, info{key, meter, bpm, bars, budget[], budget_source, sections[]}}` |
| GET | `/api/arrangements/{id}` | one sheet in full: `abc`, `words` too |
| POST | `/api/arrangements` | `{source_song_id}` (its own sheet, at once) / `{file}` (an upload: a recording -> `{job}`; `.abc/.mid/.musicxml/.mxl` -> `{arrangement}`; a printed page -> `{job}` via Audiveris) / `{abc_text, words?}`; `name`, `instrumental` optional. The same file twice is the same sheet (`existing: true`) |
| POST | `/api/arrangements/{id}` | update `name`, `words` (the budget follows), `on_radio` |
| DELETE | `/api/arrangements/{id}` | forget it (admin) |
| POST | `/api/arrangements/{id}/fit` | `{lyrics}` -> `{ok, off, missing, extra, lines[{section, n, text, has, want, diff}], instruction}` |
| POST | `/api/arrangements/{id}/words` | `{station?, style?, topic?}` -> `{lyrics, fit, style, station, budget}`; a channel needs the owner; nothing is queued |
| POST | `/api/arrangements/{id}/sing` | `{lyrics, style?, station?, mode (0/1), voice_id?, loras?, instrumental?, seed?, steps?, count?}` -> `{jobs[], job, title, style, fit}`; the sheet travels with the job as `abc_text` |
| POST | `/api/swap` | multipart `file` (a recording), `voice_id`, `blend` (0..1, trained voices: yours against the AI singer it was trained from; 0 = the AI singer from the sample alone), `pitch` (`auto` whole octaves into the voice's range / `song` as sung / `up` / `down` one octave), `semi_tone_shift` (-12..12, with `auto`), `station_id` (My Songs) -> `{song, job}`: the song is imported and a `revoice` queued; the take lands as `<song> - revoiced` tagged with the octave it moved |
| POST | `/api/stations` | `rewrite_chance` (0..1, blank = never): how often the channel sings its own words to a sheet marked `on_radio` |

## Queue

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/queue` | `{running, queued[], recent[], queue_paused, radio_autofill, power, engine, time}` with job views (`title`, `station`, `requested_by` auto/you/studio, `progress`, `message`) |
| GET | `/api/jobs?status=&limit=` · GET `/api/jobs/{id}` | raw jobs |
| POST | `/api/jobs/{id}/cancel` · `/bump` · `/later` | cancel (a running one starts the next), move to the front, send to the back |
| DELETE | `/api/jobs/{id}` · POST `/api/jobs/clear` | tidy finished jobs |

Priorities: Create / re-voice 10, Make one / More like this / Sing next 5, restock for the station being heard 3, other restock 0.

## Imports (your own recordings and lyric notes)

| Method | Path | What |
| --- | --- | --- |
| POST | `/api/import/upload` (multipart `file`, `station_id`=my-songs, `title?`, `album?`) | one finished track → a ready library song, kept forever, lyrics attached when a lyric file with a matching title exists |
| POST | `/api/import/folder {path, station_id?, recursive?}` | import every audio file under a folder this computer can see (runs in the background) |
| GET | `/api/import/status` | progress of the folder import |
| POST | `/api/import/keep` (multipart `file`, `folder`=my-songs) | a Google Keep export → one lyric file per song |

## Lyric files

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/lyrics/folders` | folders with file / used counts |
| GET | `/api/lyrics/folders/{folder}` | files: `{file, title, chars, hash, used}` |
| POST | `/api/lyrics/folders/{folder} {title, lyrics}` | add a file |
| GET | `/api/lyrics/folders/{folder}/{file}` | `{title, lyrics, used, songs[]}` |
| DELETE | `/api/lyrics/folders/{folder}/{file}` | remove a file |
| POST | `/api/lyrics/folders/{folder}/{file}/render {station_id?, style?}` | "Sing next": queue this file ahead of the auto stock on the station that owns the folder |
| POST | `/api/lyrics/write {topic, mood, style, length}` · `/api/lyrics/improve {lyrics, instruction}` · `/api/style/suggest {description, lyrics}` | the lyric writer (this computer's Ollama) |

## Voices

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/voices` · POST `/api/voices` (multipart `file`, `name`, `remove_music`, `source`) | saved voices for re-voicing |
| POST | `/api/voices/{id}/rename` · DELETE `/api/voices/{id}` · GET `/api/voices/{id}/file` | manage |
| POST | `/api/upload` (multipart) | generic upload for Studio (source audio / abc / lyrics) |
| POST | `/api/transcribe {path}` | Whisper transcription job |

## Client telemetry and problems (0.9.1)

`POST /api/client/log {event, song_id, title, t, duration, ua, detail, mode, station, level, tag, message, source,
device, app_version}` — playback anomalies (ended-early, stalled, error, skip) and whatever the phone app's Problems
screen has to report. Public, so a phone can report before it is signed in; the signed-in name is added server-side.
Every line is JSON in `logs/client.log`, with an `at` epoch.

`GET /api/client/log?limit=200` (admin) — `{entries: [...newest first], keep_days}`. **Nothing older than two days is
kept**: the file is pruned on write (and capped at 4000 lines), and two days is what the phone keeps as well. The
server's own `logs/server.log` and `logs/access.log` roll at midnight with two days behind them.

## Notes for the Android app

* Poll `/api/status` every few seconds for `power`, queue counts and `ui_build`; poll `/api/stations` for `next_up`.
* The web app marks a song played after 10 s and asks `/api/radio/next?exclude=<current id>` when a song ends; do the same
  so both clients agree on what "played" means.
* Ranges are supported, so a native player (ExoPlayer / MediaPlayer) can stream `/media/...` directly.
* Everything is LAN / tailnet only. Do not put this behind a public host.

## Who is listening (0.9)

Out of the box there is one person (`admin`, no password) and the server signs every request in as them:
an unauthenticated client works exactly as before. As soon as a second person exists, or anybody has a
password, everything except the public paths answers `401 {"detail": "login"}`.

Public without a session: `/`, `/static/*`, `/sw.js`, `/manifest.webmanifest`, `/api/hello`, `/api/status`
(trimmed: version, power, features, `needs_login`), `/api/auth/*`, `/api/client/log`, `/app/*`.

A session is carried by the `tf_session` cookie (browsers), by `Authorization: Bearer <token>` (the app),
or by `?token=<token>` — that last one only on `/media/...`, `/app/...` and `/api/voices/...`, because
media players cannot set headers.

| Method | Path | What |
| --- | --- | --- |
| POST | `/api/auth/login` | `{name, password?, device?}` → `{token, user, login_required}` and a cookie |
| POST | `/api/auth/logout` | ends this session |
| GET | `/api/auth/me` | `{user, login_required, users}` — `user` is null when somebody has to sign in |
| GET | `/api/auth/users-public` | `[{name, needs_password, role}]`, empty when nobody has to sign in |
| GET | `/api/users` | admin: everybody, with `has_password`, `last_seen`, `sessions` |
| POST | `/api/users` | admin: `{name, password?, role?}` |
| POST | `/api/users/{id}/password` | admin, or your own: `{password}` — `null` removes it |
| POST | `/api/users/{id}/role` | admin: `{role: "admin"\|"user"}`, never the last admin |
| DELETE | `/api/users/{id}` | admin, not yourself, not the last admin |

Admin-only elsewhere: `POST /api/settings` (anything but `radio_autofill`/`queue_paused`), `/api/power`,
`POST|DELETE /api/stations`, the theme catalog, `DELETE /api/songs/{id}`, the imports, the lyric-file
writes and `/api/engine/unload`.

## Settings (0.9)

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/settings` | `{settings: [{key, label, help, group, type, value, default, env, source, restart}], groups, features, urls, admin, apk}` |
| POST | `/api/settings` | `{key: value, ...}` — any key in `config.DEFAULTS` (admin), plus `radio_autofill` and `queue_paused` (anybody) |
| POST | `/api/settings/reset` | `{key}` → forget the stored value, fall back to env/default |
| POST | `/api/settings/reexport` | copy every hearted song into the favourites folder as it is set now |

`source` is `db`, `env` or `default`. `type` is `str`, `int`, `float`, `bool` or `path`. A `path` is created
and probe-written before it is accepted; numbers are clamped; addresses must start with http.

`GET /api/status` gained `features {voices, personal, voices_available, login_required}`, `user`,
`needs_login` and `header_tagline`. `GET /api/hello` gained `login_required` and drops `voices` from its
feature list when voices are off.

## The phone app (0.9.1)

`GET /app/TenForward.apk` hands out the built app (public, so a phone can fetch it before it has a token);
`GET /app` redirects to it.

`GET /api/app/version` (public) — `{available, version, build, size_mb, bytes, built, notes, url}`, read from
`dist/app.json`, which `android\build.ps1` writes beside the APK. The app asks this when it opens and offers to update
itself when `build` is higher than its own `versionCode`; it downloads `url` and hands the file to Android's installer.
`build` is 0 when there is no app.json, which the app reads as "nothing to say".

The app calls: `/api/hello`, `/api/auth/login`, `/api/stations`, `/api/radio/next`, `/media/<id>/mp3?token=`,
`/api/songs/<id>/played`, `/api/songs/<id>/like`, `/api/client/log`, `/api/app/version`, and — only from the channel
editor, so only for an admin — `POST /api/stations`, `DELETE /api/stations/<id>`, `POST /api/radio/<id>/generate`,
`GET /api/themes`. It never writes a theme or a setting.

## Themes (0.5)

The theme catalog decides what invented songs are about. Stations list theme ids or whole groups in `themes`.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/themes` | `full=1` for briefs, weights and flags | `{themes:[{id,name,kind[,brief,weight,custom,edited,hidden]}], kinds:[...], banned_default, ban_options, fusion_sets}` |
| POST | `/api/themes` | `{id?, name, brief, kind, weight}` | the saved theme. No `id` creates a custom theme (id slugged from the name); an existing id edits it |
| DELETE | `/api/themes/{id}` | | `{ok}`. A custom theme is removed; a built-in is hidden from every station until reset |
| POST | `/api/themes/{id}/reset` | | the built-in theme with its original text (also un-hides it) |

Briefs may use the slots `{Singer} {singer} {partner} {they} {them} {their} {theirs}`; the writer fills them from the
singer's gender. Edits live in `data/themes.json` (`{"themes":[...], "removed":[...]}`).

Station fields added in 0.5 (accepted by `POST /api/stations`, returned by `GET /api/stations`): `mood` (free text,
an attitude kept for every song, up to 300 chars) and `explicit` (0/1: swearing plus violence, guns, gangs and death
as subject matter). Groups now include `street`.

Station field added in 0.7: `post_fx` (JSON, see the Sleeping Sounds section).

0.8: `themes` entries that are neither a catalog id nor a group are ad-hoc themes (id `adhoc-<slug>`, the text is the brief); `GET /api/stations` lists them in `theme_names`. Groups now include `late`. A sung station whose `fusion_set` names a set with `vocal_cross` (`summer`, `bluehour`) gets its style crossed with 1–2 fusions about half the time; the song carries a `<genre> x <fusion>` tag. `POST /api/stations` keeps the stored `color` when the body sends none or `teal`, and keeps `fusion_set` when the body sends an empty one without `fusion_set_cleared: true`.

0.8.1 station fields: `male_ratio` (0–1 or null: share of sung songs with a male voice; the planner rolls the gender and picks a matching style line) and `place_chance` (0–1 or null = 0.12: how often a song names one of the owner's real places from personal.json, weighted by genre). A song that rolled a place has the place name in `tags` and job params carry `place`.

## Trained voices (0.6)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/voices/{id}/train` | `{steps?, remove_music?: true}` | `{job}`: a `voice_train` job (409 if one is already queued or running) |
| POST | `/api/voices/{id}/train/files` | multipart `files[]` (any audio) | `{added, files:[{name,seconds}], seconds}` |
| POST | `/api/voices/{id}/train/folder` | `{path, exclude?: [substrings], max_minutes?}` | the index plus `added` / `skipped` lists; files are copied in as 44.1 kHz mono wav, marked `separated: false` until the train job pulls the vocal out |
| DELETE | `/api/voices/{id}/train/files/{name}` | | the updated index |
| DELETE | `/api/voices/{id}/model` | | `{ok}`: forget the trained model |
| POST | `/api/songs/{id}/revoice` | `{voice_id, semi_tone_shift?, blend?}` | the job; a trained voice converts through its model. `blend` 0.25–1 (default 1) interpolates the trained weights with the base singing model: 1 = all trained, 0.5 = half; songs below 1 carry a `NN% voice` tag |

`GET /api/voices` rows gained `model` (path or null), `trained`, `train_meta` (`{clips, seconds, steps, minutes}`),
`train_files`, `train_seconds` and `training` (`{job_id, status, progress, message}` while a fine-tune runs).

`GET /api/voices` rows also carry `pitch` (`{median_hz, low_hz, high_hz, median_note, low_note, high_note, range_semitones, gender}`) once trained.

## Sleeping Sounds (0.7)

A station with `post_fx` gets a tone layer mixed under every render before the mp3 is made:

```json
"post_fx": {"binaural": 1, "carrier_hz": [140, 230], "beat_hz": [1.5, 6.5], "level_db": -21, "noise": "brown", "noise_db": -36}
```

`carrier_hz` / `beat_hz` are ranges drawn per song; `level_db` is the beat's peak in dBFS; `noise` is one of `brown`,
`pink`, `rain`, `ocean` (or absent) at `noise_db`. The song's `files` gain `layered` (the mixed wav) and its `tags` gain
`"<beat> Hz <delta|theta|alpha> on <carrier> Hz"`. Tone-only tracks come from `python sleeptones.py <out_dir> [seconds]`
(one wav per preset) and go in through `POST /api/import/upload` with `station_id=sleeping-sounds`.
