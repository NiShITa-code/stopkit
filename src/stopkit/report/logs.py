"""A run's raw logs, laid out for a person to read, with none of the kit's rules applied (stopkit logs).

Everything shown comes from the run folder's own files: record.json for what was run, when, and with which tags
(its paths, cases, timing, the stop's commands and each check's snapshot), blobs/ for the full output of every
command and listing, truth.jsonl for the recorder's log and effects.jsonl for the receiver's. Nothing that the
kit worked out from them is read: no reading, proof, verdict, truth reading, count or result (a record's items,
truth, scope_result, truthfulness, coverage, effects and facts, and each check's readings, sentinels and proofs).
So a person can compare the logs with the kit's report, or decide without it. Times are in seconds from the
cut-off, the moment the stop was sent, on the monotonic clock that every log of the run shares.
"""
from __future__ import annotations

import json
import os
from collections import Counter

from ..system.evidence import EvidenceStore
from .record import folder_sums_problems

LONG = 600          # the characters of one entry that are shown; the blob named beside it holds the rest
SHORT = 40          # an output of at most this many entries is shown whole, tagged or not; the process table always is

# What each file of a run folder holds (README.md, "Where your results and logs are").
FILES = {
    'record.json': "what was run, when and with which tags, the commands, and, unless the run was made with "
                   "--facts-only, the kit's readings and verdicts",
    'report.md': "the kit's report on the run",
    'truth.jsonl': "the recorder's log",
    'effects.jsonl': "the receiver's log: what the planted work did, as a service outside the agent received it",
    'agent-g1.log': 'everything the agent printed from its first start',
    'agent-g2.log': 'everything the agent printed after it was started again, following the restart',
    'task-output.log': 'the output of the command that gave the agent its task',
    'model.jsonl': 'every request the agent sent to the stand-in model, with the reply',
    'model-script.json': 'the script the stand-in model followed',
    'tty.raw': "every byte the agent's terminal received",
    'SHA256SUMS': "the checksums of the run's files",
}


def _at(mono, cut: float) -> str:
    return 'not recorded' if mono is None else f'{mono - cut:+.3f} s'


def _short(s: str, held: list[str] = ()) -> str:
    """s, or its start when it is long, with the place of each tag it holds that the start leaves out."""
    if len(s) <= LONG:
        return s
    out = s[:LONG]
    for t in held:
        i = s.find(t)
        if i >= LONG:
            out += f' … {s[max(LONG, i - 100):i + len(t) + 100]}'
    return f'{out}… ({len(s)} characters in all; the blob holds the whole entry)'


def entries(text: str) -> list[str]:
    """A stored output as entries: the elements of a JSON list, the whole of other JSON, or the lines of text."""
    try:
        data = json.loads(text)
    except ValueError:
        return [line for line in text.splitlines() if line.strip()]
    if isinstance(data, list):
        return [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in data]
    return [json.dumps(data, ensure_ascii=False, sort_keys=True)]


def tags(rec: dict) -> dict[str, list[str]]:
    """Each tag of the run, with what carries it: the planted paths (record.json, paths) and the sentinel account."""
    out: dict[str, list[str]] = {}
    for p in rec.get('paths') or ():
        out.setdefault(p['tag'], []).append(p['id'])
    sentinel = (rec.get('bench') or {}).get('sentinel_tag')
    if sentinel:
        out.setdefault(sentinel, []).append('the sentinel account')
    return out


def _files(run_dir: str) -> list[str]:
    listed = set()
    try:
        with open(os.path.join(run_dir, 'SHA256SUMS'), encoding='utf-8') as f:
            listed = {line.rstrip('\n').split('  ', 1)[-1] for line in f if line.strip()}
    except OSError:
        pass
    lines = ['## The run folder', '', '| File | Bytes | Holds | In SHA256SUMS |', '|---|---|---|---|']
    for name in sorted(os.listdir(run_dir)):
        p = os.path.join(run_dir, name)
        if os.path.isdir(p):
            n = len(os.listdir(p))
            lines.append(f'| `{name}/` | {n} files | the full output of every command and listing, each file named by '
                         'the SHA-256 of its content, which is checked whenever it is read | (each by its name) |')
        else:
            lines.append(f"| `{name}` | {os.path.getsize(p)} | {FILES.get(name, '')} | "
                         f"{'yes' if name in listed else '—' if name == 'SHA256SUMS' else 'no'} |")
    bad = folder_sums_problems(run_dir)
    lines += ['', bad[0] + '.' if bad else 'Every file listed in SHA256SUMS has the checksum listed there.', '']
    return lines


def _work(rec: dict) -> list[str]:
    what = {c['name']: c.get('what', '') for c in rec.get('cases') or ()}
    lines = ['## The planted work and its tags', '',
             'Each piece of work carries its tag in its name or command line, and reports to the recorder under it.',
             '', '| Path | Tag | Looked for on | What it is |', '|---|---|---|---|']
    for p in rec.get('paths') or ():
        lines.append(f"| {p['id']} | `{p['tag']}` | {', '.join(p['surfaces'])} | {what.get(p.get('case'), '')} |")
    sentinel = (rec.get('bench') or {}).get('sentinel_tag')
    if sentinel:
        lines.append(f'| the sentinel account | `{sentinel}` | every listing | work of the sentinel account, which '
                     'a listing that reaches that account shows |')
    return lines + ['']


