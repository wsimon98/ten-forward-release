"""lanes.py — how a heavy song is written, picked by the sound it will be sung in.

A channel's style lines say what a song should sound like (post-grunge, metalcore, death metal ...), but the words
were written the same way whatever the sound. A Seether song and a Cannibal Corpse song do not talk alike, so a
heavy sound now brings its own writing lane: the acts it is written like, how the words go, the turns of events
such songs are built on, and whether blood is the genre (the temper then never holds it back) or something a song
gets now and then.

Only heavy rock and metal sounds have lanes. Every other sound is written exactly as before.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Any

LOG = logging.getLogger("tenforward.lanes")

FLAVOUR_MEMORY = 2   # a channel does not get the same flavour of a lane three songs running

LANES: list[dict[str, Any]] = [
    {
        # not heavy, but the same problem: a reggaeton line was written like a country song (a diner, a late shift)
        "id": "reggaeton",
        "match": re.compile(r"\b(reggaeton|raggaeton|reggaetón|latin trap|latin pop|dembow|perreo|urbano)\b", re.I),
        "acts": "Bad Bunny, J Balvin, Ozuna, Daddy Yankee, Karol G and Rauw Alejandro",
        "blood": "some",
        "flavours": [
            {"id": "perreo", "voice": (
                "Reggaeton for the club: the party, the dance floor, the one he cannot stop looking at, the flirting and the "
                "chase, told with swagger and a wink. Short punchy lines that ride the dembow beat, a hook made to be shouted "
                "back, ad-libs now and then (eh, yeah, dale).")},
            {"id": "despecho", "voice": (
                "Reggaeton heartbreak (despecho): the ex shows up at the same party, the one who left now posting somebody new, "
                "drinking it off with the crew and dancing anyway. Sad words over a beat that still makes you move, a hook "
                "everybody sings along to.")},
            {"id": "flex", "voice": (
                "Latin trap flex: the come-up, the car, the chain, the crew, the haters, the night out that never ends. "
                "Confident, playful, a little dangerous, a hook built on one catchy phrase.")},
        ],
        "turns": [
            "she pulls him onto the dance floor before the song is over", "the ex walks into the same club",
            "the beach party at the end of summer", "windows down, bass up, the whole block hears it",
            "the after-party at somebody's apartment", "the message she sends at 3 a.m. after the club",
            "the crew pulls up to the party in one car", "the first night out after the breakup",
            "dancing with a stranger who knows every word", "the DJ plays their song",
        ],
    },
    {
        "id": "death-metal",
        "match": re.compile(r"\b(death metal|deathcore|grindcore|goregrind|slam)\b", re.I),
        "acts": "Cannibal Corpse, Deicide, Obituary and Six Feet Under",
        "blood": "genre",
        "flavours": [
            {"id": "killer", "voice": (
                "A horror story told straight and graphic, the way old-school death metal tells it, from the killer's side: "
                "what he did, with what, where, and what he left behind. Short, blunt, brutal lines; the chorus is a chant of "
                "three to five words said again and again. Gore is the genre here: say it plainly, no metaphor, no moral, no "
                "apology, and nobody comes to save anyone at the end. Fiction, like a horror film.")},
            {"id": "found", "voice": (
                "A horror story told straight and graphic, the way old-school death metal tells it, by the one who finds what "
                "was done or the one it is being done to: what is in the room, what is missing, what is still moving. Short, "
                "blunt lines; a chanted chorus of a few words. Gore is the genre: plain words, no metaphor, no moral, no rescue.")},
        ],
        "turns": [
            "the basement door he always keeps locked", "a hitchhiker on an empty county road at night",
            "the neighbour who asked one question too many", "what the dogs dug up behind the barn",
            "the night shift alone at the morgue", "a hunting trip where only one of them came back",
            "the motel room nobody cleaned for a week", "the chest freezer in the garage",
            "the cellar under the old farmhouse", "a body in the trunk and a flat tire",
            "the whole town knew and said nothing", "the funeral home after closing",
            "a dare to spend the night in the old slaughterhouse", "the doctor who kept the parts",
            "what he did to the man who hurt his family",
        ],
    },
    {
        "id": "metalcore",
        "match": re.compile(r"\b(metalcore|post-hardcore|post hardcore)\b", re.I),
        "acts": "Atreyu, We Came As Romans, Killswitch Engage, Bullet For My Valentine and Trivium",
        "blood": "genre",
        "flavours": [
            {"id": "revenge", "voice": (
                "Screamed verses that threaten and a clean chorus that bleeds. Revenge and betrayal said straight at the one "
                "who did it: 'you' in every verse, what was done to him and what he is going to do back. Violent pictures are "
                "the genre here (blood, bruises, broken glass, a grave dug in the yard), written plainly, never as a metaphor "
                "for a feeling. The chorus is the vow, sung big.")},
            {"id": "fight-back", "voice": (
                "The one who was held under and is done being held: by a father, a crowd, a friend who sold him out, the man he "
                "used to be. Defiant and loud, every line a shove back. Screamed verses, a huge clean chorus, and a breakdown "
                "line that is a short threat said twice.")},
        ],
        "turns": [
            "the friend who sold him out for a job", "the night he stopped taking the hits",
            "a grave he dug for something he loved", "the trial and the man who walked free",
            "the brother who stole from their mother", "the last time anyone held him down",
            "burning the house he grew up in", "the ones who left him bleeding in a parking lot",
            "a promise of payback made at a funeral", "walking into the bar where all of them were drinking",
            "the coach who broke him and the day he came back", "taking back what was stolen from him",
        ],
    },
    {
        "id": "punk",
        "match": re.compile(r"\b(melodic hardcore|hardcore punk|punk rock|skate punk|street punk)\b", re.I),
        "acts": "Rise Against, Pennywise, Strung Out and Strike Anywhere",
        "blood": "some",
        "flavours": [
            {"id": "stand-up", "voice": (
                "Urgent and anthemic, a fist in the air: standing up to whoever holds him down (a boss, a father, a landlord, "
                "a town that wrote him off, the people who laughed). Fast lines, 'I' in the verses and 'we' in a gang-vocal "
                "chorus everybody shouts together. Never politics, government, war or the news: this is one man's life and "
                "the people in it.")},
            {"id": "getting-out", "voice": (
                "Loud and fast about getting out: the town, the job, the house he grew up in; who he left behind and who he "
                "burned the bridge with on the way. Hope with a black eye, a chorus to shout in a crowd. Never politics, "
                "government, war or the news.")},
        ],
        "turns": [
            "walking out of the plant with the boss still yelling", "the eviction notice taped to the door",
            "his father saying he would never amount to anything", "the night they piled into the van and left town",
            "getting jumped and getting back up", "quitting in the middle of a double shift",
            "the friend who overdosed in the bathroom at the show", "getting pulled over for the third time that month",
            "selling everything he owned for a bus ticket", "the last show at the hall before they tore it down",
            "the day the bank took the farm",
        ],
    },
    {
        "id": "nu-metal",
        "match": re.compile(r"\b(nu metal|nu-metal|rap metal|alternative metal)\b", re.I),
        "acts": "Korn, early Staind, P.O.D., Disturbed and Linkin Park",
        "blood": "some",
        "flavours": [
            {"id": "pushed-down", "voice": (
                "Rhythmic, half-shouted lines and a chorus built on one hard line said at least twice. Being pushed down and "
                "pushing back: the father, the kids who jumped him, the voice in his own head, everybody who said he would "
                "never. Raw and personal, aimed at 'you', the one who did it.")},
        ],
        "turns": [
            "the father who drank and the night the son hit back", "the kids who jumped him behind the school",
            "walking out on the therapist", "breaking every mirror in the house",
            "the day he stopped taking the pills", "the uncle nobody in the family talks about",
            "screaming in the truck where nobody could hear", "the fight in the kitchen that ended the family",
        ],
    },
    {
        "id": "post-grunge",
        "match": re.compile(r"\b(post-grunge|post grunge|grunge|hard rock|heavy rock)\b", re.I),
        "acts": "Seether, Staind, Three Doors Down, Nickelback, Shinedown and Breaking Benjamin",
        "blood": "some",
        "flavours": [
            {"id": "bitter", "voice": (
                "Seether and Staind: bitter, wounded and pointed straight at somebody. 'You' in every verse: what you did, "
                "what you said, what you took. Hating himself is allowed too: the bottle, the man in the mirror, the things he "
                "broke. Quiet tense verses, then a chorus a man screams in his truck with the windows up. Plain words, short "
                "hard lines, the hurt under the anger.")},
            {"id": "divorced-dad", "voice": (
                "Divorced dad rock in the Nickelback and Three Doors Down vein: a working man's story told at the bar. The "
                "custody weekends, the ex and her new man, child support eating the check, the job, the truck, the kid asking "
                "why he does not live there anymore, getting older, getting back up. A big simple chorus everybody knows by "
                "the second time. Tough on the outside, a little busted inside, a dark joke now and then.")},
            {"id": "mean-streak", "voice": (
                "Hard rock with a mean streak: he has had enough of somebody (the boss, the friend who lied, the man who hurt "
                "his sister, the snake at the bar) and says exactly what happens next. Swagger and menace, a chorus like a "
                "dare. It can come to blows.")},
        ],
        "turns": [
            "the custody hand-off in a gas station parking lot", "child support taking half the check again",
            "the kid asking why he does not live there anymore", "getting laid off the week before Christmas",
            "the first drink after two years sober", "a fight in the bar parking lot after last call",
            "the call from her lawyer", "selling the boat to pay the lawyer", "the repo man taking the truck",
            "his father's funeral he almost did not go to", "the best friend who sold him out",
            "the night the cops came to the house", "moving into the apartment over the bar",
            "the last time he let somebody walk on him", "driving all night to make his kid's game",
            "finding out from somebody else", "fixing the truck in the cold because he cannot pay a shop",
        ],
    },
]


def lane_for(style: str) -> dict[str, Any] | None:
    """The lane a sound belongs to, or None for every sound that is not heavy rock or metal."""
    for lane in LANES:
        if lane["match"].search(style or ""):
            return lane
    return None


def pick(db, station: dict[str, Any], style: str, rng: random.Random | None = None) -> dict[str, Any] | None:
    """This song's lane for this sound: {id, flavour, acts, blood, turns, text}, or None. The flavour rotates per
    channel (the last FLAVOUR_MEMORY are skipped while there is another to take)."""
    lane = lane_for(style)
    if not lane:
        return None
    rng = rng or random
    flavours = lane["flavours"]
    key = f"lane_flavours:{station.get('id')}"
    try:
        used = [u for u in (db.setting(key, []) or []) if u]
    except Exception:
        used = []
    fresh = [f for f in flavours if f"{lane['id']}/{f['id']}" not in used[:FLAVOUR_MEMORY]] or flavours
    fl = rng.choice(fresh)
    tag = f"{lane['id']}/{fl['id']}"
    try:
        db.set_setting(key, ([tag] + [u for u in used if u != tag])[:6])
    except Exception:
        LOG.debug("lanes: could not remember the flavour", exc_info=True)
    text = f"HOW THIS SONG IS WRITTEN (the words of {lane['acts']}; never name them or their songs): {fl['voice']}"
    if lane["id"] != "death-metal":
        # the first test had a chorus of "I'm coming for you, yeah" four times over
        text += (" The chorus has at least three different lines; the hook line may come back once inside it, never "
                 "one line sung four times.")
    return {"id": lane["id"], "flavour": fl["id"], "acts": lane["acts"], "blood": lane["blood"],
            "turns": list(lane["turns"]), "text": text}
