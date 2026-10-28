# Ten Forward

Your own AI radio, running on your own computer. It writes the lyrics, sings and plays the songs, and
keeps its channels stocked so there is always something new on. Nothing is uploaded anywhere: the
words are written by a language model on your machine and the music is made by YuE2 on your own graphics
card. There is a web page for a computer or a phone browser, and an Android app built to be used while
driving.

![LCARS](static/icon-192.png)

## What you need

| | |
|---|---|
| Windows | 10 or 11 |
| Graphics card | NVIDIA, 24 GB is comfortable, 16 GB works with shorter songs |
| Disk | about 30 GB (the music model's weights are 13 GB and download on first use) |
| A lyric writer | [Ollama](https://ollama.com) with `qwen2.5:14b` (or `qwen2.5:7b` on a smaller card) |
| Optional | [Tailscale](https://tailscale.com), if you want the radio away from home |

Ten Forward can be installed with one click through [Pinokio](https://pinokio.computer), which sets up
the Python side, the music engine and this app for you. It also runs on its own: see *Running it by hand*.

## First run

1. Start it. The window says where it is: `http://localhost:8410` on this computer, and your computer's
   own address (something like `http://192.168.1.50:8410`) for phones and other machines on your wifi.
2. Open that page. Nothing asks you to sign in: out of the box there is one person, `admin`, with no
   password. Add people (and passwords) later in **Settings → Who is listening** if you want to.
3. Go to **Settings → Lyric writer** and check the address of your Ollama. The **Test** button says
   whether it answers.
4. Go to the **Channels** tab and press a channel, or open **Dial** and turn it. The first song takes a
   few minutes: the model has to load and then sing. After that each channel keeps a few songs ahead of
   you, so it always has something ready.

## The channels

Three to start: **Late Shift** (country and heartland rock, small-town late nights) · **Sad Boy Pop** (sad but
catchy pop about heartbreak and growing up, explicit) · **Engineering** (instrumental focus music: cinematic
electronic, post rock, downtempo). **Favorites** collects every song you heart, wherever it lives.

Every one of them can be changed, and you can add your own: a name, a few lines describing the sound,
the themes it may sing about, how many songs to keep ready. Open **Channels**, press one, and read the
editor — each field says what it does. Drop your own lyrics into `lyrics/<channel>/` as .txt files and
set that channel's *Lyrics* to **files** or **mixed**.

Every channel has a **Songs** button: tap a song to play it now, or hold it to add it to the play queue.
The queue is shared by the web page and the phone, and when it runs out the channel carries on by itself.
A song the radio sings from somebody else's words (a cover, off unless you switch it on) says so wherever
it appears. To pick one yourself, open **Cover** in Create and type an artist, a title, or an artist and a
word from the title (in either order): the artist is looked up on MusicBrainz under every name it goes by, so
a stage name finds the songs filed under the other one. Tick *keep only the chorus, write new verses* and the real
chorus stays word for word while new verses are written around it: under the channel's own rules when you pick a
channel, the writer's own when you just put it in the box.

**What a channel covers.** A channel with a covers share covers the acts its own description names. **Covers
come from** in the channel editor (web page and phone app) points them somewhere else: *Artists I name*
(`staind, seether, rise against`: misspellings and other names an act goes by are forgiven), *A genre*
(`country, southern rock, gangster rap`: about fifty genres and their nicknames are known, and the lyric writer
guesses the rest), or *Years* (`1930-1940`, `1999`, `the 90s`: the writer names well-known songs from then).
After you save, the editor shows what it was read as, with a *Read it again* button, so a wrong guess is seen and
can be typed differently. Needs *Learn from real songs* and *Keep the real words too* in Settings.

**Interpolations.** A channel can also say how often it keeps only a real song's chorus and writes new verses around it (*Interpolations %*). The new verses are written to what that song is about, read from its own words, and checked once against the chorus, so they lead into it instead of wandering off.

**Heavy sounds write like their bands.** A style line in post-grunge or hard rock, nu metal, melodic hardcore
punk, metalcore or death metal brings the way those words are written: bitter and pointed, divorced dad rock, a
mean streak, revenge, the one who fights back, a horror story told straight. Each song gets one of those
flavours, a turn of events that fits (a custody hand-off in a parking lot, the repo man taking the truck) and a
chorus with more than one line. On an explicit channel, death metal and metalcore keep their blood; the other
heavy sounds get it now and then. Every other sound is written exactly as before. The theme group **heavy**
(every other weekend, the divorce, the old man, the bottle, payback, last call, horror story and more) goes with it.

**Hearted songs stay in the rotation.** A song you heart stays on its channel and comes round again there:
about one song in three on a channel with hearted songs is one of them, the one heard longest ago, never one of
the last five played. Favorites still holds all of them together.

**Good words.** Each song the writer makes is checked before it is sung: a line that leans on a word in
**Settings → Words the radio has worn out**, or on a tired rhyme like night/light or fire/desire, is
rewritten, and if the song still has one after two tries a fresh one is written instead. When you notice
a word turning up too often, add it there; end it with * to catch every form (fade* also catches faded
and fading).

**Stories, not snapshots.** Each song idea has to be something that happens between people, with a turn
of events that changes from song to song; an idea that is only somebody standing and watching is sent
back. Songs do not make up street names. A channel says "street", or calls somebody by name, only now and
then: when one of its last few songs already did, the next one does not. Style lines rotate, so a
channel does not reach for the same sound twice in a row. **Settings → Channels that keep the older idea
writer** keeps a channel on the earlier writer if you liked it better.

## New words on a real tune

**Create → Studio → New words on a real tune** (or **Upload a song** on the Lyrics row, from either mode).
Give it a recording, or a sheet as `.abc`, `.mid` or MusicXML (`.musicxml`, `.mxl`). A recording is read
once: SheetSage2 writes down the melody and chords, Whisper hears the words, and the words land under the
sheet's verses and choruses by when they were sung. The sheet is kept under *Saved sheets*, so the next song
on that tune skips all of that. **Rewrite this** on any Library song takes the sheet it was sung from, at once.

The sheet shows how many lines each section has and how many syllables each line carries. **Write new words**
writes to that budget, under a channel's rules if you pick one or the writer's own if you do not, with a topic if
you give one; then it checks the fit line by line and rewrites the lines that miss. **Check the fit** does the
same for words you typed. **Sing it** sings your words to the saved tune: a new recording, the same melody and
song shape. Tick *channels may sing this tune* and give a channel a **Rewrites** share in its editor, and that
channel now and then sings its own words to it. Printed sheet music (`.pdf`, photos) needs
[Audiveris](https://github.com/Audiveris/audiveris) installed; the upload says so until it is.

**A cover is as long as the original.** When Create sings from a source (a Library song, a sheet, an
uploaded recording), the length cap follows the source with room to spare, so a three-minute song is not cut
at two and a half.

## A song's menu

Right-click any song in the Library or the recent list (on a phone: press and hold it) for everything you can do
with it: play now, up next, heart, rename, cover it, rewrite it, split the stems, download the mp3, make
another like it on its channel, keep it off the radio, delete.

## Less blood on an angry channel

An angry channel gets a violent song only when fewer than two of its last four were, and a song about an ex only
when fewer than two of its last four were. Otherwise the anger is aimed somewhere else: the boss, the landlord,
the bank, the town. An explicit rap channel keeps its violence.

## The public dial

`http://<this computer>:8410/listen/` is a radio anybody who can reach this computer may listen to, with no
sign-in even when sign-in is on: the dial, the song playing, play, next and download, and nothing else. It shows
no lyrics or prompts, cannot change a channel or a song, and never touches your own play queue. To put it on
the internet, point your own reverse proxy at that address; leave the rest of the app on your own network.

## Make a song longer, or hum one

**Extend this song** (in a song's menu) makes a new take a minute longer: the song carries on from its own
written score with one more verse and the chorus again. **Hum a request** (on Create, and the HUM button in the
phone app) turns a recording of you humming, or any recording, into a song: it can carry on from your tune or
sing it as it is, with words written to it. The hummed song plays next on the radio when it is ready.

## LoRAs: a sound or a style on top of the model

A LoRA is a small add-on file that pulls YuE2 toward a sound (a genre, a kind of voice, a production style).
Each channel has **LoRA on every song** with a strength, on the web page and in the phone app. To add one, put
the YuE2 LoRA file anywhere and convert it once:

```
venv\Scripts\python.exe lora_convert.py C:\path\to\some_lora.safetensors my_lora --trigger WORD
```

It reads the common layouts (ComfyUI, AI Toolkit, PEFT) and writes `loras\my_lora.safetensors`. A LoRA that
changes both halves of the model (the part that writes the music and the part that turns it into sound) also gets
a `.sound` copy that changes only the sound. If the LoRA's page names a **trigger word**, pass it with
`--trigger`: Ten Forward puts it at the front of the style line by itself whenever that LoRA is on.

**Instrumentals** use the engine's own instrumental mode, which is faster and stays wordless.

## Teach it a style: the Train tab

The **Train** tab teaches YuE2 a style from a folder of your own songs (15 to 50 is ideal). It listens to each
song and writes a caption (the words it hears, and a style line from your lyric writer), you read and fix them,
then press Train: the radio stops making songs while it trains, and you get checkpoints to **Try** and one to
**Keep**, which then shows up in every LoRA list. Two simple knobs (how long, how strong) or **Advanced**.
The trainer is a separate install (about 15 GB, NVIDIA 24 GB only): **Install the trainer** in Pinokio, or by
hand from `trainer_setup\`. Captions take about a minute a song; training itself about 40 minutes.

## A different music model

**Settings → Engine → Music model** picks the model every song is made with: `stock` (YuE2-3B as its authors
made it) or a whole-model finetune you converted with `tools\convert_base.py` from a ComfyUI one-file YuE2
checkpoint (for example the YuE2-RS finetunes). Songs made on a finetune are tagged with its name.

## Two languages

A channel whose style lines start with two languages (`Spanish, English, ...`), or whose name says Spanglish,
writes mostly-English verses with Spanish dropped in and a chorus that answers one language with the other; a
section the writer leaves all in one language is written again.

## The phone app

**Settings → The phone app** has a download link for the Android app (`/app/TenForward.apk`) and shows
the two addresses to type into it: the one on your own wifi, and your Tailscale address if you have one.
The app asks both and uses whichever answers, so it keeps working when you leave the house.

The app is the dial, with big controls for the car: channels, what is playing, pause, skip, heart, a
sleep timer and the lock-screen controls. Each channel has a **Songs** list (tap to play now, hold to
queue) and there is a queue screen. **♥ LIST** shows the hearted songs of the channel you are on (or of every
channel): tap one and it plays now while the radio stays on that channel and keeps making new songs behind it.
It keeps the next few songs on the phone so a dead zone does not stop the music, and it keeps the channel list
and a couple of hearted songs per channel too, so when it opens the dial is there at once and a channel with
hearted songs starts playing one before the computer has answered (or when it cannot be reached at all).
Channels can be made and changed in the app (hold a channel) or on the web page, LoRA included. **HUM**
records a tune with the phone's microphone and sends it to be made into a song.

## Keeping songs

Press the heart and a song is kept for good: never deleted, and copied out as a plain .mp3 with a .txt
of its lyrics next to it, under the favourites folder (**Settings → Songs and keeping**, `songs/` by
default). A song nobody hearts is deleted a week after it was first played; one that has not been
played yet is kept until it has been (up to 90 days), so nothing the graphics card made is thrown away
unheard. Both are settings; set the week to 0 and nothing is ever deleted.

## Settings

Everything is in the **Settings** tab, with a line under each one saying what it does: where favourites
go, how long unhearted songs live, how many minutes of music each channel keeps ready, which words
the writer has worn out, how often a song may name
your town, which lyric writer to use, when to let go of the graphics card, the two addresses, and who is
listening. Anything you have not changed says *default* and can be put back with one press.

Now and then a song can name a real place near you. List your places in `personal.json` next to
`server.py` (it is yours: git ignores it and an update leaves it alone):

```json
{"places": [{"name": "Springfield", "weights": {"rap": 1, "country": 3, "other": 2},
             "prompt": "Springfield: the old mill, Route 9, the Friday game. Name it once, the way a local says it."}]}
```

The weights say which kind of channel may use it. **Settings → Local place names** sets how often.

## Running it by hand

```
venv\Scripts\python.exe server.py        (or start.bat)
```

Environment variables, all optional: `TF_PORT` (8410), `TF_HOST` (0.0.0.0), `TF_VOICES` and
`TF_PERSONAL` (feature flags, off in this build), `TF_NO_WORKER` (API only, nothing is rendered).
Everything else is a setting in the page; an environment variable of the same name (shown next to each
setting) is used when nothing is set in the page.

Away from home: install Tailscale on this computer and your phone, then run `tailscale_serve.ps1`. It
prints an `https://…ts.net:8443` address; put that in Settings → Addresses and in the app.

## What is in here

```
server.py          the web app and the job worker
radio.py           channels, planning the next song, picking lyrics
sheet.py           lead sheets: reading one, its sections and syllables, words that fit it
listen.py          the public dial at /listen/
themes.py          what a song may be about, and the sound of each channel
llm.py             talking to your lyric writer
engine.py          the music model (YuE2 through Wan2GP)
loras.py           LoRAs on the model; lora_convert.py turns downloaded ones into the layout it reads
trainer.py         the Train tab (the trainer itself installs into trainer/ from trainer_setup/)
tools/             convert_base.py (a finetuned YuE2 as a music model) and small helpers
config.py          every setting, and where its value came from
auth.py            who is listening
static/            the web page (no build step, plain JavaScript)
android/           the phone app (Kotlin)
craft/             songwriting notes the lyric writer is given
lyrics/            your own lyrics, one song per file
library/           rendered songs (made at run time)
songs/             hearted songs, as plain files
data/              the database
```

## Licences

YuE2's weights are CC BY-NC 4.0 — the authors say individuals may use and monetise what it makes,
companies need a licence. Wan2GP is Apache 2.0. Ten Forward itself is yours to use and change.

The songs are made by a machine on your computer from your prompts. What you do with them is your
business, but do not pretend a machine did not make them.
