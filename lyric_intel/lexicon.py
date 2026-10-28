"""lexicon.py — the words Ten Forward counts, and the buckets it sorts them into.

Nothing here is a ban list. The buckets are how the analyser answers "is this song reaching for the same
drawer again", and how it suggests a drawer it has not opened lately. Functional English (the, and, you)
is never counted, so it can never be restricted.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- what never counts
STOP = set("""
a an and the or but if then so to of in on at for from by with without into onto over under up down out off
i me my mine myself you your yours he him his she her hers it its we us our ours they them their theirs
this that these those there here where when why how what which who whom whose while as than because
is am are was were be been being do does did doing done have has had having will would shall should
can could may might must ought let lets need needs got get gets getting gonna wanna gotta cause cuz
not no nor never ever just only even still yet too very all any some each every both few more most other such own same
oh yeah yea ooh ohh na la da hey ah uh huh yo ay ayy okay ok mm mmm hmm woah whoa oooh
aint dont didnt cant wont isnt wasnt werent couldnt shouldnt wouldnt hasnt havent hadnt doesnt arent
im ive ill id youre youve youll youd hes shes theyre theyve theyll were weve well thats theres heres lets
one two three four five six seven eight nine ten hundred thousand million
now then again about after before against between through during above below around across along
say says said tell tells told know knows knew think thinks thought see sees saw come comes came go goes went
make makes made take takes took give gives gave keep keeps kept let leave leaves left put puts
want wants like likes feel feels feeling thing things something nothing anything everything
""".split())

# contractions collapse to their stem when tokenising, so these matter too
STOP |= {"ll", "ve", "re", "s", "t", "d", "m", "em", "til", "till", "bout", "yall"}

# --------------------------------------------------------------------------- the drawers
# A bucket is a family of concrete images. Counting them answers "how much of this song is clothing / cars /
# weather", and the least-used ones become the positive suggestion for the next song.
BUCKETS: dict[str, set[str]] = {
    "clothing": set("""hoodie hoodies jacket coat shirt tshirt sweater sweatshirt jeans denim boots shoes sneakers
        heels dress skirt hat cap scarf gloves socks pocket pockets collar sleeve sleeves zipper button buttons
        flannel uniform apron tie belt hem cuff cuffs""".split()),
    "jewelry": set("""chain chains ring rings necklace bracelet watch earrings gold silver diamond diamonds pendant
        locket band""".split()),
    "vehicles": set("""car cars truck trucks van bus taxi cab bike bicycle motorcycle engine hood trunk tires tire
        wheel wheels dashboard windshield wiper wipers seat seats backseat bumper tailgate pickup diesel horn
        headlights taillights ignition clutch gearshift mirror odometer""".split()),
    "roads": set("""road roads highway freeway interstate street streets avenue lane alley driveway sidewalk curb
        intersection stoplight crosswalk exit ramp overpass bridge gravel asphalt pavement shoulder mile miles
        milemarker route turnpike""".split()),
    "weather": set("""rain raining rained storm thunder lightning wind windy snow snowing sleet hail fog mist frost
        ice drizzle downpour puddle puddles clouds cloudy humid heat drought breeze gust""".split()),
    "light": set("""light lights lamp lamps streetlight streetlights neon glow glowing flicker flickers flickering
        headlight bulb candle candles flashlight lantern beam beams spark sparks shine shining gleam sunlight
        moonlight firelight dashboardlight""".split()),
    "dark": set("""dark darkness shadow shadows dim black blackout night nights midnight dusk twilight gloom
        silhouette""".split()),
    "kitchen": set("""kitchen fridge refrigerator freezer stove oven counter countertop sink faucet dishes plate
        plates bowl mug cup glass kettle toaster microwave pan pot spoon fork knife table napkin crumbs""".split()),
    "house": set("""house home apartment room rooms bedroom bathroom hallway stairs staircase basement attic garage
        porch deck patio yard driveway fence gate roof ceiling floor wall walls rent lease landlord mailbox""".split()),
    "openings": set("""door doors doorway window windows blinds curtain curtains screen shutter shutters latch lock
        keyhole threshold porchlight doorbell""".split()),
    "furniture": set("""couch sofa chair chairs bed mattress pillow pillows blanket sheets dresser drawer drawers
        shelf shelves mirror rug carpet lamp nightstand desk bench stool""".split()),
    "phone": set("""phone phones text texts texting message messages call calls calling voicemail ring ringtone
        screen notification inbox contacts dial unread typing read receipt seen scroll""".split()),
    "money": set("""money cash check paycheck bill bills dollar dollars cent cents wallet purse bank account
        savings debt loan rent change coins register atm card credit broke rich stacks band bands
        commission tip tips""".split()),
    "work": set("""work working job shift shifts overtime clock punch boss manager crew coworker warehouse factory
        office desk shop floor register counter uniform badge break lunchbreak schedule timecard quota""".split()),
    "drink": set("""coffee tea water soda beer whiskey bourbon wine liquor bottle bottles glass can cans cup
        thermos flask pour sip sips brew ice cubes lemonade""".split()),
    "food": set("""dinner breakfast lunch supper sandwich toast eggs bacon pancakes pizza fries burger soup stew
        bread butter sugar salt pepper plate leftovers takeout diner menu""".split()),
    "body": set("""hand hands arm arms shoulder shoulders knee knees back chest neck spine skin bones ribs wrist
        wrists ankle fingers finger thumb palm elbow hip""".split()),
    "face": set("""face eyes eye mouth lips smile smiles smiling teeth cheek cheeks jaw chin hair eyebrows lashes
        freckles frown grin stare glance blink""".split()),
    "night-hours": set("""night midnight morning dawn sunrise sunset evening afternoon noon hour hours minute minutes
        clock alarm oclock late early tonight tomorrow yesterday weekend friday saturday sunday monday""".split()),
    "rural": set("""field fields farm barn silo tractor fence pasture crop corn wheat hay creek pond woods forest
        trail dirt gravel county acre acres orchard grain elevator pickup""".split()),
    "city": set("""city downtown block blocks corner sidewalk subway train bus stop skyline building buildings
        apartment tower rooftop alley bodega store storefront parking lot garage traffic crowd""".split()),
    "water": set("""lake river ocean sea beach shore sand wave waves pool creek stream dock pier boat canoe raft
        swim swimming tide current bank bridge rain puddle""".split()),
    "plants": set("""tree trees grass leaves leaf flower flowers garden weeds vine branch branches roots pine oak
        maple bloom blooming hedge lawn""".split()),
    "animals": set("""dog dogs cat cats bird birds crow deer horse horses cow cows fish moth mosquito bug bugs
        wings feathers paws bark""".split()),
    "sound": set("""hum hums humming buzz buzzing click clicks ticking tick rattle rattling creak creaking slam
        slams knock knocking whisper whispers echo echoes silence quiet static crackle sirens siren engine radio
        thump""".split()),
    "touch": set("""cold warm hot cool heat chill damp wet dry rough smooth soft sharp sticky dusty heavy light
        tight loose numb burn burning freeze frozen""".split()),
    "travel": set("""ticket bag bags suitcase duffel trip drive driving airport bus station platform map gps
        motel hotel room key checkout mile marker border state line""".split()),
    "school": set("""school class classroom locker lockers hallway bell teacher desk homework notebook pencil
        bus gym cafeteria graduation diploma dorm campus""".split()),
    "sky": set("""sky stars star moon sun clouds horizon sunrise sunset dawn dusk space planets constellation
        blue overhead""".split()),
    "tools": set("""hammer nails wrench screwdriver tape rope wire ladder shovel saw drill toolbox bucket paint
        brush glue chain""".split()),
    "paper": set("""paper receipt receipts note notes letter letters envelope stamp card postcard photo photos
        picture album page pages book magazine newspaper list ticket""".split()),
}

# a bucket that a station's banned topic makes unusable as a suggestion
BUCKET_BANS = {"drink": "alcohol", "food": None}

BUCKET_LABELS = {
    "clothing": "clothes and what is left in a pocket",
    "jewelry": "jewellery and what somebody wears on the hand",
    "vehicles": "cars and what is inside one",
    "roads": "roads, streets and the space between towns",
    "weather": "weather other than rain",
    "light": "light sources",
    "dark": "dark and shadow",
    "kitchen": "a kitchen and what is on the counter",
    "house": "rooms of a house and the outside of it",
    "openings": "doors, windows and what you look through",
    "furniture": "furniture and what a room holds",
    "phone": "phones and messages",
    "money": "money in the hand",
    "work": "work and the shift",
    "drink": "what is in the cup",
    "food": "food and a meal",
    "body": "hands, arms and the body",
    "face": "a face and what it does",
    "night-hours": "the hour on the clock",
    "rural": "farm ground and small county roads",
    "city": "a city block",
    "water": "water, a lake or a shore",
    "plants": "trees, grass and growing things",
    "animals": "animals",
    "sound": "sounds and noises in a room",
    "touch": "temperature and texture",
    "travel": "packing, leaving and arriving",
    "school": "school and the years in it",
    "sky": "the sky",
    "tools": "tools and physical work with the hands",
    "paper": "paper, photographs and things written down",
}

# reverse index, built once
WORD_BUCKET: dict[str, str] = {}
for _b, _words in BUCKETS.items():
    for _w in _words:
        WORD_BUCKET.setdefault(_w, _b)

# buckets that make poor suggestions (too abstract or already everywhere)
SEED_SKIP = {"dark", "light", "touch", "night-hours"}


def seed_buckets() -> list[str]:
    return [b for b in BUCKETS if b not in SEED_SKIP]
