"""Work out the best set of gear to wear from a character's gear dump.

The problem: a pack full of looted armour, each piece carrying a few cantrips
("Major Focus", "Minor Flame Ward"...), and seventeen-odd places to wear
things. Which pieces, worn together, give the most useful cantrips? Sorting
that out by eye in the inventory panel is slow; it is a small optimisation
problem, and this module solves it exactly.

Inputs
------
* A **gear dump** from the Cantrip Tailor companion plugin
  (``plugin/cantrip_tailor``): every item worn or wielded and every wearable
  in the packs, with slots, spells and requirements.
* **Priorities** (:func:`build_weights`): points for each cantrip, highest
  for the attack skill, then the buffing schools, the defences, mana
  conversion and the wards - see :data:`DEFAULT_POINTS`.

The rules of wearing things
---------------------------
* **Clothing and armour** clash when their *coverage* overlaps. Coverage
  (``ClothingPriority``, PropertyInt 4) has one bit per body part and
  layer - outer chest, outer upper arms, ... , underwear chest, ... , head,
  hands, feet - and it is what an ACE server checks (``Creature_Equipment.
  GetEquippedClothingArmor`` in the ACEmulator source). It is *not* the same
  as the equip-slot mask:
  a tunic and a pair of pants both claim the abdomen slot yet can be worn
  together, because only the pants cover the abdomen. So a hauberk blocks
  chest, abdomen and both arm slots at once, and a robe blocks all armour.
* **Jewellery** goes by count: one necklace, one trinket, two bracelets, two
  rings.
* Things in the hands (wand, weapon, shield) and aetheria are left alone:
  whatever is equipped there stays, and its cantrips count as already had.

A cantrip only helps once: two pieces with Major Focus give the same +15 as
one, and a Major makes a Minor of the same thing pointless. The score of a
set is therefore, for each cantrip, the points of the *best level* present.

Why not a genetic algorithm
---------------------------
Because the problem is small and has exact structure. With one yes/no
variable per item, "coverage bits used at most once" and "at most two rings"
are linear constraints, and "best level of each cantrip" becomes one extra
yes/no variable per (cantrip, level) that may be 1 only if some chosen item
supplies that level or better. That is a textbook integer programme; SciPy's
bundled HiGHS solver returns the provably best set in milliseconds for a
hundred items, where a GA would return a good guess and need tuning.

Requirements
------------
Two different things can stop a piece being useful, and the dump shows both:

* **Wield requirements** (level, base Melee Defense 370...) stop it being put
  on at all. The client works this out itself (``can_wield``).
* **Activation requirements** let it be worn but leave its spells dark: the
  wearer's *current* Arcane Lore must reach the item's difficulty, and often
  a defence skill must reach a level too (``WorldObject_Use.
  CheckUseRequirements`` in the ACEmulator source - buffed skill counts). The dump carries the
  character's skills as they stood when it was taken, so this can be checked.

By default both are reported and not enforced - the wearer may be about to
buff, level or tinker. ``usable_only`` leaves out every piece that fails
either, judged by the skills in the dump.

Level weighting uses the game's own numbers (the stock spell table): skill
and attribute cantrips give +5 / +10 / +15 / +25 / +35 for Minor / Moderate /
Major / Epic / Legendary; wards cut damage by 10 / 15 / 20 / 25 %; Armor adds
20 / 40 / 60 / 80. So with Major as 1, a Minor skill cantrip is worth a third
of its Major, a Minor ward two thirds - which is what puts "a Minor of
something important" in the middle of the list, above a Major of something
minor.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("Minor", "Moderate", "Major", "Epic", "Legendary")
MAJOR = LEVELS.index("Major")
# Value of each level relative to a Major, per family (see module docstring).
_SKILL_FACTORS = (5 / 15, 10 / 15, 1.0, 25 / 15, 35 / 15)
_WARD_FACTORS = (10 / 15, 12.5 / 15, 1.0, 20 / 15, 25 / 15)
_ARMOR_FACTORS = (20 / 40, 30 / 40, 1.0, 60 / 40, 80 / 40)

WARDS = tuple(f"{e} Ward" for e in ("Acid", "Bludgeoning", "Flame", "Frost", "Piercing", "Slashing", "Storm"))
_ALIASES = {"Bludgeon Ward": "Bludgeoning Ward"}   # the spell table has both spellings
_CANTRIP = re.compile(r"^(" + "|".join(LEVELS) + r") (.+)$")

# Every cantrip (other than the wards) that loot-generated armour, clothing and
# jewellery can carry, spelt as the client's spell table spells them. Used to
# catch typos in --want; anything else found on an item in a dump is accepted too.
KNOWN_CANTRIPS = (
    "Strength", "Endurance", "Coordination", "Quickness", "Focus", "Willpower", "Armor", "Impenetrability",
    "Acid Bane", "Bludgeoning Bane", "Flame Bane", "Frost Bane", "Piercing Bane", "Slashing Bane",
    "Storm Bane", "Alchemical Prowess", "Arcane Prowess", "Armor Tinkering Expertise", "Cooking Prowess",
    "Creature Enchantment Aptitude", "Deception Prowess", "Dirty Fighting Prowess", "Dual Wield Aptitude",
    "Fealty", "Finesse Weapon Aptitude", "Fletching Prowess", "Healing Prowess", "Heavy Weapon Aptitude",
    "Impregnability", "Invulnerability", "Item Enchantment Aptitude", "Item Tinkering Expertise",
    "Jumping Prowess", "Leadership", "Life Magic Aptitude", "Light Weapon Aptitude", "Lockpick Prowess",
    "Magic Item Tinkering Expertise", "Magic Resistance", "Mana Conversion Prowess",
    "Missile Weapon Aptitude", "Monster Attunement", "Person Attunement", "Recklessness Prowess",
    "Salvaging Aptitude", "Shield Aptitude", "Sneak Attack Prowess", "Sprint", "Summoning Prowess",
    "Two Handed Combat Aptitude", "Void Magic Aptitude", "War Magic Aptitude", "Weapon Tinkering Expertise",
)

# --attack choices -> the cantrip that boosts that skill.
ATTACKS = {
    "void": "Void Magic Aptitude", "war": "War Magic Aptitude",
    "missile": "Missile Weapon Aptitude", "heavy": "Heavy Weapon Aptitude",
    "light": "Light Weapon Aptitude", "finesse": "Finesse Weapon Aptitude",
    "two-handed": "Two Handed Combat Aptitude", "summoning": "Summoning Prowess",
}
# ACE skill ids, for guessing the attack skill from the dump's skill list.
_ATTACK_SKILL_IDS = {34: "war", 43: "void", 47: "missile", 44: "heavy", 45: "light", 46: "finesse", 41: "two-handed"}
# Attributes in each attack skill's formula: worth a little on top.
_ATTACK_ATTRIBUTES = {
    "void": ("Focus", "Willpower"), "war": ("Focus", "Willpower"), "missile": ("Coordination",),
    "heavy": ("Strength", "Coordination"), "light": ("Strength", "Coordination"),
    "finesse": ("Coordination", "Quickness"), "two-handed": ("Strength", "Coordination"),
    "summoning": ("Endurance", "Willpower"),
}

# Points for a *Major* of each cantrip: the attack skill, then the buffing
# schools creature > life > item, then melee and magic defence, mana
# conversion, then the wards. "support" is the modest value given to things
# nobody asks for but everybody uses. Change any of it with --want.
DEFAULT_POINTS = {
    "attack": 100.0,
    "Creature Enchantment Aptitude": 90.0,
    "Life Magic Aptitude": 80.0,
    "Item Enchantment Aptitude": 70.0,
    "Invulnerability": 60.0,          # melee defence
    "Magic Resistance": 60.0,         # magic defence
    "Mana Conversion Prowess": 50.0,
    "ward": 30.0,                     # each of the seven
    "support": 15.0,
}
SUPPORT = ("Armor", "Endurance", "Impregnability", "Focus", "Willpower", "Coordination", "Quickness")
# --want NAME=high|med|low|none slots an optional cantrip into that order:
# high sits between item enchantment and the defences, med between mana
# conversion and the wards, low with the support cantrips.
WANT_POINTS = {"high": 65.0, "med": 40.0, "medium": 40.0, "low": 15.0, "none": 0.0}

# --- equip slots (ACE EquipMask) and coverage (ACE CoverageMask) -------------------
SLOT_NECK, SLOT_WRISTS, SLOT_FINGERS, SLOT_TRINKET, SLOT_CLOAK = 0x8000, 0x30000, 0xC0000, 0x4000000, 0x8000000
SLOT_BODY = 0x7FFF                      # every clothing and armour slot
COVER_CLOAK = 0x20000
# (label, coverage bits) in the order a person reads their paper doll.
BODY_ROWS = (
    ("Head", 0x4000), ("Chest", 0x400), ("Abdomen", 0x800), ("Upper arms", 0x1000),
    ("Lower arms", 0x2000), ("Hands", 0x8000), ("Upper legs", 0x100), ("Lower legs", 0x200),
    ("Feet", 0x10000), ("Shirt", 0x8 | 0x20 | 0x40), ("Pants", 0x2 | 0x4 | 0x10), ("Cloak", COVER_CLOAK),
)
COVER_ALL = 0x3FFFE                     # every real coverage bit (bit 0 is "unknown")
# Equip slot -> coverage, used only when an item's own coverage can't be found.
_SLOT_COVER = {
    0x1: 0x4000, 0x20: 0x8000, 0x100: 0x10000,
    0x2: 0x8, 0x4: 0x10, 0x8: 0x20, 0x10: 0x40, 0x40: 0x2, 0x80: 0x4,
    0x200: 0x400, 0x400: 0x800, 0x800: 0x1000, 0x1000: 0x2000, 0x2000: 0x100, 0x4000: 0x200,
}
JEWELRY = (("Necklace", "neck", 1), ("Trinket", "trinket", 1), ("Bracelet", "wrist", 2), ("Ring", "finger", 2))
# "Every slot filled" means these: one piece over each outer body part, a shirt
# (something covering the chest under the armour), pants (the upper legs), and
# the full count of each kind of jewellery. A cloak is not required.
FILL_BODY = (("Head", 0x4000), ("Chest", 0x400), ("Abdomen", 0x800), ("Upper arms", 0x1000), ("Lower arms", 0x2000),
             ("Hands", 0x8000), ("Upper legs", 0x100), ("Lower legs", 0x200), ("Feet", 0x10000),
             ("Shirt", 0x8), ("Pants", 0x2))
# Names accepted by lock_slots: a body row (any piece touching it) or a kind of jewellery.
SLOT_NAMES: dict[str, tuple[str, object]] = {
    **{label.lower().replace(" ", "-"): ("body", bits) for label, bits in BODY_ROWS},
    "arms": ("body", 0x1000 | 0x2000), "legs": ("body", 0x100 | 0x200), "robe": ("body", 0x400),
    "necklace": ("kind", "neck"), "neck": ("kind", "neck"), "trinket": ("kind", "trinket"),
    "bracelets": ("kind", "wrist"), "bracelet": ("kind", "wrist"), "wrists": ("kind", "wrist"),
    "rings": ("kind", "finger"), "ring": ("kind", "finger"), "fingers": ("kind", "finger"),
}

INT_COVERAGE, INT_LOCATIONS, INT_ARMOR_LEVEL, INT_ARCANE, INT_RANK = 4, 9, 28, 109, 110
# The client also publishes some qualities under the old Decal "LongValueKey" numbers.
DECAL_COVERAGE, DECAL_LOCATIONS = 218103821, 218103822
INT_SKILL_LEVEL_LIMIT, INT_APPRAISAL_SKILL, INT_SET = 115, 176, 265
_WIELD_TRIPLETS = ((158, 159, 160), (270, 271, 272), (273, 274, 275), (276, 277, 278))
_SKILL_NAMES = {
    6: "Melee Defense", 7: "Missile Defense", 14: "Arcane Lore", 15: "Magic Defense", 16: "Mana Conversion",
    31: "Creature Enchantment", 32: "Item Enchantment", 33: "Life Magic", 34: "War Magic", 41: "Two Handed Combat",
    43: "Void Magic", 44: "Heavy Weapons", 45: "Light Weapons", 46: "Finesse Weapons", 47: "Missile Weapons",
    48: "Shield", 49: "Dual Wield", 54: "Summoning",
}
_ATTRIBUTE_NAMES = {1: "Strength", 2: "Endurance", 3: "Quickness", 4: "Coordination", 5: "Focus", 6: "Self"}
_SET_NAMES = {
    13: "Soldier's", 14: "Adept's", 15: "Archer's", 16: "Defender's", 17: "Tinker's", 18: "Crafter's",
    19: "Hearty", 20: "Dexterous", 21: "Wise", 22: "Swift", 23: "Hardened", 24: "Reinforced",
    25: "Interlocking", 26: "Flameproof", 27: "Acidproof", 28: "Coldproof", 29: "Lightningproof",
}

# Tie-breakers, far below the smallest real difference (a Minor support cantrip is 5 points).
SKILL_ARCANE_LORE = 14
_HAVE = re.compile(r"\(have (-?\d+)\)")
_KEEP_ON_BONUS = 0.01      # all else equal, keep what is already worn
_PUT_ON_COST = 0.001       # ...and don't put on a piece that adds nothing (per slot it takes)
_PROBLEM_COST = 0.5        # all else equal, prefer a piece that works right now
_AL_BONUS = 0.0002         # when filling every slot: per point of armour level, per body part covered


class GearError(Exception):
    """Something the user needs to fix: no dump, unknown name, impossible lock."""


def level_factor(stem: str, level: int) -> float:
    if stem.endswith((" Ward", " Bane")):
        return _WARD_FACTORS[level]
    if stem in ("Armor", "Impenetrability"):
        return _ARMOR_FACTORS[level]
    return _SKILL_FACTORS[level]


def parse_cantrip(spell_name: str) -> tuple[str, int] | None:
    """"Major Focus" -> ("Focus", 2); None for anything that isn't a levelled cantrip."""
    m = _CANTRIP.match(spell_name.strip())
    if not m:
        return None
    stem = m.group(2)
    return _ALIASES.get(stem, stem), LEVELS.index(m.group(1))