def _command(c: dict, store: EvidenceStore) -> list[str]:
    head = f"`{' '.join(c['argv'])}`: exit code {c['rc']}" + (f", error: {c['error']}" if c.get('error') else '')
    if c.get('truncated'):
        head += ', its output cut by the kit'
    lines = [f'- {head}']
    for what, digest in (('output', c.get('out_sha256')), ('error output', c.get('err_sha256'))):
        if not digest:
            continue
        try:
            text = store.get(digest)
        except (OSError, ValueError) as e:
            lines.append(f'  - its {what}, blobs/{digest}.gz: cannot be read: {e}')
            continue
        if text.strip():
            lines.append(f'  - its {what}, blobs/{digest}.gz: {_short(" ".join(text.split()))}')
    return lines


def _scan(label: str, digest: str, store: EvidenceStore, tag_of: dict[str, list[str]], found: Counter,
          whole: bool = False) -> list[str]:
    """One stored output: how many entries it has, and each entry that holds a tag, or every entry when the output
    is short or `whole` is set, marked with what carries the tags it holds."""
    try:
        text = store.get(digest)
    except (OSError, ValueError) as e:
        return [f'- {label}, blobs/{digest}.gz: cannot be read: {e}']
    es = entries(text)
    lines = [f'- {label}, blobs/{digest}.gz: {len(es)} entries' + ('' if whole or len(es) <= SHORT else
                                                                     ', of which those that hold a tag:')]
    for e in es:
        held = [t for t in tag_of if t in e]
        for t in held:
            found[t] += 1
        if held or whole or len(es) <= SHORT:
            mark = '[' + ', '.join(p for t in held for p in tag_of[t]) + '] ' if held else ''
            lines.append(f'  - {mark}{_short(e, held)}')
    return lines


def _check(cp: dict, cut: float, store: EvidenceStore, tag_of: dict[str, list[str]],
           looked: dict[str, set[str]]) -> list[str]:
    planned = cp.get('planned_s')
    lines = [f"## Check `{cp['name']}`" + (f', planned for {planned:+g} s' if isinstance(planned, (int, float)) else ''),
             '']
    for li in (cp.get('snapshot') or {}).get('listings') or ():
        mono = li.get('mono') or [None, None]
        lines.append(f"### {li['surface']}, of {li.get('scope')}: listed from {_at(mono[0], cut)} to "
                     f"{_at(mono[1], cut)}")
        lines.append('')
        found: Counter = Counter()
        for c in li.get('commands') or ():
            head = f"`{' '.join(c['argv'])}`: exit code {c['rc']}" + (f", error: {c['error']}" if c.get('error') else '')
            lines += _scan(f'{head}; its output', c['out_sha256'], store, tag_of, found)
            try:
                err = store.get(c['err_sha256']).strip() if c.get('err_sha256') else ''
            except (OSError, ValueError) as e:
                err = f'cannot be read: {e}'
            if err:
                lines.append(f"  - its error output, blobs/{c['err_sha256']}.gz: {_short(' '.join(err.split()))}")
        for r in li.get('raw') or ():
            lines += _scan(f"the {r['name']}", r['sha256'], store, tag_of, found, whole=li['surface'] == 'process')
        missing = [t for t in tag_of if li['surface'] in looked.get(t, ()) and not found[t]]
        for t in missing:
            lines.append(f"- No entry holds `{t}` ({', '.join(tag_of[t])})")
        lines.append('')
    return lines


