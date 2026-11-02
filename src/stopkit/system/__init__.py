"""The machine (layer 2 of the kit; docs/DESIGN.md, section 5): running commands, reading /proc, the evidence store,
the kit's accounts, its names and places, and the checks it makes before it changes anything.
"""


class KitError(Exception):
    """The kit refuses to go on: the message says why."""