def build_weights(attack: str, wants: dict[str, float] | None = None) -> dict[str, float]:
    """Points for a Major of each cantrip that matters, highest first.

    ``attack`` is a key of :data:`ATTACKS`. ``wants`` adds or overrides single
    cantrips (name -> points; 0 removes one) - the "I'd also like Arcane Lore
    covered" knob.
    """
    if attack not in ATTACKS:
        raise GearError(f"unknown attack skill {attack!r}; choose from {', '.join(ATTACKS)}")
    points = {stem: DEFAULT_POINTS["support"] for stem in (*SUPPORT, *_ATTACK_ATTRIBUTES[attack])}
    points.update({w: DEFAULT_POINTS["ward"] for w in WARDS})
    points.update({k: v for k, v in DEFAULT_POINTS.items() if k not in ("attack", "ward", "support")})
    points[ATTACKS[attack]] = DEFAULT_POINTS["attack"]
    for stem, value in (wants or {}).items():
        points[_ALIASES.get(stem, stem)] = float(value)
    ordered = sorted(((s, p) for s, p in points.items() if p > 0), key=lambda sp: (-sp[1], sp[0]))
    return dict(ordered)


def parse_want(text: str, known: set[str] | None = None) -> tuple[str, float]:
    """``"Arcane Prowess=high"`` -> ("Arcane Prowess", 65.0). Bare names mean ``med``."""
    name, _, level = text.partition("=")
    name, level = name.strip(), (level.strip().lower() or "med")
    if known is not None:
        canon = {k.lower(): k for k in known}
        if name.lower() not in canon:
            raise GearError(f"unknown cantrip {name!r}; e.g. {', '.join(sorted(known)[:8])}...")
        name = canon[name.lower()]
    if level in WANT_POINTS:
        return name, WANT_POINTS[level]
    try:
        return name, float(level)
    except ValueError:
        raise GearError(f"{text!r}: priority must be high, med, low, none or a number of points") from None


