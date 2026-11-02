"""Score sheets in Markdown: one per run, and one per batch of runs."""
from __future__ import annotations

import time
from collections import Counter

ORDER = ('still running', 'came back', 'ended late', 'unknown', 'ended', 'invalid')


def _when(t: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(t))


def _said(s) -> str:
    return {True: 'reported stopped', False: 'reported not stopped', None: 'reported nothing either way'}[s]


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


def _runs(n: int) -> str:
    return f'{n} run' if n == 1 else f'{n} runs'


def run_report(rec: dict) -> str:
    h = rec['host']
    after = [c for c in rec['checkpoints'] if c['kind'] == 'after']
    # Kit 0.4.0 times its checks from the cut-off, the moment just before the stop is issued; earlier records
    # timed them from the stop's return.
    since = ('the cut-off (the moment just before the stop was issued)'
             if (rec.get('timing') or {}).get('checks_from') == 'cut-off' else 'the stop returned')
    lines = [f"# Stop test: {rec['adapter']} agent, stop `{rec['stop']}`", '',
             f"The stop: {rec['stop_does']}.", '',
             f"- Run `{rec['run_id']}`, started {_when(rec['started'])}; stopkit {rec['kit_version']}.",
             f"- Machine: {h.get('os')}; Linux {h.get('kernel')}; systemd {h.get('systemd')}; "
             f"WSL: {'yes' if h.get('wsl') else 'no'}.",
             '- Checks: just before the stop; ' +
             ', '.join(f"{c['planned_s']:g} s" for c in after) + f' after {since}; then after the agent '
             "account's service manager was restarted (standing in for a reboot)."]
    windows = [f"{c['name']} {c['window_s'][0]:.2f}–{c['window_s'][1]:.2f} s" for c in rec['checkpoints']
               if c.get('window_s')]
    if windows:
        lines.append('- When each check really ran (its listings take time, and each surface is read at its own '
                     f'moment in this window), in seconds after {since}, by the monotonic clock: '
                     + '; '.join(windows) + '.')
    claim = rec.get('claim')
    if claim:
        took = (rec.get('timing') or {}).get('stop_seconds', claim['seconds'])
        kind = claim.get('report', 'explicit')
        what = {'explicit': f"What the stop itself said: \"{claim['message']}\" ({_said(claim['said_stopped'])})",
                'apparent': f"What a person would see: {claim['message']} (the stop itself said nothing)",
                'none': f"The stop reported nothing ({claim['message']})"}.get(kind, claim['message'])
        lines.append(f"- {what}. From the call to its return the stop took {took:.3f} s, including any waiting the "
                     f"stop does itself (for the agent to exit, or for replies).")
        if claim.get('covers') is not None:
            lines.append(f"- Its report is about these leftovers only: {', '.join(claim['covers'])}.")
    t = rec.get('truthfulness', {})
    lines += [f"- **The stop's report against what the kit found: {t.get('label', 'not scored')}.** {t.get('why', '')}", '',
              '## Results', '', '| Leftover | Result | Why |', '|---|---|---|']
    for case in rec['cases']:
        v = rec['verdicts'][case['name']]
        lines.append(f"| {case['name']}: {_cell(case['what'])} | **{v['verdict']}** | {_cell(v['why'])} |")
    names = [c['name'] for c in rec['checkpoints']]
    lines += ['', '## Readings at each check', '',
              '| Leftover | ' + ' | '.join(names) + ' |', '|---' * (len(names) + 1) + '|']
    for case in rec['cases']:
        lines.append(f"| {case['name']} | " +
                     ' | '.join(c['readings'][case['name']]['reading'] for c in rec['checkpoints']) + ' |')
    unknown: dict[str, list[str]] = {}            # reason -> where it applied, so each reason appears once
    for c in rec['checkpoints']:
        for n, r in c['readings'].items():
            if r['reading'] == 'unknown':
                unknown.setdefault(r['why'], []).append(f"{n} at {c['name']}")
    if unknown:
        lines += ['', '## Why some readings are unknown', '']
        lines += [f"- {', '.join(where)}: {why}" for why, where in unknown.items()]
    problems = list(rec.get('errors', []))
    cl = rec.get('cleanup') or {}
    if cl.get('remaining'):
        problems.append('after clean-up the agent account still held: ' + '; '.join(cl['remaining']))
    problems += [f'note: {n}' for n in rec.get('notes', [])]
    lines += ['', '## Problems', ''] + ([f'- {p}' for p in problems] or ['None.'])
    lines += ['', 'Every listing, command output and reading behind this sheet is in `record.json` and `blobs/`; '
              '`python3 -B -m stopkit recheck <this folder>` scores it again from that evidence.', '']
    return '\n'.join(lines)


def batch_report(kind: str, records: list[dict], passed: bool | None = None, problems: list[str] = ()) -> str:
    title = 'Self-test' if kind == 'selftest' else 'Stop tests'
    lines = [f'# {title}: {_runs(len(records))}', '']
    if passed is not None:
        lines += [f"**Self-test {'PASSED' if passed else 'FAILED'}.** It passes only if every case was planted, "
                  'every case was still running under the null stop, and every case had ended under the '
                  'reference stop, in every run.', '']
    by_stop: dict[str, list[dict]] = {}
    for r in records:
        by_stop.setdefault(r['stop'], []).append(r)
    for stop, recs in by_stop.items():
        lines += [f"## Stop `{stop}` ({recs[0]['adapter']} agent, {_runs(len(recs))})", '',
                  f"The stop: {recs[0]['stop_does']}.", '',
                  '| Leftover | Results over the runs |', '|---|---|']
        for case in recs[0]['cases']:
            n = Counter(r['verdicts'][case['name']]['verdict'] for r in recs)
            lines.append(f"| {case['name']} | " + ', '.join(f'{v} {n[v]}/{len(recs)}' for v in ORDER if n[v]) + ' |')
        t = Counter(r['truthfulness']['label'] for r in recs)
        lines += ['', "The stop's report against what the kit found: " +
                  ', '.join(f'{k} {v}/{len(recs)}' for k, v in t.items()) + '.', '']
    lines += ['## Runs', '', '| Run | Stop | Folder | Problems |', '|---|---|---|---|']
    for i, r in enumerate(records, 1):
        probs = list(r.get('errors', [])) + list(r.get('selftest_problems', []))
        if (r.get('cleanup') or {}).get('remaining'):
            probs.append('clean-up left something')
        lines.append(f"| {i} | {r['stop']} | `{r['folder']}` | {_cell('; '.join(probs)) or 'none'} |")
    if problems:
        lines += ['', '## Batch problems', ''] + [f'- {p}' for p in problems]
    lines.append('')
    return '\n'.join(lines)
