"""The common interfaces every architecture plugs into (FRAMEWORK.md section 4). The core knows only these.

A binding (an adapter) realises the scenario's items on one kind of system, through that system's normal
paths, and uses its stops. An observer lists one surface, or records effects, and says whether its listing
worked. The ground-truth recorder is not an observer: verdicts never read it (stopkit/truth.py).
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .specs import OBSERVER_KINDS  # noqa: F401  (re-exported: observers declare one of these)


class Observer(Protocol):
    name: str
    kind: str                                  # one of OBSERVER_KINDS
    surfaces: tuple[str, ...]                  # the surfaces it lists or records
    blind_spots: tuple[str, ...]               # what it cannot see, stated for the coverage manifest

    def take(self, ctx: Any) -> Sequence[Any]: ...     # fresh listings (observe.model.Listing) for this run


class Binding(Protocol):
    name: str                                  # the binding's name, for example 'pretend' (the host reference)
    architecture: str                          # for example 'A1 Linux host'
    binding_version: str
    scenario: str                              # the scenario file it realises (spec/scenarios/<name>.json)
    stops: Mapping[str, str]                   # its stop name -> what the stop does
    stop_ids: Mapping[str, str]                # its stop name -> the stop register id (spec/stops.json)
    trusted: bool                              # True only for the kit's own agents (may run where untrusted code may not)

    def configuration(self) -> Mapping[str, Any]: ...   # every setting that differs from the system's default
    def start(self, ctx: Any) -> Any: ...
    def plant(self, ctx: Any, handle: Any) -> None: ...
    def stop(self, ctx: Any, handle: Any, stop: str) -> Any: ...
    def teardown(self, ctx: Any, handle: Any) -> None: ...
