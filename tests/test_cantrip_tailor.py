"""Tests for the solver and for the in-game plugin that feeds it.

The solver tests build tiny wardrobes by hand; two of them check the integer
programme against plain brute force on random wardrobes, which is what backs
the word "exact". The plugin tests run the real ``plugin/cantrip_tailor/
main.lua`` under a small pretend AC:Unreal host (via lupa) that appraises
items when asked, enforces the client's limits, and writes the files the
profiles service would write - and then the Python loader and solver read
them, end to end. They are skipped if lupa isn't installed.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from pathlib import Path

import pytest

from cantrip_tailor import cli
from cantrip_tailor import solver as g

# Everything the client's documentation says is missing from a plugin's Lua sandbox.
_BANNED = """
string.find, string.match, string.gmatch, string.gsub, string.dump = nil, nil, nil, nil, nil
pcall, xpcall, require, io, os, debug, coroutine, print = nil, nil, nil, nil, nil, nil, nil, nil
load, loadstring, dofile, loadfile, collectgarbage, setmetatable, getmetatable = nil, nil, nil, nil, nil, nil, nil
"""
PLAYER = 0x50000001          # a made-up character id; items in the main pack have it as their container

# Coverage bits (ACE CoverageMask) and equip slots (EquipMask) used below.
HEAD, HANDS, FEET = 0x4000, 0x8000, 0x10000
CHEST, ABDOMEN, UPPER_ARMS, LOWER_ARMS, UPPER_LEGS, LOWER_LEGS = 0x400, 0x800, 0x1000, 0x2000, 0x100, 0x200
HAUBERK = CHEST | ABDOMEN | UPPER_ARMS | LOWER_ARMS
ROBE = HAUBERK | UPPER_LEGS | LOWER_LEGS | FEET
RING_SLOTS, WRIST_SLOTS, NECK_SLOT = 0xC0000, 0x30000, 0x8000

_next_id = itertools.count(1000)


def body(name, coverage, *spells, equipped=False, **kw) -> g.Item:
    item = g.Item(id=next(_next_id), name=name, item_type=2, slots=0x200, coverage=coverage, kind="body",
                  equipped=equipped, **kw)
    return _with_spells(item, spells)


def jewel(name, kind, *spells, equipped=False) -> g.Item:
    return _with_spells(g.Item(id=next(_next_id), name=name, item_type=8, kind=kind, equipped=equipped), spells)


def _with_spells(item: g.Item, spells) -> g.Item:
    item.spells = list(spells)
    for s in spells:
        stem, level = g.parse_cantrip(s)
        item.cantrips[stem] = level
    return item


def dump_of(*items: g.Item) -> g.Dump:
    return g.Dump(character="Test", server="T", level=140, player=1, unix_time=0, items=list(items),
                  containers=[(50, "Backpack")], skills={})


VOID = g.build_weights("void")


def names(plan: g.Plan) -> list[str]:
    return sorted(i.name for i in plan.chosen)


# --- priorities ---------------------------------------------------------------------

def test_default_order_is_attack_buffs_defences_mana_wards():
    order = list(VOID)
    assert order[:7] == ["Void Magic Aptitude", "Creature Enchantment Aptitude", "Life Magic Aptitude",
                         "Item Enchantment Aptitude", "Invulnerability", "Magic Resistance", "Mana Conversion Prowess"]
    assert set(order[7:14]) == set(g.WARDS)
    assert set(order[14:]) == {"Armor", "Endurance", "Impregnability", "Focus", "Willpower", "Coordination", "Quickness"}
    # A different attack skill swaps the top line and its attributes; nothing else moves.
    bow = g.build_weights("missile")
    assert list(bow)[0] == "Missile Weapon Aptitude" and "Void Magic Aptitude" not in bow
    assert list(bow)[1:7] == order[1:7]
    assert "Strength" in g.build_weights("heavy") and "Strength" not in VOID
    with pytest.raises(g.GearError):
        g.build_weights("pirate")


def test_level_factors_follow_the_games_numbers():
    assert g.level_factor("Focus", g.LEVELS.index("Minor")) == pytest.approx(5 / 15)
    assert g.level_factor("Focus", g.LEVELS.index("Legendary")) == pytest.approx(35 / 15)
    assert g.level_factor("Flame Ward", g.LEVELS.index("Minor")) == pytest.approx(10 / 15)
    assert g.level_factor("Armor", g.LEVELS.index("Epic")) == pytest.approx(1.5)
    # "A Minor of something important sits in the middle": Minor Void beats a Major ward, not a Major defence.
    minor_void = VOID["Void Magic Aptitude"] * g.level_factor("Void Magic Aptitude", 0)
    assert VOID["Flame Ward"] < minor_void < VOID["Mana Conversion Prowess"]


def test_want_adds_and_removes_cantrips():
    known = set(g.KNOWN_CANTRIPS) | set(g.WARDS)
    assert g.parse_want("arcane prowess=high", known) == ("Arcane Prowess", 65.0)
    assert g.parse_want("Fletching Prowess", known) == ("Fletching Prowess", 40.0)
    assert g.parse_want("Summoning Prowess=95", known) == ("Summoning Prowess", 95.0)
    w = g.build_weights("void", {"Arcane Prowess": 65.0, "Quickness": 0.0, "Summoning Prowess": 95.0})
    order = list(w)
    assert order.index("Summoning Prowess") == 1                      # between void (100) and creature (90)
    assert order.index("Item Enchantment Aptitude") < order.index("Arcane Prowess") < order.index("Invulnerability")
    assert "Quickness" not in w
    for bad in ("Focuss=high", "Focus=sometimes"):
        with pytest.raises(g.GearError):
            g.parse_want(bad, known)


def test_parse_cantrip():
    assert g.parse_cantrip("Major Focus") == ("Focus", 2)
    assert g.parse_cantrip("Epic Bludgeon Ward") == ("Bludgeoning Ward", 3)     # both spellings exist
    assert g.parse_cantrip("Moderate Void Magic Aptitude") == ("Void Magic Aptitude", 1)
    assert g.parse_cantrip("Strength Self VII") is None and g.parse_cantrip("Impenetrability VI") is None


# --- the solver ---------------------------------------------------------------------

def test_picks_the_higher_priority_piece_for_a_slot():
    plan = g.solve(dump_of(body("Focus Helm", HEAD, "Major Focus"), body("Void Helm", HEAD, "Major Void Magic Aptitude")), VOID)
    assert names(plan) == ["Void Helm"]
    assert plan.coverage["Void Magic Aptitude"][0] == g.MAJOR and plan.score == pytest.approx(100)
    assert [i.name for i in plan.bench] == ["Focus Helm"] and plan.put_on == plan.chosen


def test_a_cantrip_counts_once_so_duplicates_are_spread():
    # Two Major Void pieces and a Major Life helm: wearing both Voids wastes the head slot.
    plan = g.solve(dump_of(body("Void Helm", HEAD, "Major Void Magic Aptitude"),
                           body("Life Helm", HEAD, "Major Life Magic Aptitude"),
                           body("Void Gloves", HANDS, "Major Void Magic Aptitude")), VOID)
    assert names(plan) == ["Life Helm", "Void Gloves"]
    assert plan.score == pytest.approx(180)


def test_a_higher_level_makes_the_lower_one_pointless():
    plan = g.solve(dump_of(body("Minor Helm", HEAD, "Minor Focus"), body("Major Gloves", HANDS, "Major Focus"),
                           body("Epic Boots", FEET, "Epic Focus")), VOID)
    assert names(plan) == ["Epic Boots"]                       # the other two add nothing, so they stay in the bag
    assert plan.score == pytest.approx(15 * 25 / 15)


def test_multi_slot_piece_against_single_pieces():
    hauberk = body("Hauberk", HAUBERK, "Major Life Magic Aptitude")                     # 80, but blocks four parts
    pieces = [body("Breastplate", CHEST, "Major Item Enchantment Aptitude"),             # 70
              body("Girth", ABDOMEN, "Major Flame Ward"),                                # 30
              body("Bracers", LOWER_ARMS, "Minor Life Magic Aptitude")]                  # 26.7
    assert names(g.solve(dump_of(hauberk, *pieces), VOID)) == ["Bracers", "Breastplate", "Girth"]
    assert names(g.solve(dump_of(hauberk, pieces[0]), VOID)) == ["Hauberk"]              # 80 > 70
    # A robe covers everything outer; it wins only if it beats the armour underneath it put together.
    robe = body("Starter Robe", ROBE, "Minor Armor", equipped=True)
    plan = g.solve(dump_of(robe, *pieces), VOID)
    assert "Starter Robe" not in names(plan) and plan.take_off == [robe]


def test_shirt_and_pants_do_not_clash_but_two_shirts_do():
    # A tunic and pants both claim the abdomen *slot*; only the pants *cover* it, and coverage is what counts.
    tunic = g.item_from_record({"id": 1, "name": "Tunic", "type": 4, "slots": 0xE, "identified": True,
                                "spell_names": ["Major Focus"], "int_properties": {"4": 0x28}})
    pants = g.item_from_record({"id": 2, "name": "Pants", "type": 4, "slots": 0xC4, "identified": True,
                                "spell_names": ["Major Willpower"], "int_properties": {"218103821": 0x16}})   # the Decal key works too
    shirt = g.item_from_record({"id": 3, "name": "Shirt", "type": 4, "slots": 0x1E, "identified": True,
                                "spell_names": ["Minor Focus", "Minor Armor"]})                               # no coverage given at all
    assert (tunic.coverage, tunic.coverage_source) == (0x28, "item")
    assert (pants.coverage, pants.coverage_source) == (0x16, "item")
    assert (shirt.coverage, shirt.coverage_source) == (0x68, "slots")
    assert names(g.solve(dump_of(tunic, pants, shirt), VOID)) == ["Pants", "Tunic"]


def test_coverage_can_be_worked_out_from_the_slots():
    # (equip slots, coverage) for the common shapes of stock clothing and armour.
    shapes = {
        0x1: HEAD, 0x20: HANDS, 0x100: FEET, 0x200: CHEST, 0x400: ABDOMEN, 0x800: UPPER_ARMS, 0x1000: LOWER_ARMS,
        0x2000: UPPER_LEGS, 0x4000: LOWER_LEGS,
        0x600: CHEST | ABDOMEN,                              # cuirass
        0x1800: UPPER_ARMS | LOWER_ARMS,                     # sleeves
        0x1A00: CHEST | UPPER_ARMS | LOWER_ARMS,             # coat
        0x1E00: HAUBERK,
        0x6000: UPPER_LEGS | LOWER_LEGS,                     # leggings
        0x6400: ABDOMEN | UPPER_LEGS | LOWER_LEGS,           # leggings with a girth
        0x7F00: ROBE,
        0x180: FEET,                                         # boots that also claim the lower-leg slot
        0xE: 0x28, 0x1E: 0x68,                               # tunic, shirt: no abdomen
        0xC4: 0x16, 0x44: 0x12,                              # pants, breeches: abdomen and legs
        0xDE: 0x7E,                                          # raiment: everything under the armour
        0x8000000: 0x20000,                                  # cloak
    }
    for slots, coverage in shapes.items():
        assert g.coverage_from_slots(slots) == coverage, hex(slots)


def test_jewellery_goes_by_count():
    rings = [jewel("Ring A", "finger", "Major Life Magic Aptitude"), jewel("Ring B", "finger", "Major Flame Ward"),
             jewel("Ring C", "finger", "Major Item Enchantment Aptitude"), jewel("Ring D", "finger", "Minor Focus")]
    necks = [jewel("Amulet", "neck", "Major Frost Ward"), jewel("Gorget", "neck", "Major Magic Resistance")]
    plan = g.solve(dump_of(*rings, *necks, jewel("Bracelet", "wrist", "Minor Armor")), VOID)
    assert names(plan) == ["Bracelet", "Gorget", "Ring A", "Ring C"]


def test_what_is_in_the_hand_counts_as_had():
    wand = _with_spells(g.Item(id=1, name="Nether Wand", item_type=0x8000, kind="held", equipped=True),
                        ["Major Void Magic Aptitude", "Minor Mana Conversion Prowess"])
    helms = [body("Void Helm", HEAD, "Major Void Magic Aptitude"), body("Ward Helm", HEAD, "Major Storm Ward")]
    plan = g.solve(dump_of(wand, *helms), VOID)
    assert names(plan) == ["Ward Helm"] and plan.coverage["Void Magic Aptitude"][1] is wand
    assert names(g.solve(dump_of(wand, *helms), VOID, count_held=False)) == ["Void Helm"]
    # An upgrade over the hand's level is still worth something.
    better = body("MC Gloves", HANDS, "Major Mana Conversion Prowess")
    plan = g.solve(dump_of(wand, better), VOID)
    assert names(plan) == ["MC Gloves"] and plan.score == pytest.approx(100 + 50)


def test_ties_keep_what_is_worn_and_leave_useless_pieces_in_the_bag():
    worn = body("Old Helm", HEAD, "Major Focus", equipped=True)
    same = body("New Helm", HEAD, "Major Focus")
    junk = body("Plain Gloves", HANDS)
    worn_junk = body("Plain Boots", FEET, equipped=True)
    plan = g.solve(dump_of(worn, same, junk, worn_junk), VOID)
    assert names(plan) == ["Old Helm", "Plain Boots"]
    assert plan.put_on == [] and plan.take_off == [] and [i.name for i in plan.no_value] == ["Plain Gloves"]
    assert "CHANGES: none" in g.report(dump_of(worn, same, junk, worn_junk), plan, "void")


def test_lock_exclude_and_usable_only():
    a = body("Void Helm", HEAD, "Major Void Magic Aptitude", can_wield=False, wield_reason="level 180")
    b = body("Focus Helm", HEAD, "Major Focus")
    c = body("Life Hauberk", HAUBERK, "Major Life Magic Aptitude")
    d = body("Item Breastplate", CHEST, "Minor Item Enchantment Aptitude")
    dump = dump_of(a, b, c, d)
    plan = g.solve(dump, VOID)
    assert names(plan) == ["Life Hauberk", "Void Helm"] and plan.unusable == [a]      # requirements are information only
    text = g.report(dump, plan, "void")
    assert "CAN'T WIELD: NEEDS LEVEL 180" in text and "WARNING: 1 chosen piece(s) would not work right now" in text
    assert names(g.solve(dump, VOID, usable_only=True)) == ["Focus Helm", "Life Hauberk"]
    assert names(g.solve(dump, VOID, exclude=["hauberk"])) == ["Item Breastplate", "Void Helm"]
    assert names(g.solve(dump, VOID, lock=["focus helm"])) == ["Focus Helm", "Life Hauberk"]
    assert names(g.solve(dump, VOID, lock=[str(d.id)])) == ["Item Breastplate", "Void Helm"]
    with pytest.raises(g.GearError, match="same body part"):
        g.solve(dump, VOID, lock=["hauberk", "breastplate"])
    with pytest.raises(g.GearError, match="ambiguous"):
        g.solve(dump, VOID, lock=["helm"])
    with pytest.raises(g.GearError, match="matches nothing"):
        g.solve(dump, VOID, exclude=["tiara"])


def test_unappraised_pieces_are_ignored_and_reported():
    known = body("Helm", HEAD, "Minor Focus")
    unknown = body("Mystery Helm", HEAD, identified=False)
    dump = dump_of(known, unknown)
    plan = g.solve(dump, VOID)
    assert names(plan) == ["Helm"] and plan.unidentified == [unknown]
    assert "NOT APPRAISED" in g.report(dump, plan, "void")


def _brute_force(items: list[g.Item], weights) -> float:
    best = 0.0
    for r in range(len(items) + 1):
        for combo in itertools.combinations(items, r):
            cover, counts, ok = 0, {}, True
            for i in combo:
                if i.kind == "body":
                    if cover & i.coverage:
                        ok = False
                        break
                    cover |= i.coverage
                else:
                    counts[i.kind] = counts.get(i.kind, 0) + 1
                    if counts[i.kind] > {"neck": 1, "trinket": 1, "wrist": 2, "finger": 2}[i.kind]:
                        ok = False
                        break
            if not ok:
                continue
            levels: dict[str, int] = {}
            for i in combo:
                for s, lv in i.cantrips.items():
                    levels[s] = max(lv, levels.get(s, -1))
            best = max(best, sum(weights[s] * g.level_factor(s, lv) for s, lv in levels.items() if s in weights))
    return best


def test_solver_matches_brute_force_on_random_wardrobes():
    rng = random.Random(20261115)
    shapes = [HEAD, HANDS, FEET, CHEST, ABDOMEN, UPPER_ARMS, LOWER_ARMS, UPPER_LEGS, LOWER_LEGS, HAUBERK,
              CHEST | ABDOMEN, UPPER_ARMS | LOWER_ARMS, ABDOMEN | UPPER_LEGS | LOWER_LEGS, ROBE, 0x28, 0x16]
    stems = list(VOID) + ["Strength", "Cooking Prowess"]
    for _ in range(40):
        items = []
        for n in range(rng.randint(6, 13)):
            spells = [f"{rng.choice(('Minor', 'Minor', 'Major', 'Epic'))} {s}" for s in rng.sample(stems, rng.randint(0, 3))]
            if rng.random() < 0.3:
                items.append(jewel(f"J{n}", rng.choice(("finger", "wrist", "neck", "trinket")), *spells))
            else:
                items.append(body(f"B{n}", rng.choice(shapes), *spells))
        plan = g.solve(dump_of(*items), VOID)
        assert plan.score == pytest.approx(_brute_force(items, VOID)), [(i.name, hex(i.coverage), i.spells) for i in items]


def test_report_shows_slots_coverage_changes_and_leftovers():
    robe = body("Starter Robe", ROBE, "Minor Armor", equipped=True)
    helm = body("Covenant Helm", HEAD, "Major Void Magic Aptitude", "Minor Flame Ward", container=50,
                ints={28: 420, 265: 14, 158: 7, 160: 150, 109: 250})
    coat = body("Chiran Coat", CHEST | UPPER_ARMS | LOWER_ARMS, "Major Life Magic Aptitude", "Major Flame Ward")
    ring = jewel("Ring", "finger", "Major Flame Ward")
    spare = body("Leather Helm", HEAD, "Minor Void Magic Aptitude")
    dump = dump_of(robe, helm, coat, ring, spare, body("Sollerets", FEET, "Minor Cooking Prowess"))
    plan = g.solve(dump, VOID)
    text = g.report(dump, plan, "void")
    assert "Test - level 140 - best set for Void Magic Aptitude" in text
    assert re.search(r"Head\s+Covenant Helm  \[pack 1 \(Backpack\)\]  \*Major Void Magic Aptitude, \(Minor Flame Ward\)"
                     r"   <AL 420; Adept's set; wield: level 150; activate: Arcane Lore 250>", text)
    assert re.search(r"Chest\s+Chiran Coat  \[main pack\]", text) and re.search(r"Upper arms\s+\(same piece: Chiran Coat\)", text)
    assert re.search(r"Abdomen\s+-  nothing in the bags helps here", text)
    assert re.search(r"Void Magic Aptitude\s+Major\s+Covenant Helm", text) and re.search(r"Creature Enchantment Aptitude\s+-\n", text)
    assert "take off  Starter Robe" in text and "put on    Covenant Helm  [pack 1 (Backpack)]" in text
    assert "SPARES - 2 unused" in text and "NOTHING WANTED on these 1 piece(s)" in text and "Sollerets" in text
    data = g.plan_json(dump, plan, "void")
    assert json.loads(json.dumps(data))["missing"][0] == "Creature Enchantment Aptitude"
    assert {w["name"] for w in data["wear"]} == {"Covenant Helm", "Chiran Coat"}      # one Flame Ward is enough: the ring stays off
    coat = next(w for w in data["wear"] if w["name"] == "Chiran Coat")
    assert coat["slot"] == "Chest + arms" and coat["gives"] == "Major Life Magic Aptitude, Major Flame Ward"
    assert data["take_off"] == [{"id": robe.id, "name": "Starter Robe", "slot": "Robe"}]
    assert "Major Void Magic Aptitude - Covenant Helm" in data["coverage"] and g.plan_name("Mage O'Test") == "plan_Mage_O_Test"
    labels = {g.slot_label(body("x", c)) for c in (HEAD, UPPER_ARMS, UPPER_LEGS | LOWER_LEGS, ABDOMEN | UPPER_LEGS | LOWER_LEGS, HAUBERK, 0x28, 0x16)}
    assert labels == {"Head", "Upper arms", "Legs", "Abdomen + legs", "Chest + abdomen + arms", "Shirt", "Pants"}
    assert g.slot_label(jewel("r", "finger")) == "Ring"


def test_activation_needs_current_skill_and_usable_only_plans_around_it():
    # The server lights an item's spells only if current Arcane Lore reaches its difficulty and,
    # where it names one, a defence skill reaches a level too. The dump has the skills.
    lore = body("High Lore Helm", HEAD, "Major Void Magic Aptitude", ints={109: 351})
    missile = body("Archer's Coif", HEAD, "Major Creature Enchantment Aptitude", ints={109: 197, 176: 7, 115: 217})
    fine = body("Plain Cowl", HEAD, "Major Focus", ints={109: 338, 176: 6, 115: 292})
    covenant = body("Covenant Gauntlets", HANDS, "Major Life Magic Aptitude", can_wield=False, ints={158: 2, 159: 6, 160: 370},
                    wield_reason="Wield requirement 1: base skill 6 requires 370 (have 307)")
    dump = dump_of(lore, missile, fine, covenant)
    dump.skills = {14: {"current": 340, "base": 279}, 6: {"current": 398}, 7: {"current": 128}}
    assert dump.problems(lore) == ["won't activate: Arcane Lore 351 (have 340)"]
    assert dump.problems(missile) == ["won't activate: Missile Defense 217 (have 128)"]
    assert dump.problems(fine) == []
    assert dump.problems(covenant) == ["can't wield: needs base Melee Defense 370 (have 307)"]
    assert g.requirements(fine) == "activate: Arcane Lore 338, Melee Defense 292"
    assert g.requirements(lore, dump) == "WON'T ACTIVATE: ARCANE LORE 351 (HAVE 340) [activate: Arcane Lore 351]"

    plan = g.solve(dump, VOID)                                   # information only: best on paper
    assert names(plan) == ["Covenant Gauntlets", "High Lore Helm"] and len(plan.unusable) == 2
    plan = g.solve(dump, VOID, usable_only=True)                 # ...and what actually works today
    assert names(plan) == ["Plain Cowl"] and plan.unusable == []
    # No skills in the dump: nothing can be judged, so nothing is called a problem.
    dump.skills = {}
    assert dump.problems(lore) == [] and dump.problems(covenant)


# --- the plugin, end to end ---------------------------------------------------------

PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "cantrip_tailor"


class GearHost:
    """Just enough of the AC:Unreal companion host to run the plugin's main.lua."""

    def __init__(self, inventory: list[dict], out_dir: Path, *, hidden: dict[int, list[str]] | None = None,
                 stubborn: set[int] = frozenset()):
        try:  # prefer Lua 5.4, the likeliest match for the client
            from lupa.lua54 import LuaRuntime
        except ImportError:  # pragma: no cover
            from lupa import LuaRuntime
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(_BANNED)
        self.tick_fn = self.lua.execute((PLUGIN / "main.lua").read_text(encoding="utf-8"))
        self.inventory, self.out_dir = inventory, out_dir
        self.hidden = hidden or {}        # item id -> spells revealed by an appraisal
        self.stubborn = stubborn          # ids the server never answers for
        self.time, self.events, self.identify_times, self.ui, self.files = 100.0, [], [], [], []
        self.plans_dir = out_dir / "Plans"
        self.settings_dir = out_dir / "Settings"
        self.selected: list[int] = []     # item ids the plugin's buttons selected
        self.character = "Mage O'Test"

    def _to_lua(self, v):
        if isinstance(v, dict):
            return self.lua.table_from({k: self._to_lua(x) for k, x in v.items()})
        if isinstance(v, list):
            return self.lua.table_from([self._to_lua(x) for x in v])
        return v

    def _from_lua(self, v):
        if type(v).__name__ != "_LuaTable":
            return v
        keys = list(v.keys())
        if keys and all(isinstance(k, int) for k in keys):
            assert sorted(keys) == list(range(1, len(keys) + 1)), "sparse array: the host would reject it"
            return [self._from_lua(v[i]) for i in range(1, len(keys) + 1)]
        assert all(isinstance(k, str) for k in keys), "mixed array/record table: the host would reject it"
        return {k: self._from_lua(x) for k, x in v.items()}

    def click(self, button: str) -> None:
        self.events.append({"type": "click", "id": button, "ok": True, "error": ""})

    def tick(self, seconds: float = 1.0) -> dict:
        self.time += seconds
        snapshot = {"time": self.time, "unix_time": 1794700000 + int(self.time), "player": float(PLAYER),
                    "server": "TestServer", "character_name": self.character, "level": 140, "error": "",
                    "skills": {"43": {"training": 3, "base": 347, "current": 340}, "47": {"training": 1, "base": 80, "current": 78}},
                    "inventory": self.inventory, "events": self.events}
        self.events = []
        out = self._from_lua(self.tick_fn(self._to_lua(snapshot), self._to_lua({}))) or {}
        self.ui = out.get("ui") or []
        self._check_ui()
        if out.get("action") == "identify":
            assert not self.identify_times or self.time - self.identify_times[-1] >= 1.0, "more than one appraisal a second"
            self.identify_times.append(self.time)
            for item in self.inventory:
                if item["id"] == out["target"] and item["id"] not in self.stubborn:
                    item["identified"] = True
                    item["spell_names"] = self.hidden.get(item["id"], [])
        else:
            assert "action" not in out
        requests = out.get("requests") or []
        assert len(requests) <= 4
        folders = {"Dumps": self.out_dir, "Plans": self.plans_dir, "Settings": self.settings_dir}
        for r in requests:
            assert r["service"] == "profiles" and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", r["name"]), r
            path = folders[r["group"]] / f"{r['name']}.json"
            if r["operation"] == "read":
                assert r["group"] in ("Plans", "Settings")
                if path.is_file():
                    self.events.append({"type": "result", "id": r["id"], "ok": True, "data": json.loads(path.read_text())})
                else:
                    self.events.append({"type": "result", "id": r["id"], "ok": False, "error": "profile not found"})
                continue
            assert r["operation"] == "save" and r["group"] in ("Dumps", "Settings")
            text = json.dumps(r["data"], allow_nan=False)          # must be JSON-clean
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if r["group"] == "Dumps":
                self.files.append(r["name"])
            self.events.append({"type": "result", "id": r["id"], "ok": True})
        return out

    def controls(self, rows=None):
        """Every control in the window, containers included, depth first."""
        for r in self.ui if rows is None else rows:
            yield r
            yield from self.controls(r.get("children") or [])

    def _check_ui(self) -> None:
        """The host's limits on a window: unique short ids, bounded text, a bounded number of controls."""
        ids = [c["id"] for c in self.controls()]
        assert len(ids) == len(set(ids)), "duplicate control id"
        assert len(ids) <= 256 and len(self.ui) <= 128 and all(len(i) <= 64 for i in ids)     # the client's limits
        assert all(len(c.get("text", "")) <= 512 and len(c.get("tooltip", "")) <= 512 for c in self.controls())

    def run_until_idle(self, limit: int = 300) -> None:
        for _ in range(limit):
            self.tick()
            if "Dump complete" in self.row("status").get("text", ""):
                return
        raise AssertionError("plugin never finished: " + self.row("status").get("text", ""))

    def row(self, row_id: str) -> dict:
        return next(c for c in self.controls() if c.get("id") == row_id)

    def has(self, row_id: str) -> bool:
        return any(c.get("id") == row_id for c in self.controls())

    def tab(self, title: str) -> list[dict]:
        return next(c["children"] for c in self.controls() if c.get("type") == "column" and c.get("text") == title)

    def press(self, row_id: str) -> None:
        """Click a button the way the client does: a select_item action is revalidated at click time."""
        b = self.row(row_id)
        assert b.get("type") == "button" and b.get("enabled", True)
        event = {"type": "click", "id": row_id, "ok": True, "error": ""}
        if b.get("action") == "select_item":
            item = next((i for i in self.inventory if i["id"] == b["target"]), None)
            if not item or item.get("equipped") or item.get("container") != b.get("container"):
                event.update(ok=False, error="item is not in that container")
            else:
                self.selected.append(item["id"])
        self.events.append(event)

    def change(self, row_id: str, value) -> None:
        """Edit a control the way the client does: it reports the new value and the plugin must adopt it."""
        c = self.row(row_id)
        assert c.get("type") in ("checkbox", "number", "select", "input"), c
        if c["type"] == "select":
            assert value in c["options"], (value, c["options"])
        if c["type"] == "number":
            assert c["min"] <= value <= c["max"]
        self.events.append({"type": "change", "id": row_id, "value": value})

    def points(self) -> dict[str, float]:
        """The Settings tab's score table as shown: cantrip -> points."""
        out = {}
        for c in self.controls():
            if c["id"].startswith("pt_"):
                out[c["text"].split("  (")[0]] = c["value"]
        return out

    def wear(self, item_id: int) -> None:
        item = next(i for i in self.inventory if i["id"] == item_id)
        item.update(equipped=True, container=0)

    def remove(self, item_id: int) -> None:
        item = next(i for i in self.inventory if i["id"] == item_id)
        item.update(equipped=False, container=PLAYER)


