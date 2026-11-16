"""The ``cantrip-tailor`` command.

    uv run cantrip-tailor                       # newest dump, with what you set on the plugin's Settings tab
    uv run cantrip-tailor --list                # whose dumps are there
    uv run cantrip-tailor --character "My Archer" --attack missile --usable-only
    uv run cantrip-tailor --want "Summoning Prowess=high" --lock-slot trinket --fill-slots
    uv run cantrip-tailor --show-weights        # the priority table, and nothing else

A thin shell over :mod:`cantrip_tailor.solver`: find the plugin's files, read
the dump and the character's saved settings, solve, print the report, and
write the plan where the plugin's Plan tab will pick it up.

Settings and options: whatever was set on the plugin's Settings tab is the
starting point, and options on the command line add to it or override it.
``--no-settings`` ignores the saved settings altogether.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, solver

# --- colour ---------------------------------------------------------------------------
# The solver labels each part of the report with what it is; these are the
# colours for those labels, as ANSI "SGR" codes (1 bold, 2 faint, 31 red,
# 32 green, 33 yellow, 35 magenta, 36 cyan). Only colours that read on both
# dark and light backgrounds; "faint" simply shows as normal text where a
# console doesn't support it.
COLORS = {
    "title": "1", "heading": "1;36", "score": "1;32",
    "worn": "32", "pack": "33",                      # same meaning as the Plan tab: green = on, yellow = still in a pack
    "counted": "1", "outdone": "2", "unwanted": "2", "dim": "2",
    "locked": "35", "bad": "1;31", "warn": "33", "good": "32", "missing": "31",
    "puton": "33", "takeoff": "35",
    "level_minor": "33", "level_moderate": "33", "level_major": "32", "level_epic": "36", "level_legendary": "35",
}


def ansi(kind: str, text: str) -> str:
    """Wrap text in the colour for its kind (the "paint" function handed to solver.report)."""
    code = COLORS.get(kind)
    return f"\x1b[{code}m{text}\x1b[0m" if code and text else text


def _windows_console_takes_ansi() -> bool:
    """Ask a Windows console to understand colour codes. True if it does (or already did).

    Windows Terminal always does. The older console window (what a plain cmd
    or PowerShell shortcut opens) can since Windows 10, but only once the
    program switches it on - that is what this does. Anything going wrong
    here just means "no colour".
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(kernel32.GetStdHandle(-11))            # -11 = standard output
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False                                                # not a console at all
        VIRTUAL_TERMINAL = 0x0004
        return bool(mode.value & VIRTUAL_TERMINAL) or bool(kernel32.SetConsoleMode(handle, mode.value | VIRTUAL_TERMINAL))
    except Exception:
        return False


def use_color(choice: str, stream=None) -> bool:
    """Should the report be coloured? ``choice`` is --color: auto, always or never.

    Auto means: only when writing to a real console that will show colours -
    never into a file or a pipe - and not if the NO_COLOR environment
    variable is set (the usual convention for "I don't want colour").
    """
    if choice != "auto":
        return choice == "always"
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if not (hasattr(stream, "isatty") and stream.isatty()):
        return False
    return _windows_console_takes_ansi() if os.name == "nt" else True

PLUGIN_ID = "cantrip_tailor"
# Where the plugin's files sit, relative to the client's install folder.
_LIBRARY = Path("Saved") / "ClientPlugins" / "Profiles" / PLUGIN_ID / "Library"