# --- the dump -------------------------------------------------------------------------

@dataclass
class Item:
    id: int
    name: str
    wcid: int = 0
    item_type: int = 0
    slots: int = 0                  # equip slots it can go in (ACE ValidLocations)
    coverage: int = 0               # body coverage, 0 for jewellery and held things
    kind: str = "other"             # body | neck | wrist | finger | trinket | held | other
    equipped: bool = False
    identified: bool = True
    container: int = 0
    cantrips: dict[str, int] = field(default_factory=dict)   # stem -> level index
    spells: list[str] = field(default_factory=list)          # every spell name, cantrip or not
    ints: dict[int, float] = field(default_factory=dict)
    can_wield: bool | None = None   # the client's own verdict, when it gave one
    wield_reason: str = ""
    coverage_source: str = ""       # item | slots: where the coverage came from

    @property
    def armor_level(self) -> int:
        return int(self.ints.get(INT_ARMOR_LEVEL, 0))


@dataclass
class Dump:
    character: str
    server: str
    level: int
    player: int
    unix_time: int
    items: list[Item]
    containers: list[tuple[int, str]]          # packs, in panel order
    skills: dict[int, dict]                    # skill id -> {training, base, current}
    unidentified: int = 0
    assumed: dict[str, int] = field(default_factory=dict)   # skills overridden by assume()

    def where(self, item: Item) -> str:
        """Where to find the piece: "worn", "main pack" or "pack 3" (counting down the side of the panel)."""
        if item.equipped:
            return "worn"
        if item.container in (0, self.player):
            return "main pack"
        for n, (cid, name) in enumerate(self.containers, 1):
            if cid == item.container:
                return f"pack {n}" + (f" ({name})" if name and name.lower() != "pack" else "")
        return "a pack"

    def assume(self, skill: str, value: int) -> str:
        """Plan as if a skill's current value were ``value`` ("what if I buff Arcane Lore to 360?").

        Only activation is affected: wield requirements go by base skill and the
        client's own verdict, which no buff changes. Returns the skill's proper name.
        """
        by_name = {name.lower(): sid for sid, name in _SKILL_NAMES.items()}
        key = skill.strip().lower()
        hits = [sid for name, sid in by_name.items() if name == key] or [sid for name, sid in by_name.items() if name.startswith(key)]
        if len(hits) != 1:
            raise GearError(f"unknown skill {skill!r}; e.g. Arcane Lore, Melee Defense, Missile Defense, Magic Defense")
        self.skills.setdefault(hits[0], {})["current"] = int(value)
        self.assumed[_SKILL_NAMES[hits[0]]] = int(value)
        return _SKILL_NAMES[hits[0]]

    def skill(self, skill_id: int) -> int | None:
        """Current (buffed) skill when the dump was taken; None if the dump doesn't say."""
        s = self.skills.get(skill_id)
        if not s or "current" not in s:
            return None
        return int(s["current"])

    def problems(self, item: Item) -> list[str]:
        """Why this piece would not work for this character right now (empty = fine).

        "Can't wield" comes from the client's own check; "won't activate" is
        the server's activation rule applied to the skills in the dump.
        """
        out = []
        if item.can_wield is False:
            have = _HAVE.search(item.wield_reason)
            needs = wield_requirements(item) or item.wield_reason or "requirement not met"
            out.append(f"can't wield: needs {needs}" + (f" (have {have.group(1)})" if have else ""))
        arcane, have = int(item.ints.get(INT_ARCANE, 0)), self.skill(SKILL_ARCANE_LORE)
        if arcane and have is not None and have < arcane:
            out.append(f"won't activate: Arcane Lore {arcane} (have {have})")
        limit, skill_id = int(item.ints.get(INT_SKILL_LEVEL_LIMIT, 0)), int(item.ints.get(INT_APPRAISAL_SKILL, 0))
        have = self.skill(skill_id) if skill_id else None
        if limit and have is not None and have < limit:
            out.append(f"won't activate: {_SKILL_NAMES.get(skill_id, f'skill {skill_id}')} {limit} (have {have})")
        return out