def _inventory() -> list[dict]:
    return [
        {"id": 50, "wcid": 136, "name": "Backpack", "type": 0x200, "identified": True, "container": PLAYER},
        {"id": 10, "wcid": 5850, "name": "Faran Robe", "type": 4, "slots": 0x7F00, "equipped": True, "equipped_slot": 0x7F00,
         "identified": True, "spell_names": ["Minor Armor"], "int_properties": {"4": 0x13F00, "28": 150, "19": 5000}},
        {"id": 11, "wcid": 99999, "name": "Nether Wand", "type": 0x8000, "slots": 0x1000000, "equipped": True,
         "identified": True, "spell_names": ["Major Void Magic Aptitude"]},
        {"id": 12, "wcid": 21152, "name": "Covenant Breastplate", "type": 2, "slots": 0x200, "identified": False, "container": 50,
         "int_properties": {"4": 0x400}},
        {"id": 13, "wcid": 297, "name": "Ring", "type": 8, "slots": 0xC0000, "identified": False, "container": PLAYER},
        {"id": 14, "wcid": 134, "name": "Tunic", "type": 4, "slots": 0xE, "identified": True, "container": 50,
         "spell_names": ["Major Creature Enchantment Aptitude"], "can_wield": True,
         "string_properties": {"16": "x" * 5000}, "float_properties": {"13": 1.3}},
        {"id": 15, "wcid": 1, "name": "Flaming Sword", "type": 1, "slots": 0x100000, "identified": False, "container": 50},
        {"id": 16, "wcid": 8331, "name": "Silver Pea", "type": 0x1000, "identified": True, "container": 50, "count": 4},
    ]


