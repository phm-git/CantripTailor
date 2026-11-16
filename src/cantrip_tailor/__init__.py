"""Cantrip Tailor: works out the best set of gear to wear in Asheron's Call.

The in-game half is the AC:Unreal plugin in ``plugin/cantrip_tailor``; this
package is the desktop half - it reads the plugin's dump of your inventory,
solves for the best cantrip coverage and writes a plan the plugin shows back
in game. Start with ``README.md``; the solver is explained in
``cantrip_tailor/solver.py`` and ``docs/how-it-works.md``.
"""

__version__ = "0.2.0"