def coverage_from_slots(slots: int) -> int:
    """Best guess at coverage from the equip-slot mask alone.

    Only used for an item that doesn't carry its own coverage (the AC:Unreal
    client normally supplies it). Outer armour maps slot for slot. Underwear
    needs two corrections: a top (it has the chest slot and no leg slot) does
    not cover the abdomen even though it claims the slot, and footwear that
    also claims the lower-leg slot covers only the feet. Checked against the
    stock item data, this agrees for more than 99 % of wearables.
    """
    cover = 0
    for slot, bit in _SLOT_COVER.items():
        if slots & slot:
            cover |= bit
    if slots & 0x2 and not slots & 0xC0:      # chest wear, no leg wear: a shirt
        cover &= ~0x10
    if slots & 0x100:                         # footwear
        cover &= ~0x4
    if slots & SLOT_CLOAK:
        cover |= COVER_CLOAK
    return cover


def _as_list(v) -> list:
    """A Lua array may come back as a list, an empty object, or an index-keyed object."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda k: int(k) if str(k).isdigit() else 0)]
    return []


def _int_map(v) -> dict[int, float]:
    out = {}
    if isinstance(v, dict):
        for k, x in v.items():
            if str(k).lstrip("-").isdigit() and isinstance(x, (int, float)) and not isinstance(x, bool):
                out[int(k)] = x
    return out


def item_from_record(rec: dict) -> Item:
    """One raw inventory record from the client -> :class:`Item`.

    Tolerant on purpose: the dump is the client's snapshot as-is and the
    client is a moving target, so every field is optional and has a fallback.
    """
    ints = _int_map(rec.get("int_properties"))
    wcid = int(rec.get("wcid") or 0)
    slots = int(rec.get("slots") or ints.get(INT_LOCATIONS) or ints.get(DECAL_LOCATIONS) or 0)
    item = Item(
        id=int(rec.get("id") or 0), name=str(rec.get("name") or "?"), wcid=wcid,
        item_type=int(rec.get("type") or 0), slots=slots,
        equipped=bool(rec.get("equipped")), identified=bool(rec.get("identified", True)),
        container=int(rec.get("container") or 0), ints=ints,
        can_wield=rec.get("can_wield") if isinstance(rec.get("can_wield"), bool) else None,
    )
    reason = rec.get("wield_reason")
    item.wield_reason = reason if isinstance(reason, str) else (json.dumps(reason) if reason else "")
    item.spells = [str(s) for s in _as_list(rec.get("spell_names"))]
    for name in item.spells:
        parsed = parse_cantrip(name)
        if parsed:
            stem, level = parsed
            item.cantrips[stem] = max(level, item.cantrips.get(stem, -1))

    if slots & SLOT_NECK:
        item.kind = "neck"
    elif slots & SLOT_WRISTS:
        item.kind = "wrist"
    elif slots & SLOT_FINGERS:
        item.kind = "finger"
    elif slots & SLOT_TRINKET:
        item.kind = "trinket"
    elif slots & (SLOT_BODY | SLOT_CLOAK):
        item.kind = "body"
        own = int(ints.get(INT_COVERAGE) or ints.get(DECAL_COVERAGE) or 0) & COVER_ALL
        if own:
            item.coverage, item.coverage_source = own, "item"
        else:
            item.coverage, item.coverage_source = coverage_from_slots(slots), "slots"
        if not item.coverage:
            item.kind = "other"
    elif item.equipped:
        item.kind = "held"
    return item


def _slug(text: str) -> str:
    return "".join(c if (c.isascii() and (c.isalnum() or c in "-_")) else "_" for c in text[:40]) or "unknown"


def list_dumps(dump_dir: str | Path) -> list[dict]:
    """Index files in the dump folder, newest first."""
    found = []
    for p in Path(dump_dir).glob("gear_*.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(j, dict) and j.get("kind") == "gear_index":
            j["_path"] = p
            found.append(j)
    return sorted(found, key=lambda j: -int(j.get("unix_time") or 0))


def load_dump(dump_dir: str | Path, character: str | None = None) -> Dump:
    """Read one character's dump (the newest one if no name is given)."""
    dump_dir = Path(dump_dir)
    indexes = list_dumps(dump_dir)
    if not indexes:
        raise GearError(f"no gear dump in {dump_dir} - in game, open the Cantrip Tailor window and press 'Scan + dump'")
    if character:
        indexes = [j for j in indexes if str(j.get("character", "")).lower() == character.lower()]
        if not indexes:
            names = sorted({str(j.get('character')) for j in list_dumps(dump_dir)})
            raise GearError(f"no gear dump for {character!r}; found: {', '.join(names)}")
    index = indexes[0]
    stem = index["_path"].stem
    items: list[Item] = []
    for n in range(1, int(index.get("chunks") or 0) + 1):
        path = dump_dir / f"{stem}_{n}.json"
        try:
            chunk = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise GearError(f"dump is incomplete: {path.name} ({e}); dump again") from None
        if chunk.get("dump_id") != index.get("dump_id"):
            raise GearError(f"{path.name} belongs to a different dump than {index['_path'].name}; dump again")
        items += [item_from_record(r) for r in _as_list(chunk.get("items")) if isinstance(r, dict)]
    skills = {}
    raw = index.get("skills")
    if isinstance(raw, dict):
        skills = {int(k): v for k, v in raw.items() if str(k).isdigit() and isinstance(v, dict)}
    containers = [(int(c.get("id") or 0), str(c.get("name") or "")) for c in _as_list(index.get("containers"))
                  if isinstance(c, dict)]
    return Dump(
        character=str(index.get("character") or ""), server=str(index.get("server") or ""),
        level=int(index.get("level") or 0), player=int(float(index.get("player") or 0)),
        unix_time=int(index.get("unix_time") or 0), items=items, containers=containers, skills=skills,
        unidentified=sum(1 for i in items if not i.identified),
    )


def guess_attack(dump: Dump) -> str | None:
    """The attack skill to build for: specialised beats trained, then the higher base skill."""
    best = None
    for skill_id, key in _ATTACK_SKILL_IDS.items():
        s = dump.skills.get(skill_id)
        if not s or int(s.get("training") or 0) < 2:
            continue
        rank = (int(s.get("training") or 0), float(s.get("base") or 0))
        if best is None or rank > best[0]:
            best = (rank, key)
    return best[1] if best else None


# --- the solver -----------------------------------------------------------------------