@pytest.fixture
def lua_host(tmp_path):
    pytest.importorskip("lupa")

    def make(inventory, **kw):
        return GearHost(inventory, tmp_path, **kw)
    return make


def test_plugin_scans_dumps_and_the_solver_reads_it(lua_host, tmp_path):
    host = lua_host(_inventory(), hidden={12: ["Major Life Magic Aptitude", "Minor Flame Bane"], 13: ["Major Storm Ward"]})
    host.tick()
    assert "gear in total: 5" in host.row("counts")["text"] and "not appraised yet: 2" in host.row("counts")["text"]
    host.click("scan")
    host.run_until_idle()
    assert len(host.identify_times) == 2                              # the two unappraised wearables, not the sword
    assert host.files == ["gear_Mage_O_Test_1", "gear_Mage_O_Test"]   # items first, index last

    dump = g.load_dump(tmp_path)
    assert (dump.character, dump.level, dump.player, dump.unidentified) == ("Mage O'Test", 140, PLAYER, 0)
    assert sorted(i.name for i in dump.items) == ["Covenant Breastplate", "Faran Robe", "Nether Wand", "Ring", "Tunic"]
    assert dump.containers == [(50, "Backpack")] and g.guess_attack(dump) == "void"
    by_name = {i.name: i for i in dump.items}
    assert by_name["Nether Wand"].kind == "held" and by_name["Ring"].kind == "finger"
    assert by_name["Covenant Breastplate"].cantrips == {"Life Magic Aptitude": 2, "Flame Bane": 0}
    assert by_name["Faran Robe"].armor_level == 150 and by_name["Faran Robe"].coverage_source == "item"
    assert dump.where(by_name["Tunic"]) == "pack 1 (Backpack)" and dump.where(by_name["Ring"]) == "main pack"
    raw = json.loads((tmp_path / "gear_Mage_O_Test_1.json").read_text())
    tunic = next(r for r in raw["items"] if r["name"] == "Tunic")
    assert len(tunic["string_properties"]["16"]) == 600 and tunic["float_properties"]["13"] == 1.3   # long text capped

    plan = g.solve(dump, g.build_weights("void"))
    assert names(plan) == ["Covenant Breastplate", "Ring", "Tunic"]   # robe off: breastplate 80 beats Minor Armor 7.5
    assert [i.name for i in plan.take_off] == ["Faran Robe"] and [i.name for i in plan.held] == ["Nether Wand"]


