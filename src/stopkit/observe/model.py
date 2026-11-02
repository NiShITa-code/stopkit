"""The data the listers return.

An Item is one thing found on a surface: a process, a unit, a timer, a cron line, an at job or a listening
socket. A Listing is one complete reading of one surface. It is ok only if every command it needed
succeeded and was fully understood; a listing that is not ok proves nothing, not even that an item is gone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..cmd import CmdResult

SURFACES = ('process', 'unit', 'timer', 'cron', 'at', 'listener', 'worker')   # where leftovers can be
CONTEXT = ('manager', 'status')            # read only to explain the others: a service manager, a platform's claims


@dataclass(frozen=True)
class Item:
    surface: str
    key: str                                   # identity within the surface, stable across listings
    owner: str | None                          # account name, 'system', 'uid:<n>', or None if unknown
    text: str                                  # names and (masked) command lines, searched for tags
    fields: Mapping[str, Any] = field(default_factory=dict)

    def has(self, tag: str) -> bool:
        return bool(tag) and (tag in self.key or tag in self.text)


@dataclass(frozen=True)
class Listing:
    surface: str
    scope: str                                 # whose items: an account name, 'system' or 'all'
    ok: bool
    items: tuple[Item, ...]
    taken_at: float
    commands: tuple[CmdResult, ...] = ()       # every command run for this listing, with full output
    raw: tuple[tuple[str, str], ...] = ()      # other evidence as (name, text), e.g. the /proc table
    problems: tuple[str, ...] = ()             # why the listing is not ok

    def find(self, tag: str) -> list[Item]:
        return [i for i in self.items if i.has(tag)]


def failed(surface: str, scope: str, taken_at: float, problems, commands=(), raw=()) -> Listing:
    """A listing that could not be completed: it holds no items and proves nothing."""
    return Listing(surface, scope, False, (), taken_at, tuple(commands), tuple(raw), tuple(problems))