def _recorder(path: str, cut: float, tag_of: dict[str, list[str]]) -> list[str]:
    per: dict = {}
    start = end = None
    still: list = []
    try:
        with open(path, encoding='utf-8') as f:
            events = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        return ["## The recorder's log", '', f'truth.jsonl cannot be read: {e}', '']
    for e in events:
        ev, mono = e.get('ev'), e.get('mono')
        if ev == 'start':
            start = mono
        elif ev == 'end':
            end, still = mono, [w[2] for w in e.get('still_watching') or () if len(w) > 2]
        elif ev in ('msg', 'exit', 'watch'):
            m = (e.get('msg') or {}) if ev == 'msg' else e
            t = per.setdefault(m.get('tag'), {'hello': [], 'exit': [], 'before': 0, 'after': 0, 'last': None,
                                               'fire': [], 'other': Counter()})
            kind = m.get('type') if ev == 'msg' else ev
            if kind == 'hello':
                t['hello'].append(f"{m.get('pid')} at {_at(mono, cut)}")
            elif kind == 'exit':
                t['exit'].append(f"{e.get('pid')} at {_at(mono, cut)}")
            elif kind == 'beat':
                t['before' if mono < cut else 'after'] += 1
                t['last'] = mono
            elif kind == 'fire':
                t['fire'].append(_at(mono, cut))
            elif kind != 'watch':
                t['other'][kind] += 1
    lines = ["## The recorder's log", '',
             f"From truth.jsonl, which runs from {_at(start, cut)} to {_at(end, cut)}. A process of planted work says "
             "hello when it starts, then sends a heartbeat every few tenths of a second; the recorder logs when each "
             "process it watches exits; a trigger's job reports each firing.", '',
             '| Tag | Carried by | Said hello (process, at) | Exited (process, at) | Heartbeats before / after the '
             'cut-off | Last heartbeat | Firings | Still watched at the end |', '|---|---|---|---|---|---|---|---|']
    for tag in list(tag_of) + sorted(t for t in per if t not in tag_of and t is not None):
        t = per.get(tag)
        if t is None:
            lines.append(f"| `{tag}` | {', '.join(tag_of[tag])} | — | — | 0 / 0 | — | — | no |")
            continue
        extra = '; '.join(f'{n} {k}' for k, n in sorted(t['other'].items()))
        lines.append(f"| `{tag}` | {', '.join(tag_of.get(tag, ['not planted in this run']))} | "
                     f"{'; '.join(t['hello']) or '—'} | {'; '.join(t['exit']) or '—'} | {t['before']} / {t['after']} | "
                     f"{_at(t['last'], cut) if t['last'] is not None else '—'} | "
                     f"{', '.join(t['fire']) or '—'}{'; ' + extra if extra else ''} | {'yes' if tag in still else 'no'} |")
    return lines + ['']


def _receiver(path: str, cut: float, tag_of: dict[str, list[str]]) -> list[str]:
    per: dict = {}
    try:
        with open(path, encoding='utf-8') as f:
            events = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        return ["## The receiver's log", '', f'effects.jsonl cannot be read: {e}', '']
    for e in events:
        if e.get('ev') != 'effect':
            continue
        tag, mono = (e.get('msg') or {}).get('tag'), e.get('mono')
        t = per.setdefault(tag, {'before': 0, 'after': []})
        if mono < cut:
            t['before'] += 1
        else:
            t['after'].append(mono)
    lines = ["## The receiver's log", '',
             'From effects.jsonl: each action of the planted work, at the time the receiver, a service outside the '
             'agent, got it.', '',
             '| Tag | Carried by | Actions before the cut-off | After the cut-off | First after | Last |',
             '|---|---|---|---|---|---|']
    for tag in list(tag_of) + sorted(t for t in per if t not in tag_of and t is not None):
        t = per.get(tag, {'before': 0, 'after': []})
        after = t['after']
        lines.append(f"| `{tag}` | {', '.join(tag_of.get(tag, ['not planted in this run']))} | {t['before']} | "
                     f"{len(after)} | {_at(min(after), cut) if after else '—'} | {_at(max(after), cut) if after else '—'} |")
    return lines + ['']


def run_logs(run_dir: str) -> str:
    """The raw logs of the run in run_dir, as Markdown."""
    with open(os.path.join(run_dir, 'record.json'), encoding='utf-8') as f:
        rec = json.load(f)
    store = EvidenceStore(run_dir, create=False)
    timing = rec.get('timing') or {}
    cut = timing.get('cut_off_mono')
    if cut is None:
        raise ValueError(f'{run_dir}: the record has no cut-off time')
    tag_of = tags(rec)
    looked: dict[str, set[str]] = {}
    for p in rec.get('paths') or ():
        looked.setdefault(p['tag'], set()).update(p['surfaces'])
    system, stop = rec.get('system') or {}, (rec.get('stop_spec') or {}).get('id')
    claim = rec.get('claim') or {}
    lines = [f"# The logs of run {rec.get('run_id')}: stop `{stop}`, {system.get('binding')}", '',
             "Copied from this run folder's files, with none of the kit's rules applied: no reading, verdict or "
             'result. Times are in seconds from the cut-off, the moment the stop was sent.', '']
    lines += _files(run_dir) + _work(rec)
    lines += ['## The stop', '',
              f"- Sent at the cut-off; it returned at {_at(timing.get('stop_returned_mono'), cut)}.",
              f"- The restart action began at {_at(timing.get('restart_action_mono'), cut)} and ended at "
              f"{_at(timing.get('restart_done_mono'), cut)}; the run ended at {_at(timing.get('end_mono'), cut)}.",
              f"- What the binding wrote about it: {_short(claim.get('message') or '(nothing)')}"]
    if claim.get('details'):
        lines.append(f"- What the binding kept of the agent's side: {_short(json.dumps(claim['details'], ensure_ascii=False))}")
    for c in claim.get('commands') or ():
        lines += _command(c, store)
    lines.append('')
    for cp in rec.get('checkpoints') or ():
        lines += _check(cp, cut, store, tag_of, looked)
    lines += _recorder(os.path.join(run_dir, 'truth.jsonl'), cut, tag_of)
    lines += _receiver(os.path.join(run_dir, 'effects.jsonl'), cut, tag_of)
    return '\n'.join(lines)