def test_plugin_gives_up_on_an_item_the_server_wont_appraise(lua_host, tmp_path):
    host = lua_host(_inventory(), hidden={13: ["Major Storm Ward"]}, stubborn={12})
    host.click("scan")
    host.run_until_idle()
    assert host.identify_times and sum(1 for _ in host.identify_times) == 1 + 3      # the ring once, the breastplate three times
    dump = g.load_dump(tmp_path)
    assert dump.unidentified == 1 and [i.name for i in g.solve(dump, VOID).unidentified] == ["Covenant Breastplate"]
    assert "1 without spell data" in host.row("status")["text"]


def test_plugin_dump_now_skips_appraisals_and_splits_big_inventories(lua_host, tmp_path):
    inv = _inventory() + [{"id": 100 + n, "wcid": 297, "name": f"Ring {n}", "type": 8, "slots": 0xC0000,
                           "identified": True, "spell_names": ["Minor Focus"], "container": 50} for n in range(40)]
    host = lua_host(inv)
    host.click("dump")
    host.run_until_idle()
    assert host.identify_times == []
    assert host.files == ["gear_Mage_O_Test_1", "gear_Mage_O_Test_2", "gear_Mage_O_Test_3", "gear_Mage_O_Test"]
    dump = g.load_dump(tmp_path, "mage o'test")
    assert len(dump.items) == 45 and dump.unidentified == 2
    with pytest.raises(g.GearError, match="no gear dump for"):
        g.load_dump(tmp_path, "Somebody Else")
    # A second, smaller dump must not be confused by the first one's leftover files.
    host2 = lua_host(_inventory())
    host2.time = 5000.0
    host2.click("dump")
    host2.run_until_idle()
    assert len(g.load_dump(tmp_path).items) == 5
    (tmp_path / "gear_Mage_O_Test_1.json").write_text(json.dumps({"dump_id": 1, "items": []}))
    with pytest.raises(g.GearError, match="different dump"):
        g.load_dump(tmp_path)


