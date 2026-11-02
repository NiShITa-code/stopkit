"""The commands: python3 -B -m stopkit <command> (run as root).

  setup        create the kit's accounts, start the worker stand-in, put the sentinels in place, check the
               agent account is empty
  status       what the kit sees now: accounts, sentinels, anything left in the agent account
  selftest     check the kit on known answers, against the ground truth too; nothing else should be trusted
               until it passes
  bench        run stops of an adapter against the scenario's leftovers with the ground truth, and write
               records (stopkit-record/2), rows and the report
  report       generate the report (reach matrix, run details, accuracy, timelines, coverage) from run folders
  test         the earlier runs without the ground truth (stopkit-record/1), kept for adapters with no
               scenario yet
  check        list what an account still runs around a stop you make yourself (check mode)
  recheck      score run or check folders again from their own evidence, and report any difference
  smoke-check  hold a binding's smoke batch (its none and reference runs) to the known answers (GUIDE.md section 8)
  e0           check the ground truth on its own (FRAMEWORK.md section 3.4) before anything is scored with it
  noninterference
               check that the kit's observers and instruments do not change what they measure (N1–N7), for
               one binding
  cleanup      return the agent account to empty; with --all, remove everything the kit made

Exit status: 0 done (and passed); 1 a check failed; 2 the kit refused to start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, accounts, adapters, check, delegation, guards, record, runner, sentinel, stops
from .config import AGENT, SENTINEL, STATE_DIR, WORKER, WORKER_SOCKET, KitError
from .observe import snapshot


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_setup(a) -> int:
    agent, sent, worker = runner.setup(log=_log)
    _log(f'ready: the agent account {agent.name} is empty, the worker stand-in runs as {worker.name}, and the '
         f'sentinels of {sent.name} are visible')
    return 0


def cmd_status(a) -> int:
    agent, sent, worker = accounts.owned(AGENT), accounts.owned(SENTINEL), accounts.owned(WORKER)
    for acc in (agent, sent):
        _log(f'{acc.name}: uid {acc.uid}; service manager {accounts.manager_state(acc)}')
    _log(f"{worker.name}: uid {worker.uid}; worker stand-in {'running' if delegation.running() else 'NOT RUNNING'}")
    seen = sentinel.seen(snapshot.take([agent, sent], process_only=[worker], worker=WORKER_SOCKET), sent)
    _log('sentinels: ' + ', '.join(f"{s} {'seen' if why is None else 'NOT SEEN (' + why + ')'}" for s, why in seen.items()))
    left = stops.leftovers(agent, files_ok=False)
    _log('agent account: ' + ('empty' if not left else f'{len(left)} things left: ' + '; '.join(left)))
    return 0 if not left and all(w is None for w in seen.values()) else 1


def cmd_selftest(a) -> int:
    from . import protocol
    folder, records, problems = protocol.run_batch('pretend', ['none', 'reference'], a.runs, a.out, 'selftest',
                                                   protocol.SELFTEST_CHECKS, log=_log)
    failed = {r['run_id']: protocol.selftest_problems(r) for r in records}
    passed = not problems and len(records) == 2 * a.runs and not any(failed.values())
    for run_id, ps in failed.items():
        for p in ps:
            _log(f'  {run_id}: {p}')
    _log(f"self-test {'PASSED' if passed else 'FAILED'}: {os.path.join(folder, 'report', 'REPORT.md')}")
    return 0 if passed else 1


def _checks(text: str | None) -> tuple[float, ...] | None:
    if text is None:
        return None
    try:
        checks = tuple(float(x) for x in text.split(','))
    except ValueError:
        raise KitError(f'--checks takes seconds separated by commas, for example 0,60,300; got {text!r}')
    if any(x < 0 for x in checks) or list(checks) != sorted(checks):
        raise KitError('--checks needs increasing, non-negative seconds')
    return checks


def cmd_bench(a) -> int:
    from . import protocol
    folder, records, problems = protocol.run_batch(a.adapter, a.stop, a.runs, a.out, 'test', _checks(a.checks),
                                                   log=_log)
    _log(f"done: {os.path.join(folder, 'report', 'REPORT.md')}")
    return 0 if not problems and all(not r['errors'] for r in records) else 1


def cmd_report(a) -> int:
    from . import reports
    res = reports.write(a.folders, a.out)
    _log(f"report of {res['runs']} runs ({res['rows']} rows): {os.path.join(a.out, 'REPORT.md')} and REPORT.html")
    return 0


def cmd_test(a) -> int:
    try:
        offsets = tuple(float(x) for x in a.at.split(','))
    except ValueError:
        raise KitError(f'--at takes seconds separated by commas, for example 0,60,300; got {a.at!r}')
    if any(x < 0 for x in offsets) or list(offsets) != sorted(offsets):
        raise KitError('--at needs increasing, non-negative seconds')
    settings = runner.Settings(offsets=offsets)
    folder, records, problems = runner.run_batch(a.adapter, a.stop, a.runs, a.out, settings, 'test', log=_log)
    runner.write_batch(folder, 'test', records, problems)
    _log(f"done: {os.path.join(folder, 'summary.md')}")
    return 0 if not problems and all(not r['errors'] for r in records) else 1


def cmd_check(a) -> int:
    if a.step != 'report':
        problems = guards.root_problems() + guards.disk_problems()
        if problems:
            raise KitError('; '.join(problems))
    if a.folder is None:
        if a.step not in ('baseline', 'before') or not a.account:
            raise KitError('start a check with `check baseline --account U` or `check before --account U`; '
                           'later steps take the check folder')
        folder = check.new_check(a.account, a.out)
    else:
        folder = a.folder
    if a.step == 'report':
        check.report(folder)
        _log(f"score sheet: {os.path.join(folder, 'report.md')}")
        return 0
    if a.step == 'restart' and not a.yes:
        raise KitError("restarting the account's service manager stops every service it runs; add --yes to do it")
    cp = check.add(folder, a.step, restart=a.step == 'restart')
    _log(f"{cp['name']}: listed into {folder}")
    if cp.get('restart_problem'):
        _log(f"the restart is not confirmed: {cp['restart_problem']}")
    return 0


def cmd_recheck(a) -> int:
    bad = 0
    for d in a.folders:
        try:
            if os.path.exists(os.path.join(d, 'check.json')):
                problems = check.recheck(d)
            else:
                with open(os.path.join(d, 'record.json'), encoding='utf-8') as f:
                    fmt = json.load(f).get('format')
                if fmt == 'stopkit-record/2':
                    from . import protocol
                    problems = protocol.recheck(d)
                else:
                    problems = record.recheck(d)
        except Exception as e:                   # one unreadable folder must not stop the others being checked
            problems = [f'could not be checked: {type(e).__name__}: {e}']
        _log(f"{d}: {'same verdicts from the evidence' if not problems else 'DIFFERENCES'}")
        for p in problems:
            _log(f'  {p}')
        bad += bool(problems)
    return 1 if bad else 0


def cmd_smoke_check(a) -> int:
    from . import protocol
    paths = sorted(os.path.join(a.folder, d, 'record.json') for d in os.listdir(a.folder)
                   if os.path.isfile(os.path.join(a.folder, d, 'record.json')))
    problems, notes, judged = [], [], 0
    for path in paths:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        if rec['stop_spec']['adapter_stop'] not in ('none', 'reference'):
            continue                             # only these two stops have known answers
        judged += 1
        bad, said = protocol.smoke_problems(rec)
        problems += [f"{rec['folder']}: {p}" for p in bad]
        notes += [f"{rec['folder']}: {n}" for n in said]
    if not judged:
        problems.append(f'{a.folder}: no run of the none or reference stop')
    _log(f'PASS ({judged} runs of none and reference)' if not problems else 'FAIL: ' + '; '.join(problems))
    for n in notes:
        _log(f'  note: {n}')
    return 0 if not problems else 1


def cmd_e0(a) -> int:
    from . import e0
    passed, folder = e0.run(a.out, a.trials, a.fires, log=_log, binding=a.binding)
    _log(f"E0 {'PASSED' if passed else 'FAILED'}: {os.path.join(folder, 'E0.md')}")
    return 0 if passed else 1


def cmd_noninterference(a) -> int:
    from . import noninterference
    passed, folder = noninterference.run_all(a.adapter, a.stop, a.out, log=_log)
    _log(f"N1–N7 {'PASSED' if passed else 'FAILED'}: {os.path.join(folder, 'NONINTERFERENCE.md')}")
    return 0 if passed else 1


def _runs(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return n


def cmd_cleanup(a) -> int:
    agent = accounts.owned(AGENT)
    if accounts.manager_state(agent) not in accounts.MANAGER_UP:
        accounts.start_manager(agent)
    _, left = stops.cleanup(agent)
    _log('agent account: ' + ('empty' if not left else 'still holds: ' + '; '.join(left)))
    if not a.all:
        return 0 if not left else 1
    problems = sentinel.remove(accounts.owned(SENTINEL))
    delegation.stop()
    for name in (AGENT, SENTINEL, WORKER):
        if name in accounts.load_marker():
            problems += accounts.remove(name)
    _log(f'removed the accounts {AGENT}, {SENTINEL} and {WORKER}, and stopped the worker stand-in' +
         (f'; problems: {problems}' if problems else ''))
    _log(f'{STATE_DIR} is left in place (the kit\'s marker and scripts); remove it by hand if wanted')
    return 0 if not problems else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='stopkit', description='Tests from outside whether stopping an AI agent '
                                'really stops its work. Run as root on a Linux machine with systemd.')
    p.add_argument('--version', action='version', version=f'stopkit {__version__}')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('setup', help='create the accounts and sentinels').set_defaults(func=cmd_setup)
    sub.add_parser('status', help='show what the kit sees now').set_defaults(func=cmd_status)
    s = sub.add_parser('selftest', help='check the kit on known answers')
    s.add_argument('--runs', type=_runs, default=3)
    s.add_argument('--out', default='results')
    s.set_defaults(func=cmd_selftest)
    bn = sub.add_parser('bench', help='test stops of an adapter with the ground truth (stopkit-record/2)')
    bn.add_argument('--adapter', default='pretend', choices=adapters.NAMES)
    bn.add_argument('--stop', action='append', required=True, help='a stop of the adapter; give it several times for several')
    bn.add_argument('--runs', type=_runs, default=1)
    bn.add_argument('--checks', help="checks in seconds after the cut-off (default: the scenario's)")
    bn.add_argument('--out', default='results')
    bn.set_defaults(func=cmd_bench)
    rp = sub.add_parser('report', help='generate the report from run folders')
    rp.add_argument('folders', nargs='+')
    rp.add_argument('--out', required=True)
    rp.set_defaults(func=cmd_report)
    t = sub.add_parser('test', help='test stops of an adapter without the ground truth (stopkit-record/1)')
    t.add_argument('--adapter', default='pretend', choices=adapters.NAMES)
    t.add_argument('--stop', action='append', required=True, help='a stop of the adapter; give it several times for several')
    t.add_argument('--runs', type=_runs, default=1)
    t.add_argument('--at', default='0,60,300', help='checks, in seconds after the cut-off, the moment just before the stop is issued (default 0,60,300)')
    t.add_argument('--out', default='results')
    t.set_defaults(func=cmd_test)
    k = sub.add_parser('check', help='list what an account still runs around a stop you make yourself')
    k.add_argument('step', choices=('baseline', 'before', 'after', 'restart', 'report'))
    k.add_argument('folder', nargs='?', help='the check folder (made by the first step)')
    k.add_argument('--account', help='the account the agent runs as (first step only)')
    k.add_argument('--out', default='results')
    k.add_argument('--yes', action='store_true', help="confirm the restart of the account's service manager")
    k.set_defaults(func=cmd_check)
    r = sub.add_parser('recheck', help='score run or check folders again from their evidence')
    r.add_argument('folders', nargs='+')
    r.set_defaults(func=cmd_recheck)
    sc = sub.add_parser('smoke-check', help="hold a binding's smoke batch to the known answers")
    sc.add_argument('folder', help='the batch folder that `bench --stop none --stop reference` wrote')
    sc.set_defaults(func=cmd_smoke_check)
    e = sub.add_parser('e0', help='check the ground truth on its own')
    e.add_argument('--trials', type=_runs, default=30)
    e.add_argument('--fires', action='store_true', help='also time real timers, cron and at (about 4 more minutes)')
    e.add_argument('--binding', choices=adapters.NAMES, help="also check the parts of the truth this binding's "
                   'scenario uses: work inside a process, the HTTP stand-in')
    e.add_argument('--out', default='results')
    e.set_defaults(func=cmd_e0)
    ni = sub.add_parser('noninterference', help="check that the kit's observers and instruments do not change "
                        'what they measure (N1–N7)')
    ni.add_argument('--adapter', default='pretend', choices=adapters.NAMES)
    ni.add_argument('--stop', action='append', required=True, help='a stop to run with and without the instruments, '
                    'and at two polling rates; give it several times for several')
    ni.add_argument('--out', default='results')
    ni.set_defaults(func=cmd_noninterference)
    c = sub.add_parser('cleanup', help='return the agent account to empty')
    c.add_argument('--all', action='store_true', help='also remove the sentinels and both accounts')
    c.set_defaults(func=cmd_cleanup)
    a = p.parse_args(argv)
    try:
        if a.command in ('status', 'recheck', 'report', 'smoke-check'):     # these only read
            return a.func(a)
        with guards.single_run():
            return a.func(a)
    except KitError as e:
        print(f'stopkit: {e}', file=sys.stderr)
        return 2