@dataclass
class Plan:
    chosen: list[Item]                       # what to wear (body + jewellery), in paper-doll order
    held: list[Item]                         # equipped hand/aetheria items whose cantrips were counted
    coverage: dict[str, tuple[int, Item]]    # weighted cantrip -> (best level, the piece supplying it)
    score: float
    weights: dict[str, float]
    put_on: list[Item]
    take_off: list[Item]
    bench: list[Item]                        # unused pieces that do carry something wanted
    no_value: list[Item]                     # unused pieces with nothing wanted on them
    unidentified: list[Item]
    unusable: list[Item]                     # chosen although they can't be worn, or won't activate, right now
    fill: bool = False                       # was "every slot filled" asked for
    unfillable: list[str] = field(default_factory=list)   # slots nothing in the dump could fill
    locked: list[Item] = field(default_factory=list)      # pieces forced in by lock / lock_slots
    empty_locks: list[str] = field(default_factory=list)  # locked slots with nothing worn in them

    def missing(self) -> list[str]:
        return [stem for stem in self.weights if stem not in self.coverage]


def _resources(item: Item) -> list[str]:
    """The things this piece uses up; no two chosen pieces may overdraw one."""
    if item.kind == "body":
        return [f"cover{bit}" for bit in range(1, 18) if item.coverage >> bit & 1]
    return [item.kind]


_CAPACITY = {"neck": 1, "trinket": 1, "wrist": 2, "finger": 2}


def _match(items: list[Item], needle: str) -> list[Item]:
    if needle.isdigit():
        return [i for i in items if i.id == int(needle)]
    return [i for i in items if needle.lower() in i.name.lower()]


def solve(dump: Dump, weights: dict[str, float], *, lock: list[str] | None = None,
          lock_slots: list[str] | None = None, exclude: list[str] | None = None,
          usable_only: bool = False, count_held: bool = True, fill: bool = False) -> Plan:
    """The best-scoring set of body and jewellery pieces. Exact, not a heuristic.

    * ``lock`` forces pieces in (each a name fragment or an item id);
      ``lock_slots`` forces in whatever is *currently worn* in the named slots
      (keys of :data:`SLOT_NAMES`: "trinket", "head", "rings"...). A locked
      piece stays even if ``usable_only`` would have dropped it.
    * ``exclude`` keeps pieces out.
    * ``usable_only`` drops pieces that can't be worn or whose spells wouldn't
      activate at the skills in the dump (see :meth:`Dump.problems`); by
      default requirements are information only.
    * ``count_held``: cantrips on whatever is in the hands count as had.
    * ``fill`` makes "every slot has something in it" (:data:`FILL_BODY` and
      the jewellery counts) come first: the set with the most slots filled
      wins, cantrips decide among those, and armour level decides what goes in
      a slot no cantrip cares about. Without it, such a slot is left free.
    """
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
    except ImportError:  # pragma: no cover
        raise GearError("the solver needs NumPy and SciPy: run `uv sync` (or `pip install scipy`)") from None

    wearable = [i for i in dump.items if i.kind in ("body", "neck", "wrist", "finger", "trinket")]
    held = [i for i in dump.items if i.kind == "held"] if count_held else []

    forced: dict[int, Item] = {}
    empty_locks: list[str] = []
    for needle in lock or []:
        hits = _match(wearable, needle)
        if not hits:
            raise GearError(f"--lock {needle!r} matches nothing wearable in the dump")
        if len(hits) > 1 and not all(h.equipped for h in hits):
            worn = [h for h in hits if h.equipped]
            if len(worn) != 1:
                names = ", ".join(sorted({f"{h.name} ({h.id})" for h in hits})[:6])
                raise GearError(f"--lock {needle!r} is ambiguous: {names}; use the id")
            hits = worn
        forced.update((i.id, i) for i in hits)
    for name in lock_slots or []:
        key = name.strip().lower().replace(" ", "-").replace("_", "-")
        if key not in SLOT_NAMES:
            raise GearError(f"unknown slot {name!r}; choose from {', '.join(sorted(SLOT_NAMES))}")
        how, what = SLOT_NAMES[key]
        hits = [i for i in wearable if i.equipped and
                (i.kind == what if how == "kind" else (i.kind == "body" and i.coverage & what))]
        if not hits:
            empty_locks.append(key)        # a standing "keep my trinket" with no trinket on: nothing to keep, not an error
        forced.update((i.id, i) for i in hits)

    out = set()
    for needle in exclude or []:
        hits = _match(wearable, needle)
        if not hits:
            raise GearError(f"--exclude {needle!r} matches nothing in the dump")
        clash = [i.name for i in hits if i.id in forced]
        if clash:
            raise GearError(f"{clash[0]} is both locked and excluded")
        out.update(i.id for i in hits)
    if usable_only:
        out.update(i.id for i in wearable if dump.problems(i) and i.id not in forced)
    cands = [i for i in wearable if i.id not in out]

    # What is already had from the hands, per cantrip.
    base: dict[str, tuple[int, Item]] = {}
    for h in held:
        for stem, level in h.cantrips.items():
            if stem in weights and level > base.get(stem, (-1, None))[0]:
                base[stem] = (level, h)

    # Variables: x[i] per candidate; y[k] per (cantrip, level) step above the
    # base; f[j] per slot unit to fill (only with ``fill``).
    steps: list[tuple[str, int, float]] = []          # (stem, level, extra points for reaching it)
    for stem, pts in weights.items():
        floor = base.get(stem, (-1, None))[0]
        levels = sorted({i.cantrips[stem] for i in cands if i.cantrips.get(stem, -1) > floor})
        prev = pts * level_factor(stem, floor) if floor >= 0 else 0.0
        for lv in levels:
            now = pts * level_factor(stem, lv)
            steps.append((stem, lv, now - prev))
            prev = now
    fills: list[tuple[list[int], int]] = []           # (candidates that can fill it, units wanted)
    unfillable: list[str] = []
    if fill:
        for label, bit in FILL_BODY:
            members = [k for k, i in enumerate(cands) if i.kind == "body" and i.coverage & bit]
            if members:
                fills.append((members, 1))
            else:
                unfillable.append(label)
        for label, kind, cap in JEWELRY:
            members = [k for k, i in enumerate(cands) if i.kind == kind]
            if len(members) < cap:
                unfillable.append(label if cap == 1 or not members else f"second {label.lower()}")
                if cap > 1 and not members:
                    unfillable.append(f"second {label.lower()}")
            if members:
                fills.append((members, min(cap, len(members))))
    n, m, nf = len(cands), len(steps), sum(units for _members, units in fills)
    size = n + m + nf
    # A filled slot must outweigh any amount of cantrip value, so that "most
    # slots filled" is decided first and cantrips only among equals.
    fill_bonus = 10.0 * (sum(gain for _s, _l, gain in steps) + 1.0)
    cost = np.zeros(size)
    for k, item in enumerate(cands):
        cost[k] = -_KEEP_ON_BONUS if item.equipped else _PUT_ON_COST * len(_resources(item))
        if dump.problems(item):
            cost[k] += _PROBLEM_COST
        if fill and item.kind == "body":
            cost[k] -= _AL_BONUS * item.armor_level * len(_resources(item))
    for k, (_stem, _lv, gain) in enumerate(steps):
        cost[n + k] = -gain
    cost[n + m:] = -fill_bonus

    rows, upper = [], []
    by_resource: dict[str, list[int]] = {}
    for k, item in enumerate(cands):
        for r in _resources(item):
            by_resource.setdefault(r, []).append(k)
    for r, members in by_resource.items():
        cap = _CAPACITY.get(r, 1)
        if len(members) > cap:
            row = np.zeros(size)
            row[members] = 1
            rows.append(row)
            upper.append(cap)
    for k, (stem, lv, _gain) in enumerate(steps):      # y <= number of chosen suppliers
        row = np.zeros(size)
        row[n + k] = 1
        for j, item in enumerate(cands):
            if item.cantrips.get(stem, -1) >= lv:
                row[j] = -1
        rows.append(row)
        upper.append(0)
    at = n + m
    for members, units in fills:                       # filled units <= number of chosen fillers
        row = np.zeros(size)
        row[at:at + units] = 1
        row[members] = -1
        rows.append(row)
        upper.append(0)
        at += units

    lower_bound = np.zeros(size)
    for k, item in enumerate(cands):
        if item.id in forced:
            lower_bound[k] = 1
    if size == 0:
        picked: list[Item] = []
    else:
        kwargs = {}
        if rows:
            kwargs["constraints"] = LinearConstraint(np.array(rows), -np.inf, np.array(upper))
        # mip_rel_gap 0: prove the optimum outright. The default (1e-4 of the
        # objective) is harmless normally but not next to a 10,000-point fill bonus.
        res = milp(cost, integrality=np.ones(size), bounds=Bounds(lower_bound, np.ones(size)),
                   options={"mip_rel_gap": 0.0}, **kwargs)
        if res.x is None:
            raise GearError("no set satisfies the locks: two locked pieces cover the same body part "
                            "(or there are more locked rings/bracelets than fingers/wrists)")
        picked = [item for k, item in enumerate(cands) if res.x[k] > 0.5]

    coverage = dict(base)
    for item in picked:
        for stem, level in item.cantrips.items():
            if stem in weights and level > coverage.get(stem, (-1, None))[0]:
                coverage[stem] = (level, item)
    score = sum(weights[s] * level_factor(s, lv) for s, (lv, _i) in coverage.items())
    picked_ids = {i.id for i in picked}
    rest = [i for i in wearable if i.id not in picked_ids]
    wanted = lambda i: any(s in weights for s in i.cantrips)   # noqa: E731
    return Plan(
        chosen=sorted(picked, key=_doll_order), held=held, coverage=coverage, score=score, weights=weights,
        put_on=sorted((i for i in picked if not i.equipped), key=_doll_order),
        take_off=[i for i in rest if i.equipped],
        bench=sorted((i for i in rest if not i.equipped and i.identified and wanted(i)),
                     key=lambda i: -item_points(i, weights)),
        no_value=sorted((i for i in rest if not i.equipped and i.identified and not wanted(i)), key=lambda i: i.name),
        unidentified=[i for i in wearable if not i.identified],
        unusable=sorted((i for i in picked if dump.problems(i)), key=_doll_order),
        fill=fill, unfillable=unfillable, locked=sorted((i for i in picked if i.id in forced), key=_doll_order),
        empty_locks=empty_locks,
    )