def test_no_dump_is_a_helpful_error(tmp_path):
    with pytest.raises(g.GearError, match="Scan \\+ dump"):
        g.load_dump(tmp_path)


def test_plan_tab_lists_the_set_selects_pieces_and_ticks_them_off(lua_host, tmp_path):
    host = lua_host(_inventory(), hidden={12: ["Major Life Magic Aptitude"], 13: ["Major Storm Ward"]})
    host.tick()
    assert "No plan for Mage O'Test yet" in host.tab("Plan")[0]["text"] or "Looking for a plan" in host.tab("Plan")[0]["text"]
    host.click("scan")
    host.run_until_idle()

    # The desktop half: read the dump, solve, write the plan where the plugin looks.
    dump = g.load_dump(tmp_path)
    plan = g.solve(dump, g.build_weights("void"))
    host.plans_dir.mkdir()
    (host.plans_dir / f"{g.plan_name(dump.character)}.json").write_text(
        json.dumps(g.plan_json(dump, plan, "void", generated=111)), encoding="utf-8")
    for _ in range(17):                     # found on the next 15-second look, no button needed
        host.tick()
    assert "best set for Void Magic Aptitude" in host.row("p_title")["text"]
    assert host.row("p_sum")["text"] == "0 of 3 on,  3 to put on,  1 to take off first"
    assert host.row("off1")["text"] == "TAKE OFF  Faran Robe  (Robe)"
    texts = {c["id"]: c["text"] for c in host.controls() if c["id"].startswith("wt")}
    assert texts["wt1"] == "PUT ON  Covenant Breastplate  [pack 1]  -  Major Life Magic Aptitude"
    assert "PUT ON  Ring  [main pack]  -  Major Storm Ward" in texts.values()
    assert [c["text"] for c in host.row("p_cover")["children"]][:2] == [
        "Major Void Magic Aptitude - Nether Wand", "Major Creature Enchantment Aptitude - Tunic"]

    # "Select next" picks the first piece still in a pack; wearing it moves the list on.
    assert host.row("next")["text"] == "Select next:  Covenant Breastplate  [pack 1]"
    host.press("next")
    host.tick()
    assert host.selected == [12] and not host.has("p_click")
    host.remove(10)                         # robe off...
    host.wear(12)                           # ...breastplate on
    host.tick()
    assert host.row("p_sum")["text"] == "1 of 3 on,  2 to put on" and not host.has("off1")
    assert host.row("wt1")["text"].startswith("on  Covenant Breastplate") and host.row("wb1")["enabled"] is False
    # Each piece has its own select button too.
    ring_button = next(c for c in host.controls() if c.get("action") == "select_item" and c.get("target") == 13 and c["id"] != "next")
    host.press(ring_button["id"])
    host.wear(13)
    host.wear(14)
    host.tick()
    assert host.selected == [12, 13]
    assert host.row("p_sum")["text"] == "All 3 pieces are on. Done." and not host.has("next")
    # A piece that has left the inventory is called out, not silently dropped.
    host.inventory[:] = [i for i in host.inventory if i["id"] != 13]
    host.tick()
    assert "1 no longer in the inventory" in host.row("p_sum")["text"]


def test_plan_tab_reports_a_failed_selection_and_ignores_another_characters_plan(lua_host, tmp_path):
    host = lua_host(_inventory())
    host.click("dump")
    host.run_until_idle()
    dump = g.load_dump(tmp_path)
    plan = g.solve(dump, g.build_weights("void"))
    host.plans_dir.mkdir()
    path = host.plans_dir / f"{g.plan_name(dump.character)}.json"
    path.write_text(json.dumps(g.plan_json(dump, plan, "void", generated=1)), encoding="utf-8")
    host.click("reload")
    host.tick()
    host.tick()
    assert host.row("next")["text"].startswith("Select next:  Tunic")
    # The piece is moved to another pack between the window being drawn and the click.
    next(i for i in host.inventory if i["id"] == 14)["container"] = 4242
    host.press("next")
    host.tick()
    assert "Couldn't select that piece" in host.row("p_click")["text"]
    # Same file name, different character inside: don't show someone else's set.
    other = lua_host(_inventory())
    data = g.plan_json(dump, plan, "void", generated=2)
    data["character"] = "Somebody Else"
    path.write_text(json.dumps(data), encoding="utf-8")
    other.tick()
    other.tick()
    assert "is for Somebody Else" in other.tab("Plan")[0]["text"]


# --- every slot filled, locked slots, assumed skills ---------------------------------

def _filled_units(pieces) -> int:
    cover = 0
    for i in pieces:
        if i.kind == "body":
            cover |= i.coverage
    units = sum(1 for _label, bit in g.FILL_BODY if cover & bit)
    return units + sum(min(cap, sum(1 for i in pieces if i.kind == kind)) for _label, kind, cap in g.JEWELRY)