def find_library(args: argparse.Namespace) -> Path:
    """The plugin's Library folder (it holds ``Dumps`` and ``Plans``).

    In order: ``--library``; ``--client-dir`` (the AC:Unreal install folder);
    the ``CANTRIP_TAILOR_LIBRARY`` / ``CANTRIP_TAILOR_CLIENT`` environment
    variables; then the client's default install location under
    ``%LOCALAPPDATA%\\Programs``.
    """
    if args.library:
        return Path(args.library)
    if args.client_dir:
        return Path(args.client_dir) / _LIBRARY
    if os.environ.get("CANTRIP_TAILOR_LIBRARY"):
        return Path(os.environ["CANTRIP_TAILOR_LIBRARY"])
    if os.environ.get("CANTRIP_TAILOR_CLIENT"):
        return Path(os.environ["CANTRIP_TAILOR_CLIENT"]) / _LIBRARY
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Programs" / "ACUnreal" / "ACUnreal" / _LIBRARY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cantrip-tailor",
        description="Work out the best set of gear to wear from the Cantrip Tailor plugin's dump of your inventory, "
                    "and write the plan the plugin shows in game.")
    p.add_argument("--version", action="version", version=f"cantrip-tailor {__version__}")

    where = p.add_argument_group("where the plugin's files are")
    where.add_argument("--client-dir", metavar="DIR",
                       help="the AC:Unreal install folder (the one containing Saved); "
                            "default: %%LOCALAPPDATA%%\\Programs\\ACUnreal\\ACUnreal, or the CANTRIP_TAILOR_CLIENT environment variable")
    where.add_argument("--library", metavar="DIR",
                       help="the plugin's Library folder itself (it contains Dumps and Plans), if you'd rather point straight at it")

    who = p.add_argument_group("which character")
    who.add_argument("--character", help="whose dump to use (default: the most recent one)")
    who.add_argument("--list", action="store_true", help="list the dumps found, then exit")
    who.add_argument("--no-settings", action="store_true",
                     help="ignore what was set on the plugin's Settings tab; use only the defaults and this command line")

    want = p.add_argument_group("what you want")
    want.add_argument("--attack", choices=list(solver.ATTACKS),
                      help="the attack skill to build for (default: your specialised, or highest trained, attack skill)")
    want.add_argument("--want", action="append", metavar="CANTRIP=PRIORITY",
                      help="also value this cantrip: high, med, low, none or a number of points (repeatable), "
                           "e.g. --want \"Arcane Prowess=high\" --want \"Summoning Prowess=90\" --want Quickness=none")
    want.add_argument("--show-weights", action="store_true", help="print the priority points that would be used, then exit")

    rules = p.add_argument_group("rules for the set")
    rules.add_argument("--usable-only", action="store_true",
                       help="leave out pieces you can't wield, or whose spells wouldn't activate, at the skills in the dump "
                            "(default: requirements are shown and flagged, not enforced)")
    rules.add_argument("--assume", action="append", metavar="SKILL=VALUE",
                       help="check activation as if this skill were at this value (repeatable), e.g. --assume \"Arcane Lore=360\"")
    rules.add_argument("--fill-slots", action="store_true",
                       help="every slot must have something in it: a slot no cantrip cares about gets the best armour level "
                            "instead of being left free")
    rules.add_argument("--lock-slot", action="append", metavar="SLOT",
                       help="keep whatever is worn there now (repeatable): trinket, necklace, bracelets, rings, head, chest, "
                            "abdomen, upper-arms, lower-arms, hands, upper-legs, lower-legs, feet, shirt, pants, cloak")
    rules.add_argument("--lock", action="append", metavar="NAME|ID", help="always wear this piece (repeatable)")
    rules.add_argument("--exclude", action="append", metavar="NAME|ID", help="never pick pieces matching this (repeatable)")
    rules.add_argument("--ignore-held", action="store_true",
                       help="don't count the cantrips on the wand, weapon or shield in your hands")

    out = p.add_argument_group("output")
    out.add_argument("-o", "--output", metavar="FILE", help="also write the report to this file (always plain text)")
    out.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                     help="colour the report: auto (default) = only on a console that can show it; "
                          "setting the NO_COLOR environment variable also turns it off")
    out.add_argument("--no-plan", action="store_true", help="don't write the plan file for the in-game Plan tab")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    library = find_library(args)
    dumps = library / "Dumps"
    try:
        if not dumps.is_dir():
            raise solver.GearError(
                f"can't find the plugin's dump folder:\n  {dumps}\n"
                "Has the Cantrip Tailor plugin written a dump yet (Dump tab > Scan + dump)?\n"
                "If your client is installed somewhere else, say where:  --client-dir \"D:\\Games\\ACUnreal\"")
        if args.list:
            for j in solver.list_dumps(dumps):
                print(f"{j.get('character')}  level {j.get('level')}  {j.get('items')} items  "
                      f"({j.get('unidentified')} not appraised)  {j['_path'].name}")
            return 0
        dump = solver.load_dump(dumps, args.character)
        # Start from what the character saved on the Settings tab; the command line then adds or overrides.
        saved = None if args.no_settings else solver.load_settings(library / "Settings", dump.character)
        use = saved or solver.Settings()
        for skill, value in use.assume.items():
            dump.assume(skill, value)
        for text in args.assume or []:
            skill, _, value = text.partition("=")
            if not value.strip().isdigit():
                raise solver.GearError(f"--assume {text!r}: write it as \"Arcane Lore=360\"")
            use.assume[dump.assume(skill, int(value))] = int(value)
        use.attack = args.attack or use.attack
        attack = use.attack or solver.guess_attack(dump)
        if not attack:
            raise solver.GearError(f"can't tell {dump.character}'s attack skill from the dump; "
                                   f"pick one on the Settings tab or pass --attack ({', '.join(solver.ATTACKS)})")
        known = set(solver.KNOWN_CANTRIPS) | set(solver.WARDS) | {s for i in dump.items for s in i.cantrips}
        use.points.update(solver.parse_want(w, known) for w in args.want or [])
        use.usable_only, use.fill_slots = use.usable_only or args.usable_only, use.fill_slots or args.fill_slots
        use.ignore_held = use.ignore_held or args.ignore_held
        use.lock_slots = list(dict.fromkeys([*use.lock_slots, *(args.lock_slot or [])]))
        weights = solver.build_weights(attack, use.points)
        if args.show_weights:
            print("Points for a Major of each (a Minor is worth 1/3 for skills and attributes, 2/3 for wards, 1/2 for Armor):")
            for stem, pts in weights.items():
                print(f"  {pts:>5.0f}  {stem}")
            return 0
        plan = solver.solve(dump, weights, lock=args.lock, lock_slots=use.lock_slots, exclude=args.exclude,
                            usable_only=use.usable_only, count_held=not use.ignore_held, fill=use.fill_slots)
    except solver.GearError as e:
        print(e, file=sys.stderr)
        return 2

    paint = ansi if use_color(args.color) else (lambda kind, text: text)
    if saved:
        print(paint("dim", "Using this character's saved settings (plugin > Settings tab), plus anything on this command line."))
    described = use.describe() + "".join(f"; {what} {v}" for what, values in (("lock", args.lock), ("exclude", args.exclude))
                                         for v in values or [])
    now = time.time()
    print(paint("dim", f"Options: {described}") + "\n" + solver.report(dump, plan, attack, paint, now))
    if args.output:      # the file never gets colour codes
        Path(args.output).write_text(f"Options: {described}\n" + solver.report(dump, plan, attack, now=now) + "\n", encoding="utf-8")
    if not args.no_plan:
        # The plugin reads this from its own library, next to the dumps, and shows it on the Plan tab.
        data = solver.plan_json(dump, plan, attack, generated=int(time.time()), options=described)
        plans = library / "Plans"
        plans.mkdir(parents=True, exist_ok=True)
        path = plans / f"{solver.plan_name(dump.character)}.json"
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"\nPlan written: {path}\nIn game: Cantrip Tailor window > Plan tab (it appears within 15 seconds, or press Reload plan).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