def item_points(item: Item, weights: dict[str, float]) -> float:
    """What a piece would be worth on its own - used to order the bench list."""
    return sum(weights[s] * level_factor(s, lv) for s, lv in item.cantrips.items() if s in weights)


def _doll_order(item: Item) -> tuple:
    if item.kind == "body":
        for n, (_label, bits) in enumerate(BODY_ROWS):
            if item.coverage & bits:
                return (0, n, item.name)
    return (1, [k for _l, k, _c in JEWELRY].index(item.kind) if item.kind in _CAPACITY else 9, item.name)


# --- the report -----------------------------------------------------------------------

def wield_requirements(item: Item) -> str:
    """What it takes to put the piece on: "level 150", "base Melee Defense 370"."""
    parts = []
    for req, key, value in _WIELD_TRIPLETS:
        kind, what, need = int(item.ints.get(req, 0)), int(item.ints.get(key, 0)), int(item.ints.get(value, 0))
        if kind == 7:
            parts.append(f"level {need}")
        elif kind in (1, 2):
            parts.append(f"{'base ' if kind == 2 else ''}{_SKILL_NAMES.get(what, f'skill {what}')} {need}")
        elif kind in (3, 4):
            parts.append(f"{'base ' if kind == 4 else ''}{_ATTRIBUTE_NAMES.get(what, f'attribute {what}')} {need}")
        elif kind:
            parts.append(f"requirement type {kind}: {need}")
    if item.ints.get(INT_RANK):
        parts.append(f"allegiance rank {int(item.ints[INT_RANK])}")
    return ", ".join(parts)


def activation_requirements(item: Item) -> str:
    """What it takes for the piece's spells to light up: "Arcane Lore 327, Melee Defense 284"."""
    parts = []
    if item.ints.get(INT_ARCANE):
        parts.append(f"Arcane Lore {int(item.ints[INT_ARCANE])}")
    if item.ints.get(INT_SKILL_LEVEL_LIMIT):
        skill = int(item.ints.get(INT_APPRAISAL_SKILL, 0))
        parts.append(f"{_SKILL_NAMES.get(skill, f'skill {skill}')} {int(item.ints[INT_SKILL_LEVEL_LIMIT])}")
    return ", ".join(parts)


def requirements(item: Item, dump: Dump | None = None) -> str:
    """Both kinds as one note; with a dump, anything not met right now is called out in capitals."""
    parts = []
    if wield_requirements(item):
        parts.append("wield: " + wield_requirements(item))
    if activation_requirements(item):
        parts.append("activate: " + activation_requirements(item))
    text = "; ".join(parts)
    problems = dump.problems(item) if dump else []
    if problems:
        text = " / ".join(p.upper() if n == 0 else p for n, p in enumerate(problems)) + (f" [{text}]" if text else "")
    return text


# --- the report -----------------------------------------------------------------------
# Every bit of the report that could be coloured is passed through a "paint"
# function as (kind, text). The default returns the text untouched, so the
# report is plain; the command swaps in one that wraps it in colour codes. The
# solver itself never knows what a colour is - it only says what a thing IS
# ("worn", "bad", "heading"...), and padding is done before painting so the
# columns line up either way.

def _plain(kind: str, text: str) -> str:
    return text


def age_text(seconds: float) -> str:
    """How long ago, in words a person would use: "just now", "25 minutes ago", "3 hours ago", "2 days ago"."""
    minutes = seconds / 60
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} minutes ago"
    if minutes < 36 * 60:
        return f"{minutes / 60:.0f} hours ago"
    return f"{minutes / 1440:.0f} days ago"