def test_fill_puts_something_in_every_slot_and_uses_armour_level_for_the_rest():
    helm = body("Void Helm", HEAD, "Major Void Magic Aptitude", ints={28: 200})
    thin = body("Thin Gloves", HANDS, ints={28: 150})
    thick = body("Thick Gloves", HANDS, ints={28: 420})
    ring = jewel("Plain Ring", "finger")
    dump = dump_of(helm, thin, thick, ring)
    assert names(g.solve(dump, VOID)) == ["Void Helm"]                      # default: useless pieces stay in the bag
    plan = g.solve(dump, VOID, fill=True)
    assert names(plan) == ["Plain Ring", "Thick Gloves", "Void Helm"]       # filled, by armour level where nothing else decides
    assert plan.score == pytest.approx(100) and plan.fill
    assert plan.unfillable == ["Chest", "Abdomen", "Upper arms", "Lower arms", "Upper legs", "Lower legs", "Feet",
                               "Shirt", "Pants", "Necklace", "Trinket", "Bracelet", "second bracelet", "second ring"]
    text = g.report(dump, plan, "void")
    assert "with every slot filled" in text and re.search(r"Chest\s+-  NOTHING IN THE DUMP FITS HERE", text)
    data = g.plan_json(dump, plan, "void")
    assert next(w for w in data["wear"] if w["name"] == "Thick Gloves")["gives"] == "fills the slot (AL 420)"


def test_fill_comes_before_cantrips():
    # The hood-and-gloves piece has nothing on it but fills two slots; the helm fills one.
    helm = body("Void Helm", HEAD, "Major Void Magic Aptitude")
    guise = body("Hooded Mitts", HEAD | HANDS)
    assert names(g.solve(dump_of(helm, guise), VOID, fill=True)) == ["Hooded Mitts"]
    # ...but given gloves as well, both slots fill either way and the cantrip decides again.
    assert names(g.solve(dump_of(helm, guise, body("Gloves", HANDS)), VOID, fill=True)) == ["Gloves", "Void Helm"]


def test_fill_matches_brute_force_on_random_wardrobes():
    rng = random.Random(7)
    shapes = [HEAD, HANDS, FEET, CHEST, ABDOMEN, UPPER_ARMS, LOWER_ARMS, UPPER_LEGS, LOWER_LEGS, HAUBERK,
              CHEST | ABDOMEN, ABDOMEN | UPPER_LEGS | LOWER_LEGS, ROBE, 0x28, 0x16, HEAD | HANDS]
    for _ in range(25):
        items = []
        for n in range(rng.randint(6, 12)):
            spells = [f"{rng.choice(('Minor', 'Major', 'Epic'))} {s}" for s in rng.sample(list(VOID), rng.randint(0, 2))]
            if rng.random() < 0.3:
                items.append(jewel(f"J{n}", rng.choice(("finger", "wrist", "neck", "trinket")), *spells))
            else:
                items.append(body(f"B{n}", rng.choice(shapes), *spells))
        best = (-1, -1.0)
        for r in range(len(items) + 1):
            for combo in itertools.combinations(items, r):
                cover, ok, counts = 0, True, {}
                for i in combo:
                    if i.kind == "body":
                        ok = ok and not cover & i.coverage
                        cover |= i.coverage
                    else:
                        counts[i.kind] = counts.get(i.kind, 0) + 1
                        ok = ok and counts[i.kind] <= {"neck": 1, "trinket": 1, "wrist": 2, "finger": 2}[i.kind]
                if ok:
                    levels: dict[str, int] = {}
                    for i in combo:
                        for s, lv in i.cantrips.items():
                            levels[s] = max(lv, levels.get(s, -1))
                    score = sum(VOID[s] * g.level_factor(s, lv) for s, lv in levels.items())
                    best = max(best, (_filled_units(combo), round(score, 6)))
        plan = g.solve(dump_of(*items), VOID, fill=True)
        assert (_filled_units(plan.chosen), round(plan.score, 6)) == best


def test_lock_slot_keeps_what_is_worn_there():
    pathwarden = jewel("Pathwarden Trinket", "trinket", equipped=True)       # no cantrips; worn for another reason
    scarab = jewel("Mechanical Scarab", "trinket", "Major Slashing Ward")
    old_helm = body("Old Helm", HEAD, "Minor Focus", equipped=True)
    new_helm = body("New Helm", HEAD, "Major Void Magic Aptitude")
    dump = dump_of(pathwarden, scarab, old_helm, new_helm)
    assert names(g.solve(dump, VOID)) == ["Mechanical Scarab", "New Helm"]
    plan = g.solve(dump, VOID, lock_slots=["trinket"])
    assert names(plan) == ["New Helm", "Pathwarden Trinket"] and plan.locked == [pathwarden] and plan.take_off == [old_helm]
    assert "<LOCKED>" in g.report(dump, plan, "void")
    assert g.plan_json(dump, plan, "void")["wear"][1]["gives"] == "locked"
    assert names(g.solve(dump, VOID, lock_slots=["Trinket", "head"])) == ["Old Helm", "Pathwarden Trinket"]
    # A locked piece stays even when --usable-only would have dropped it; excluding it as well is a contradiction.
    pathwarden.can_wield = False
    assert "Pathwarden Trinket" in names(g.solve(dump, VOID, lock_slots=["trinket"], usable_only=True))
    with pytest.raises(g.GearError, match="both locked and excluded"):
        g.solve(dump, VOID, lock_slots=["trinket"], exclude=["pathwarden"])
    plan = g.solve(dump, VOID, lock_slots=["rings"])            # nothing worn there: noted, not fatal
    assert plan.empty_locks == ["rings"] and "nothing is worn in the locked slot(s) rings" in g.report(dump, plan, "void")
    with pytest.raises(g.GearError, match="unknown slot"):
        g.solve(dump, VOID, lock_slots=["tail"])


def test_assumed_skill_changes_what_counts_as_usable():
    lore = body("High Lore Helm", HEAD, "Major Void Magic Aptitude", ints={109: 351})
    fine = body("Plain Cowl", HEAD, "Major Focus", ints={109: 300})
    dump = dump_of(lore, fine)
    dump.skills = {14: {"current": 340}}
    assert names(g.solve(dump, VOID, usable_only=True)) == ["Plain Cowl"]
    assert dump.assume("arcane lore", 360) == "Arcane Lore"
    assert names(g.solve(dump, VOID, usable_only=True)) == ["High Lore Helm"]
    assert "Assuming Arcane Lore 360" in g.report(dump, g.solve(dump, VOID), "void")
    with pytest.raises(g.GearError):
        dump.assume("juggling", 10)


def test_put_on_order_raises_arcane_lore_first_then_climbs():
    a = body("Hard Helm", HEAD, "Major Void Magic Aptitude", ints={109: 351})
    b = body("Easy Boots", FEET, "Major Flame Ward", ints={109: 150})
    c = jewel("Focus Ring", "finger", "Major Focus")
    c.ints = {109: 320}
    d = jewel("Lore Scarab", "trinket", "Major Arcane Prowess")
    d.ints = {109: 258}
    assert [i.name for i in g.put_on_order([a, b, c, d])] == ["Lore Scarab", "Focus Ring", "Easy Boots", "Hard Helm"]


def test_select_next_follows_the_plans_put_on_order(lua_host, tmp_path):
    host = lua_host(_inventory(), hidden={12: ["Major Life Magic Aptitude"], 13: ["Major Focus"]})
    host.click("scan")
    host.run_until_idle()
    dump = g.load_dump(tmp_path)
    plan = g.solve(dump, g.build_weights("void"))
    data = g.plan_json(dump, plan, "void", generated=5)
    assert [w["name"] for w in sorted(data["wear"], key=lambda w: w["order"])][0] == "Ring"   # it raises Arcane Lore
    host.plans_dir.mkdir()
    (host.plans_dir / f"{g.plan_name(dump.character)}.json").write_text(json.dumps(data), encoding="utf-8")
    host.click("reload")
    host.tick()
    host.tick()
    assert host.row("next")["text"] == "Select next:  Ring  [main pack]"     # not the breastplate, which is first on the doll
    assert host.row("wt1")["text"].startswith("PUT ON  Covenant Breastplate")


# --- the command ---------------------------------------------------------------------

