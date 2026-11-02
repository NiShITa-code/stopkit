"""The coverage manifest of a run (FORMAT.md section 1, `coverage`): what the run could see.

Per surface the scenario reads: its observer, what it lists, how its listing is proven complete, whether it
was proven at each checkpoint (and why not), and what it cannot see. Per kind of work: the planted paths and
the surfaces that read them, or why the kind was not tested in this run. The coverage and limitations report
is built from these manifests and spec/surfaces.json; nothing here is inferred from verdicts.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from . import specs

NOT_LISTED = 'no listing of this surface at this checkpoint'


def proofs_at(snap, surfaces: Iterable[str], agent, seen: Mapping[str, str | None]) -> dict[str, str | None]:
    """{surface: None if this snapshot's listing of it is proven complete, else why not}, by the same rule the
    verdicts use (verdict.proof)."""
    from .verdict import proof
    return {s: proof(snap, s, agent, seen) for s in surfaces}


def _proven(p: Mapping[str, str | None], surface: str):
    """True if proven, otherwise the reason it was not."""
    if surface not in p:
        return NOT_LISTED
    return True if p[surface] is None else p[surface]


def manifest(scenario: specs.Scenario, proofs: Mapping[str, Mapping[str, str | None]]) -> dict:
    """The coverage section of a run record. proofs: {checkpoint name: {surface: None if proven, else why
    not}}, one entry per checkpoint in the order they were taken."""
    surf = specs.surfaces()
    used = sorted({s for it in scenario.items for ss in it.paths.values() for s in ss})
    surfaces = {}
    for s in used:
        sp = surf[s]
        surfaces[s] = {'observer': sp.observer, 'observer_kind': sp.kind, 'lists': sp.lists, 'proof': sp.proof,
                       'blind_spots': list(sp.blind_spots),
                       'proven': {cp: _proven(p, s) for cp, p in proofs.items()}}
    kinds = {}
    for k, kd in specs.kinds()['kinds'].items():
        items = [it for it in scenario.items if k in it.paths]
        read_on = sorted({s for it in items for s in it.paths[k]})
        observers = sorted(n for n, sp in surf.items() if k in sp.kinds and sp.implemented)
        if items:
            status = 'tested'
            proven = all(surfaces[s]['proven'][cp] is True for s in read_on for cp in proofs)
        else:
            status = 'not tested in this scenario' if observers else 'not tested: no implemented observer'
            proven = None
        kinds[k] = {'name': kd['name'], 'family': kd['family'], 'status': status,
                    'items': [it.id for it in items], 'surfaces': read_on,
                    'proven_at_every_checkpoint': proven, 'implemented_observers': observers}
    return {'spec_version': specs.version(), 'scenario': scenario.name, 'checkpoints': list(proofs),
            'surfaces': surfaces, 'kinds': kinds}