def _cantrip_text(item: Item, plan: Plan | None = None, weights: dict[str, float] | None = None, paint=_plain) -> str:
    """"*Major Focus, Minor Armor, (Minor Flame Ward)": * = counts in the plan, () = wanted but outdone."""
    weights = weights if weights is not None else (plan.weights if plan else {})
    bits = []
    for stem, level in sorted(item.cantrips.items(), key=lambda sl: (-weights.get(sl[0], 0) * level_factor(*sl), sl[0])):
        text = f"{LEVELS[level]} {stem}"
        if stem not in weights:
            text = paint("unwanted", text)
        elif plan:
            text = paint("counted", "*" + text) if plan.coverage.get(stem, (None, None))[1] is item else paint("outdone", f"({text})")
        bits.append(text)
    return ", ".join(bits) or (paint("warn", "not appraised") if not item.identified else paint("dim", "no cantrips"))


def _where_text(item: Item, dump: Dump, paint=_plain) -> str:
    return paint("worn" if item.equipped else "pack", f"[{dump.where(item)}]")


def _piece_line(item: Item, dump: Dump, plan: Plan | None, weights=None, paint=_plain) -> str:
    extras = []
    if plan and any(item is i for i in plan.locked):
        extras.append(paint("locked", "LOCKED"))
    if item.armor_level:
        extras.append(paint("dim", f"AL {item.armor_level}"))
    if item.ints.get(INT_SET):
        s = int(item.ints[INT_SET])
        extras.append(paint("dim", f"{_SET_NAMES[s]} set" if s in _SET_NAMES else f"set #{s}"))
    req = requirements(item, dump)
    if req:
        extras.append(paint("bad" if dump.problems(item) else "dim", req))
    return (f"{item.name}  {_where_text(item, dump, paint)}  {_cantrip_text(item, plan, weights, paint)}"
            + (f"   {paint('dim', '<')}{paint('dim', '; ').join(extras)}{paint('dim', '>')}" if extras else ""))


def report(dump: Dump, plan: Plan, attack: str, paint=_plain, now: float | None = None) -> str:
    """The plan as text: what to wear, what that covers, what changes, what's left over.

    ``paint(kind, text)`` decorates the parts worth colouring (see above);
    left alone, the result is plain text. ``now`` is the time to measure the
    dump's age from (default: this moment).
    """
    L: list[str] = []
    head = lambda word, rest="": paint("heading", word) + rest          # noqa: E731 - a section heading
    wearable = len(plan.chosen) + len(plan.take_off) + len(plan.bench) + len(plan.no_value)
    L.append(paint("title", f"{dump.character} - level {dump.level} - best set for {ATTACKS[attack]}"
                            + (" with every slot filled" if plan.fill else "")))
    if dump.unix_time:
        # A plan is only as fresh as the dump it was made from, and nothing else here would tell you.
        age = (time.time() if now is None else now) - dump.unix_time
        L.append(f"Dump taken {age_text(age)}." if age < 86400 else
                 paint("warn", f"Dump taken {age_text(age)} - if your packs have changed since, dump again."))
    if dump.assumed:
        L.append("Assuming " + ", ".join(f"{k} {v}" for k, v in dump.assumed.items()) + " when checking activation.")
    if plan.empty_locks:
        L.append(paint("warn", f"Note: nothing is worn in the locked slot(s) {', '.join(plan.empty_locks)}, "
                               "so there was nothing to keep there."))
    L.append(f"{wearable} wearable pieces in the dump"
             + (paint("warn", f", {len(plan.unidentified)} not appraised (their spells are unknown - rescan)") if plan.unidentified else "")
             + f".  Score {paint('score', f'{plan.score:.0f}')}.")
    L.append("")
    L.append(head("WEAR", paint("dim", "   (* = this piece supplies the cantrip in the plan, (...) = wanted but outdone by another piece)")))
    shown: set[int] = set()
    for label, bits in BODY_ROWS:
        pieces = [i for i in plan.chosen if i.kind == "body" and i.coverage & bits]
        if not pieces:
            if label != "Cloak":
                L.append(f"  {label:<11} -  " + (paint("bad", "NOTHING IN THE DUMP FITS HERE") if plan.fill else
                                               paint("dim", "nothing in the bags helps here: wear whatever has the best armour")))
            continue
        for item in pieces:
            L.append(f"  {label:<11} " + (paint("dim", f"(same piece: {item.name})") if item.id in shown
                                          else _piece_line(item, dump, plan, None, paint)))
            shown.add(item.id)
    for label, kind, cap in JEWELRY:
        pieces = [i for i in plan.chosen if i.kind == kind]
        for n in range(cap):
            tag = label + (f" {n + 1}" if cap > 1 else "")
            L.append(f"  {tag:<11} " + (_piece_line(pieces[n], dump, plan, None, paint) if n < len(pieces) else
                                        ("-  " + paint("bad", "NOTHING IN THE DUMP FITS HERE") if plan.fill
                                         else "-  " + paint("dim", "nothing helps here"))))
    if plan.held:
        L.append("  In hand (left as is, cantrips counted):")
        for item in plan.held:
            L.append(f"              {_piece_line(item, dump, plan, None, paint)}")

    L.append("")
    L.append(head("COVERAGE", ", in priority order"))
    for stem, pts in plan.weights.items():
        if stem in plan.coverage:
            level, item = plan.coverage[stem]
            L.append(f"  {stem:<32} {paint('level_' + LEVELS[level].lower(), f'{LEVELS[level]:<10}')} {item.name}")
        else:
            L.append("  " + paint("missing", f"{stem:<32} -"))
    L.append("")
    if plan.put_on or plan.take_off:
        L.append(head("CHANGES", f": put on {len(plan.put_on)}, take off {len(plan.take_off)}"))
        for item in plan.take_off:
            L.append(f"  {paint('takeoff', 'take off')}  {item.name}  ({_cantrip_text(item, None, plan.weights, paint)})")
        for item in plan.put_on:
            L.append(f"  {paint('puton', 'put on')}    {item.name}  {_where_text(item, dump, paint)}")
    else:
        L.append(head("CHANGES", ": " + paint("good", "none - what is being worn is already the best set.")))
    if plan.unusable:
        L.append("")
        L.append(paint("bad", f"WARNING: {len(plan.unusable)} chosen piece(s) would not work right now, going by the skills in the dump "
                              "(rerun with --usable-only to plan around them):"))
        L += [f"  {i.name}: {paint('bad', '; '.join(dump.problems(i)))}" for i in plan.unusable]
    if plan.bench:
        L.append("")
        L.append(head("SPARES", f" - {len(plan.bench)} unused piece(s) that carry something wanted (best first)"))
        L += [f"  {_piece_line(i, dump, None, plan.weights, paint)}" for i in plan.bench]
    if plan.no_value:
        L.append("")
        L.append(head("NOTHING WANTED", f" on these {len(plan.no_value)} piece(s) - sell, salvage or mule:"))
        L += [f"  {i.name}  {_where_text(i, dump, paint)}  {_cantrip_text(i, None, None, paint)}" for i in plan.no_value]
    if plan.unidentified:
        L.append("")
        L.append(paint("warn", "NOT APPRAISED (ignored above): ") + ", ".join(sorted(i.name for i in plan.unidentified)))
    return "\n".join(L)