def test_command_finds_the_dump_writes_the_plan_and_explains_a_missing_folder(lua_host, tmp_path, capsys):
    library = tmp_path / "client" / "Saved" / "ClientPlugins" / "Profiles" / "cantrip_tailor" / "Library"
    (library / "Dumps").mkdir(parents=True)
    host = GearHost(_inventory(), library / "Dumps", hidden={12: ["Major Life Magic Aptitude"], 13: ["Major Storm Ward"]})
    host.plans_dir = library / "Plans"
    host.click("scan")
    host.run_until_idle()

    assert cli.main(["--client-dir", str(tmp_path / "client"), "--list"]) == 0
    assert "Mage O'Test  level 140  5 items" in capsys.readouterr().out
    assert cli.main(["--client-dir", str(tmp_path / "client"), "--lock-slot", "robe", "--fill-slots"]) == 0
    out = capsys.readouterr().out
    assert "best set for Void Magic Aptitude with every slot filled" in out and "Plan written:" in out
    plan = json.loads((library / "Plans" / "plan_Mage_O_Test.json").read_text())
    assert plan["options"] == "attack automatic; fill slots; lock robe" and plan["attack"] == "Void Magic Aptitude"
    for _ in range(17):                    # ...and the plugin picks that file up by itself
        host.tick()
    assert "best set for Void Magic Aptitude" in host.row("p_title")["text"]

    assert cli.main(["--library", str(library), "--show-weights"]) == 0
    assert "100  Void Magic Aptitude" in capsys.readouterr().out
    assert cli.main(["--library", str(library), "--want", "Focuss=high"]) == 2
    assert "unknown cantrip" in capsys.readouterr().err
    assert cli.main(["--client-dir", str(tmp_path / "nowhere")]) == 2
    assert "--client-dir" in capsys.readouterr().err


# --- the Settings tab ----------------------------------------------------------------

ATTACK_LABELS = {"void": "Void Magic", "war": "War Magic", "missile": "Missile Weapons", "heavy": "Heavy Weapons",
                 "light": "Light Weapons", "finesse": "Finesse Weapons", "two-handed": "Two Handed Combat",
                 "summoning": "Summoning"}


def _settled(host: GearHost, ticks: int = 6) -> GearHost:
    for _ in range(ticks):
        host.tick()
    return host


def test_settings_tab_shows_the_same_defaults_the_solver_uses(lua_host):
    host = lua_host(_inventory())
    host.tick()
    assert host.tab("Settings")[0]["text"] == "Reading this character's settings..."
    _settled(host)                                           # no file yet: after a second look, defaults
    assert host.row("attack")["value"] == "Automatic (from your skills)"
    assert host.row("s_guess")["text"] == "Automatic picks Void Magic for this character."      # void is specialised in the test skills
    assert host.points() == g.build_weights("void")
    assert set(ATTACK_LABELS) == set(g.ATTACKS)
    for key, label in ATTACK_LABELS.items():                 # the plugin's copy of the table, against the real one
        host.change("attack", label)
        host.tick()
        assert host.points() == g.build_weights(key), key
    # Every slot it offers to lock is one the solver can lock.
    locks = [c["id"][3:] for c in host.controls() if c["id"].startswith("lk_")]
    assert len(locks) == 16 and set(locks) <= set(g.SLOT_NAMES)
    # Everything it offers to add is a name the solver knows, and nothing is offered twice.
    offered = host.row("add_name")["options"]
    assert set(offered) | set(host.points()) == set(g.KNOWN_CANTRIPS) | set(g.WARDS)
    assert not set(offered) & set(host.points())


def test_settings_are_saved_per_character_and_the_command_uses_them(lua_host, tmp_path, capsys):
    library = tmp_path / "client" / "Saved" / "ClientPlugins" / "Profiles" / "cantrip_tailor" / "Library"
    (library / "Dumps").mkdir(parents=True)
    inventory = _inventory() + [{"id": 20, "name": "Pathwarden Trinket", "type": 8, "slots": 0x4000000, "equipped": True,
                                 "identified": True, "spell_names": []},
                                {"id": 21, "name": "Mechanical Scarab", "type": 8, "slots": 0x4000000, "identified": True,
                                 "container": PLAYER, "spell_names": ["Major Slashing Ward"]}]
    host = GearHost(inventory, library / "Dumps")
    host.plans_dir, host.settings_dir = library / "Plans", library / "Settings"
    _settled(host)
    # What is worn shows beside each lockable slot.
    assert host.row("lk_trinket")["text"] == "Trinket:  Pathwarden Trinket"
    assert host.row("lk_chest")["text"] == "Chest:  Faran Robe" and host.row("lk_rings")["text"] == "Rings:  (nothing worn)"
    assert host.row("as_14")["text"] == "Arcane Lore" and host.row("usable")["value"] is False

    host.change("attack", "Missile Weapons")
    host.change("pt_Quickness", 0)                           # ignore one
    host.change("pt_Focus", 65)                              # raise one
    host.change("add_name", "Summoning Prowess")             # add one
    host.change("add_pts", 95)
    host.tick()
    host.press("add_go")
    host.change("usable", True)
    host.change("fill", True)
    host.change("lk_trinket", True)
    host.change("as_14", 360)
    host.tick()
    assert host.row("s_note")["text"] == "Not saved yet..."
    _settled(host)
    assert host.row("s_note")["text"].startswith("Saved.")
    shown = host.points()
    assert shown["Summoning Prowess"] == 95 and shown["Quickness"] == 0 and shown["Focus"] == 65
    assert list(shown)[0] == "Summoning Prowess"             # what you added is listed first
    assert host.row("pt_Focus")["text"] == "Focus  (default 15)" and host.row("pt_Summoning_Prowess")["text"] == "Summoning Prowess  (added)"

    saved = g.load_settings(library / "Settings", "Mage O'Test")
    assert saved == g.Settings(attack="missile", points={"Quickness": 0.0, "Focus": 65.0, "Summoning Prowess": 95.0},
                               usable_only=True, fill_slots=True, ignore_held=False, lock_slots=["trinket"],
                               assume={"Arcane Lore": 360})

    # The command needs no options now...
    host.click("dump")
    host.run_until_idle()
    assert cli.main(["--client-dir", str(tmp_path / "client")]) == 0
    out = capsys.readouterr().out
    assert "Using this character's saved settings" in out
    assert ("Options: attack missile; Summoning Prowess 95, Focus 65, Quickness 0; usable only; fill slots; "
            "lock trinket; assume Arcane Lore 360") in out
    assert "best set for Missile Weapon Aptitude with every slot filled" in out and "Assuming Arcane Lore 360" in out
    assert re.search(r"Trinket\s+Pathwarden Trinket  \[worn\]  no cantrips   <LOCKED>", out)
    # ...but what you type still wins, and --no-settings ignores the file.
    assert cli.main(["--client-dir", str(tmp_path / "client"), "--attack", "void", "--want", "Quickness=high"]) == 0
    assert "Options: attack void; Summoning Prowess 95, Focus 65, Quickness 65; usable only" in capsys.readouterr().out
    assert cli.main(["--client-dir", str(tmp_path / "client"), "--no-settings"]) == 0
    out = capsys.readouterr().out
    assert "Options: attack automatic\n" in out and "best set for Void Magic Aptitude\n" in out
    assert re.search(r"Trinket\s+Mechanical Scarab", out)

    # Next login: the tab comes back as it was left.
    again = GearHost(inventory, library / "Dumps")
    again.settings_dir = library / "Settings"
    _settled(again, 3)
    assert again.row("attack")["value"] == "Missile Weapons" and again.row("lk_trinket")["value"] is True
    assert again.row("as_14")["value"] == 360 and again.points()["Summoning Prowess"] == 95
    assert not again.has("s_guess")


def test_settings_only_remember_what_was_changed(lua_host, tmp_path):
    host = _settled(lua_host(_inventory()))
    host.change("pt_Focus", 65)
    host.change("add_name", "Arcane Prowess")
    host.tick()
    host.press("add_go")                                     # at the suggested 40 points
    _settled(host)
    stored = json.loads((host.settings_dir / "settings_Mage_O_Test.json").read_text())
    assert stored["points"] == {"Focus": 65, "Arcane Prowess": 40} and stored["attack"] == "auto"
    host.change("pt_Focus", 15)                              # back to its default: nothing left to remember
    host.change("pt_Arcane_Prowess", 0)                      # an added cantrip set to 0 just goes away
    _settled(host)
    assert not g.load_settings(host.settings_dir, "Mage O'Test").points
    assert "Arcane Prowess" not in host.points() and "Arcane Prowess" in host.row("add_name")["options"]
    host.change("pt_Focus", 80)
    host.tick()
    host.press("reset")
    _settled(host)
    assert host.points() == g.build_weights("void") and not g.load_settings(host.settings_dir, "Mage O'Test").points


