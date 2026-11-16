# How Cantrip Tailor works

## The problem

You have *n* wearable pieces. Each occupies some slots and carries some
cantrips at some level. You want the set that maximises

> the sum, over the cantrips you care about, of the points for the **best
> level** of that cantrip anywhere in the set

subject to the rules of what can be worn together. "Best level" is what makes
it awkward: a piece's value isn't fixed, it depends on what else you're
wearing. A second Major Focus is worth nothing; a Major Focus next to a Minor
Focus makes the Minor worthless.

## Why not a genetic algorithm (or any search)

Because the problem is small and has exact structure, so it can simply be
*solved*. A search - genetic, annealing, greedy-plus-swaps - returns a good
set and no way to know whether a better one exists. Two experiments on real
inventories of ~85 pieces made the point:

- a quick greedy + local-search matched the true optimum in only about a
  quarter of cases, and was up to 60 points short;
- a hand-written exact branch-and-bound did find the optimum but needed
  0.5-3 million search steps to prove it.

An integer-programming solver proves the optimum in milliseconds.

## The formulation

One yes/no variable per piece: **x<sub>i</sub>** = "wear piece *i*".

**Wearing rules** are linear constraints:

- for every coverage bit (outer chest, outer upper arms, ..., underwear
  chest, ..., head, hands, feet, cloak): the pieces covering it sum to ≤ 1;
- necklaces ≤ 1, trinkets ≤ 1, bracelets ≤ 2, rings ≤ 2.

**"Best level" objective.** For each cantrip *c* and each level *l* that
appears on some piece, one more yes/no variable **y<sub>c,l</sub>** = "the set
has *c* at level *l* or better", allowed to be 1 only if a chosen piece
supplies it:

> y<sub>c,l</sub> ≤ Σ x<sub>i</sub> over pieces *i* carrying *c* at level ≥ *l*

Its objective coefficient is the *extra* points for reaching level *l* over
the next level down. Summing the steps a set reaches gives exactly the points
for its best level, and because every step is worth something the solver
turns each one on whenever it can.

**Held items** (wand, weapon, shield, aetheria) aren't variables: their
cantrips set the floor each cantrip's steps start from.

**Locks** are lower bounds (x<sub>i</sub> ≥ 1). **Exclusions** and
`--usable-only` remove the variable.

**`--fill-slots`** adds one yes/no per slot unit - "is this slot filled" -
allowed only if a chosen piece fills it, worth ten times the entire cantrip
table. That makes the comparison lexicographic: most slots filled first,
cantrips second. It is a bonus rather than a hard constraint so that a slot
nothing fits is reported instead of making the problem impossible. Armour
level enters as a tiny bonus (0.0002 per point, per body part) to choose
among fillers.

**Tie-breaks**, all far below the smallest real difference: keep what's
already on (+0.01), don't put on a piece that adds nothing (-0.001 per slot),
prefer a piece that works right now (-0.5 for one with an unmet requirement).

It is solved with `scipy.optimize.milp` (the HiGHS solver bundled with SciPy)
with the optimality gap set to zero, so the answer is proven, not
approximate. `tests/` checks it against brute-force enumeration on random
wardrobes, with and without `--fill-slots`.

## Where the wearing rules come from

From the ACEmulator server source:

- **Clashes** (`Creature_Equipment.GetEquippedItems`): clothing and armour
  conflict when their `ClothingPriority` - a coverage mask, PropertyInt 4 -
  overlaps. This is *not* the equip-slot mask (`ValidLocations`): a tunic
  claims the chest, abdomen and upper-arm slots but covers only chest and
  upper arms, which is why it can be worn with pants that cover the abdomen.
  The AC:Unreal client includes each item's coverage in the inventory
  snapshot; if it is ever missing, the solver falls back to a guess from the
  slot mask (`coverage_from_slots`).
- **Activation** (`WorldObject_Use.CheckUseRequirements`): current Arcane
  Lore ≥ the item's difficulty, and where the item names a skill, current
  skill ≥ its level. The client reports those as int properties 109, 176 and
  115.
- **Wield** requirements are judged by the client itself (`can_wield`), which
  the dump carries.

A server with different rules would need different checks; the cantrip
optimisation itself doesn't depend on them.

## Level weights

From the stock spell table: skill and attribute cantrips give +5 / +10 / +15 /
+25 / +35 (Minor / Moderate / Major / Epic / Legendary); wards reduce damage
by 10 / 15 / 20 / 25 %; Armor adds 20 / 40 / 60 / 80. Each level's points are
the Major's points scaled by those ratios.

## The files

All under `Saved\ClientPlugins\Profiles\cantrip_tailor\Library\`:

| file | written by | contents |
|---|---|---|
| `Dumps\gear_<character>_<n>.json` | plugin | 20 inventory records each, as the client supplies them |
| `Dumps\gear_<character>.json` | plugin, last | index: how many files, your packs in order, your skills |
| `Plans\plan_<character>.json` | command | the chosen pieces (id, pack, slot, what each is for, put-on order), what to take off, the coverage |
| `Settings\settings_<character>.json` | plugin | what was changed on the Settings tab: attack skill, edited or added scores, the rule tick-boxes, locked slots, assumed skills |

Every file of one dump carries the same `dump_id`, so a half-written or stale
dump is detected rather than half-read.

The settings file holds only *changes*. The default score table lives in the
command (`DEFAULT_POINTS` in `solver.py`); the plugin carries a copy purely so
the Settings tab can show it, and a test checks the two agree for every attack
skill. Options on the command line are applied on top of the saved settings.

## Why the solver isn't inside the plugin

A plugin runs in a sandbox: no file or process access, a fixed budget of Lua
instructions per callback, and at most two callbacks a second. There is no
linear-programming library in there, and the exact search that would replace
one needs millions of steps. A heuristic would fit but, as measured above,
isn't reliably right. So the plugin does what only it can (read the
inventory, appraise, select) and the desktop does the arithmetic.