def put_on_order(pieces: list[Item]) -> list[Item]:
    """The order to put pieces on so that as many as possible light up.

    An item's spells activate (or not) against Arcane Lore as it stands when
    the piece goes on. So pieces that raise it - Focus or Arcane Prowess
    cantrips - go first, and after them everything in rising order of the
    Arcane Lore it asks for. It is a sensible order, not a guarantee: the
    solver doesn't model how far each piece moves the skill.
    """
    def key(i: Item):
        raises = "Focus" in i.cantrips or "Arcane Prowess" in i.cantrips
        return (0 if raises else 1, int(i.ints.get(INT_ARCANE, 0)), _doll_order(i))
    return sorted(pieces, key=key)


def slot_label(item: Item) -> str:
    """Short name for where a piece goes: "Head", "Chest + arms", "Ring"."""
    if item.kind != "body":
        return next(label for label, kind, _cap in JEWELRY if kind == item.kind)
    rows = [label for label, bits in BODY_ROWS if item.coverage & bits]
    if "Chest" in rows and "Lower legs" in rows:
        return "Robe"
    for pair, whole in ((("Upper arms", "Lower arms"), "arms"), (("Upper legs", "Lower legs"), "legs")):
        if all(p in rows for p in pair):
            rows[rows.index(pair[0])] = whole
            rows.remove(pair[1])
    text = " + ".join(rows) or "Body"
    return text[0].upper() + text[1:].lower() if len(rows) > 1 else text[0].upper() + text[1:]


def plan_json(dump: Dump, plan: Plan, attack: str, generated: int = 0, options: str = "") -> dict:
    """The plan as data for the Plan tab of the in-game plugin.

    Display text is made here so the Lua stays a dumb list: one entry per
    piece with its id and pack (for the select-item button), the slot label,
    and the cantrips it is being worn for.
    """
    def piece(i: Item) -> dict:
        counted = [f"{LEVELS[plan.coverage[s][0]]} {s}" for s in plan.weights
                   if s in plan.coverage and plan.coverage[s][1] is i]          # most important first
        if any(i is k for k in plan.locked):
            why = "locked" + (": " + ", ".join(counted) if counted else "")
        elif counted:
            why = ", ".join(counted)
        elif i.equipped:
            why = "keep on - nothing better for this slot"
        else:
            why = "fills the slot" + (f" (AL {i.armor_level})" if i.armor_level else "")
        return {"id": i.id, "container": i.container, "name": i.name, "slot": slot_label(i),
                "where": dump.where(i), "gives": why, "order": order[i.id],
                "spells": _cantrip_text(i, None, plan.weights), "note": "; ".join(dump.problems(i))}
    order = {i.id: n for n, i in enumerate(put_on_order(plan.chosen), 1)}
    lines = []
    for stem in plan.weights:
        if stem in plan.coverage:
            level, item = plan.coverage[stem]
            lines.append(f"{LEVELS[level]} {stem} - {item.name}")
    return {
        "kind": "gear_plan", "plan_id": generated, "character": dump.character, "server": dump.server,
        "attack": ATTACKS[attack], "dump_id": dump.unix_time, "score": round(plan.score), "options": options,
        "wear": [piece(i) for i in plan.chosen],
        "take_off": [{"id": i.id, "name": i.name, "slot": slot_label(i)} for i in plan.take_off],
        "coverage": lines, "missing": plan.missing(),
    }


def plan_name(character: str) -> str:
    """File name (without .json) the plugin looks for in its Plans folder."""
    return "plan_" + _slug(character)


# --- settings saved by the plugin's Settings tab ------------------------------------------

@dataclass
class Settings:
    """What a character asked for on the plugin's Settings tab.

    Only *changes* are stored: ``points`` holds the cantrips whose score was
    edited (0 = ignore it) or added, not the whole table, so the defaults
    here stay in charge of everything the player never touched.
    """
    attack: str | None = None                                  # None = work it out from the skills
    points: dict[str, float] = field(default_factory=dict)     # cantrip -> points for a Major
    usable_only: bool = False
    fill_slots: bool = False
    ignore_held: bool = False
    lock_slots: list[str] = field(default_factory=list)
    assume: dict[str, int] = field(default_factory=dict)       # skill name -> value to assume

    def describe(self) -> str:
        """One line saying what is set, for the top of the report."""
        bits = [f"attack {self.attack}" if self.attack else "attack automatic"]
        if self.points:
            bits.append(", ".join(f"{name} {pts:g}" for name, pts in sorted(self.points.items(), key=lambda kv: (-kv[1], kv[0]))))
        bits += [label for label, on in (("usable only", self.usable_only), ("fill slots", self.fill_slots),
                                         ("ignore held", self.ignore_held)) if on]
        if self.lock_slots:
            bits.append("lock " + ", ".join(self.lock_slots))
        bits += [f"assume {skill} {value}" for skill, value in self.assume.items()]
        return "; ".join(bits)


def settings_name(character: str) -> str:
    """File name (without .json) the plugin saves a character's settings under."""
    return "settings_" + _slug(character)


def load_settings(settings_dir: str | Path, character: str) -> Settings | None:
    """A character's saved settings, or None if they have never opened the Settings tab.

    Read tolerantly: the file is written by the plugin through the client, and
    an empty Lua table can arrive as ``[]`` or ``{}``. Anything unrecognised is
    dropped rather than trusted - a typo in a hand-edited file shouldn't stop
    the plan being made.
    """
    path = Path(settings_dir) / f"{settings_name(character)}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("kind") != "cantrip_tailor_settings":
        return None
    out = Settings()
    if raw.get("attack") in ATTACKS:
        out.attack = raw["attack"]
    points = raw.get("points")
    if isinstance(points, dict):
        out.points = {str(k): float(v) for k, v in points.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    out.usable_only, out.fill_slots, out.ignore_held = (raw.get(k) is True for k in ("usable_only", "fill_slots", "ignore_held"))
    out.lock_slots = [str(k) for k in _as_list(raw.get("lock_slots")) if str(k) in SLOT_NAMES]
    assume = raw.get("assume")
    if isinstance(assume, dict):
        out.assume = {str(k): int(v) for k, v in assume.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    return out