def test_saved_settings_are_read_tolerantly(tmp_path):
    name = g.settings_name("Mage O'Test") + ".json"
    assert g.load_settings(tmp_path, "Mage O'Test") is None                      # never saved
    (tmp_path / name).write_text("not json")
    assert g.load_settings(tmp_path, "Mage O'Test") is None
    (tmp_path / name).write_text(json.dumps({
        "kind": "cantrip_tailor_settings", "attack": "pirate", "points": [], "lock_slots": {}, "assume": {"Arcane Lore": 0},
        "usable_only": "yes"}))
    assert g.load_settings(tmp_path, "Mage O'Test") == g.Settings()              # junk is dropped, not trusted
    (tmp_path / name).write_text(json.dumps({
        "kind": "cantrip_tailor_settings", "attack": "war", "points": {"Focus": 70, "Bad": -5},
        "lock_slots": ["rings", "tail"], "fill_slots": True}))
    s = g.load_settings(tmp_path, "Mage O'Test")
    assert (s.attack, s.points, s.lock_slots, s.fill_slots) == ("war", {"Focus": 70.0}, ["rings"], True)
    assert s.describe() == "attack war; Focus 70; fill slots; lock rings"


def test_whole_window_stays_inside_the_clients_control_limit(lua_host, tmp_path):
    # A full set on the Plan tab and every settings row at once must still fit in 256 controls.
    inventory = _inventory()
    shapes = [("Helm", 0x1, 0x4000), ("Breastplate", 0x200, 0x400), ("Girth", 0x400, 0x800), ("Pauldrons", 0x800, 0x1000),
              ("Bracers", 0x1000, 0x2000), ("Gauntlets", 0x20, 0x8000), ("Tassets", 0x2000, 0x100), ("Greaves", 0x4000, 0x200),
              ("Boots", 0x100, 0x10000), ("Shirt", 0x1E, 0x68), ("Pants", 0xC4, 0x16)]
    stems = list(g.build_weights("void"))
    for n, (name, slots, cover) in enumerate(shapes):
        inventory.append({"id": 200 + n, "name": name, "type": 2, "slots": slots, "identified": True, "container": PLAYER,
                          "int_properties": {"4": cover}, "spell_names": [f"Major {stems[n]}", f"Major {stems[(n + 10) % len(stems)]}"]})
    for n, slots in enumerate([0x8000, 0x4000000, 0x30000, 0x30000, 0xC0000, 0xC0000]):
        inventory.append({"id": 300 + n, "name": f"Jewel {n}", "type": 8, "slots": slots, "identified": True,
                          "container": PLAYER, "spell_names": [f"Epic {stems[n]}"]})
    host = lua_host(inventory)
    host.click("dump")
    host.run_until_idle()
    dump = g.load_dump(tmp_path)
    plan = g.solve(dump, g.build_weights("void"), fill=True)
    assert len(plan.chosen) == 17
    host.plans_dir.mkdir()
    (host.plans_dir / f"{g.plan_name(dump.character)}.json").write_text(json.dumps(g.plan_json(dump, plan, "void", 9)))
    host.click("reload")
    _settled(host)                                          # every tick re-checks the limits (see _check_ui)
    assert "17" in host.row("p_sum")["text"]
    assert 120 < sum(1 for _ in host.controls()) <= 256


# --- the report: dump age and colour ---------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_age_is_said_the_way_a_person_would():
    said = {30: "just now", 25 * 60: "25 minutes ago", 80 * 60: "80 minutes ago", 3 * 3600: "3 hours ago",
            30 * 3600: "30 hours ago", 3 * 86400: "3 days ago", -500: "just now"}       # a clock a little off is "just now"
    assert {secs: g.age_text(secs) for secs in said} == said


def test_report_says_how_old_the_dump_is():
    dump = dump_of(body("Cowl", HEAD, "Major Focus", equipped=True))
    plan = g.solve(dump, VOID)
    assert "Dump taken" not in g.report(dump, plan, "void")                 # a dump with no time on it: say nothing
    dump.unix_time = 1_000_000
    lines = g.report(dump, plan, "void", now=1_000_000 + 40 * 60).splitlines()
    assert lines[1] == "Dump taken 40 minutes ago."                         # straight under the title
    old = g.report(dump, plan, "void", now=1_000_000 + 3 * 86400)
    assert "Dump taken 3 days ago - if your packs have changed since, dump again." in old


def test_colour_only_decorates_the_report_it_never_changes_it():
    worn = body("Cowl", HEAD, "Major Focus", "Minor Fealty", equipped=True)
    better = body("Helm", HEAD, "Epic Focus", "Minor Invulnerability")
    coat = body("Coat", CHEST, "Major Invulnerability", ints={g.INT_ARCANE: 900})       # its spells won't activate
    spare = jewel("Ring", "finger", "Minor Focus")
    junk = jewel("Bangle", "wrist", "Minor Fealty")
    kept = jewel("Chain", "neck", "Major Life Magic Aptitude", equipped=True)
    dump = dump_of(worn, better, coat, spare, junk, kept)
    dump.skills = {14: {"base": 100, "training": 2, "current": 100}}                       # Arcane Lore 100
    dump.unix_time = 1_000_000
    plan = g.solve(dump, VOID, lock_slots=["rings"])
    plain = g.report(dump, plan, "void", now=1_000_500)
    seen = []
    marked = g.report(dump, plan, "void", lambda kind, text: seen.append(kind) or f"<{kind}|{text}>", now=1_000_500)
    assert re.sub(r"<[a-z_]+\|", "", marked).replace(">", "") == plain.replace(">", "")
    coloured = g.report(dump, plan, "void", cli.ansi, now=1_000_500)
    assert ANSI.sub("", coloured) == plain and "\x1b[1;36mWEAR\x1b[0m" in coloured
    assert set(seen) <= set(cli.COLORS), set(seen) - set(cli.COLORS)        # every kind the report uses has a colour
    for kind in ("title", "heading", "score", "worn", "pack", "counted", "outdone", "unwanted", "dim", "bad",
                 "warn", "missing", "puton", "takeoff", "level_epic", "level_major"):
        assert kind in seen, kind
    assert "\x1b[1;31mWARNING: 1 chosen piece(s)" in coloured


def test_command_colours_only_when_asked_or_on_a_console(lua_host, tmp_path, capsys, monkeypatch):
    library = tmp_path / "client" / "Saved" / "ClientPlugins" / "Profiles" / "cantrip_tailor" / "Library"
    (library / "Dumps").mkdir(parents=True)
    host = GearHost(_inventory(), library / "Dumps")
    host.click("dump")
    host.run_until_idle()
    base = ["--client-dir", str(tmp_path / "client"), "--no-plan"]
    assert cli.main(base) == 0
    plain = capsys.readouterr().out                                         # pytest's capture is not a console
    assert "\x1b[" not in plain and re.search(r"^Dump taken \d+ (hours|days) ago", plain, re.M)
    report_file = tmp_path / "report.txt"
    assert cli.main(base + ["--color", "always", "-o", str(report_file)]) == 0
    coloured = capsys.readouterr().out
    assert "\x1b[1;36mCOVERAGE\x1b[0m" in coloured and ANSI.sub("", coloured) == plain
    assert report_file.read_text(encoding="utf-8") == plain                # the file stays plain

    class Console:
        def isatty(self): return True
    monkeypatch.setattr(cli.os, "name", "posix")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert cli.use_color("auto", Console()) and not cli.use_color("never", Console())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not cli.use_color("auto", Console()) and cli.use_color("always", Console())
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(cli.os, "name", "nt")                               # an old Windows console that says no
    monkeypatch.setattr(cli, "_windows_console_takes_ansi", lambda: False)
    assert not cli.use_color("auto", Console())
