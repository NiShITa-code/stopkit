"""The commands: python3 -B -m stopkit <command> (run as root).

  init         write a config file template for your agent, every field explained (docs/CONFIG.md)
  doctor       check the machine and one agent's config file: start the agent, see that it reaches the
               stand-in model and that its shell tool runs a command; plants nothing
  check        test the stops of an agent (its config file, or a ready-made binding's name): every stop,
               several runs each, with the ground truth; writes records, rows and the report
  watch        list what an account still runs around a stop you make yourself (watch mode)
  report       write the report (reach matrix, run details, accuracy, timelines, coverage) from run folders
  recheck      score run or watch folders again from their own evidence, and report any difference
  logs         a run's raw logs from its own files, with none of the kit's rules applied: each check's
               commands and the output lines that hold a planted tag, the recorder's and the receiver's logs
  setup        create the kit's accounts, start the worker stand-in, put the sentinels in place
  status       what the kit sees now: accounts, sentinels, anything left in the agent account
  selftest     check the kit on known answers, against the ground truth too; nothing else should be trusted
               until it passes
  cleanup      return the agent account to empty; with --all, remove everything the kit made
  dev          checks of the kit itself, for binding authors (docs/GUIDE.md): e0 (the ground truth on its own),
               noninterference (N1–N7), smoke-check (a smoke batch against the known answers), test (the
               earlier record format)

Exit status: 0 done (and passed); 1 a check failed; 2 the kit refused to start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, bindings, stops
from .agents import agent_config, delegation
from .observe import sentinel, snapshot
from .report import record
from .run import runner, watch
from .system import KitError, accounts, guards
from .system.names import AGENT, SENTINEL, STATE_DIR, WORKER, WORKER_SOCKET

# Commands that were renamed, and what to type now.
RENAMED = {'bench': 'stopkit check TARGET --stop ...', 'e0': 'stopkit dev e0', 'noninterference':
           'stopkit dev noninterference TARGET --stop ...', 'smoke-check': 'stopkit dev smoke-check FOLDER',
           'test': 'stopkit dev test --adapter ...'}
WATCH_STEPS = ('baseline', 'before', 'after', 'restart', 'report')


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_init(a) -> int:
    if os.path.exists(a.file):
        raise KitError(f'{a.file} exists already; give another file name')
    with open(a.file, 'w', encoding='utf-8', newline='\n') as f:
        f.write(agent_config.template())
    _log(f'wrote {a.file}: fill it in for your agent, then run `stopkit doctor {a.file}`')
    return 0


def cmd_doctor(a) -> int:
    from .run import doctor
    passed, folder = doctor.run_doctor(a.file, a.out, log=_log)
    _log(f"doctor {'PASSED' if passed else 'FAILED'}" + (f': {folder}' if folder else ''))
    return 0 if passed else 1


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
    from .run import protocol
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


def cmd_check(a) -> int:
    from .run import protocol
    if a.target in WATCH_STEPS:
        raise KitError(f'watch mode is `stopkit watch {a.target} ...` now; `stopkit check` tests an agent from its '
                       'config file or a ready-made binding')
    adapter = bindings.resolve(a.target)
    names = a.stop or list(adapter.stops)
    _log(f'{adapter.name}: {len(names)} stops ({", ".join(names)}), {a.runs} runs each: {len(names) * a.runs} runs, '
         'each of several minutes')
    folder, records, problems = protocol.run_batch(adapter, names, a.runs, a.out, 'test', _checks(a.checks), log=_log,
                                                   rules=not a.facts_only)
    _log(f"done: {os.path.join(folder, 'report', 'REPORT.md')}")
    if a.facts_only:
        _log(f'the rules were not applied; `python3 -B -m stopkit summarize {folder}` applies them')
    return 0 if not problems and all(not r['errors'] for r in records) else 1


def cmd_logs(a) -> int:
    from .report import logs
    texts, bad = [], 0
    for d in a.folders:
        try:
            texts.append(logs.run_logs(d))
        except (OSError, ValueError, KeyError) as e:
            _log(f'{d}: cannot be read: {type(e).__name__}: {e}')
            bad += 1
    text = '\n\n'.join(texts) + '\n'
    if a.out:
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        _log(f'written: {a.out}')
    else:
        sys.stdout.write(text)
    return 1 if bad else 0


def cmd_summarize(a) -> int:
    from .run import protocol
    bad = 0
    for d in a.folders:
        records, problems = protocol.summarize(d, log=_log)
        for p in problems:
            _log(f'  {p}')
        bad += bool(problems) or not records
        if records:
            _log(f"done: {os.path.join(d, 'report', 'REPORT.md')}")
        else:
            _log(f'{d}: no run folder to summarize')
    return 1 if bad else 0


def cmd_report(a) -> int:
    from .report import reports
    res = reports.write(a.folders, a.out)
    _log(f"report of {res['runs']} runs ({res['rows']} rows): {os.path.join(a.out, 'REPORT.md')} and REPORT.html")
    return 0


def cmd_watch(a) -> int:
    if a.step != 'report':
        problems = guards.root_problems() + guards.disk_problems()
        if problems:
            raise KitError('; '.join(problems))
    if a.folder is None:
        if a.step not in ('baseline', 'before') or not a.account:
            raise KitError('start with `watch baseline --account U` or `watch before --account U`; later steps '
                           'take the watch folder')
        folder = watch.new_check(a.account, a.out)
    else:
        folder = a.folder
    if a.step == 'report':
        watch.report(folder)
        _log(f"score sheet: {os.path.join(folder, 'report.md')}")
        return 0
    if a.step == 'restart' and not a.yes:
        raise KitError("restarting the account's service manager stops every service it runs; add --yes to do it")
    cp = watch.add(folder, a.step, restart=a.step == 'restart')
    _log(f"{cp['name']}: listed into {folder}")
    if cp.get('restart_problem'):
        _log(f"the restart is not confirmed: {cp['restart_problem']}")
    return 0


def cmd_recheck(a) -> int:
    bad = 0
    for d in a.folders:
        same = 'same verdicts from the evidence'
        try:
            if os.path.exists(os.path.join(d, 'check.json')):
                problems = watch.recheck(d)
            else:
                with open(os.path.join(d, 'record.json'), encoding='utf-8') as f:
                    rec = json.load(f)
                if rec.get('format') == 'stopkit-record/2':
                    from .run import protocol
                    from .report import reports
                    problems = protocol.recheck(d)
                    if not reports.rules_applied(rec):
                        same = 'same readings and facts from the evidence (facts only: no verdicts yet)'
                else:
                    problems = record.recheck(d)
        except Exception as e:                   # one unreadable folder must not stop the others being checked
            problems = [f'could not be checked: {type(e).__name__}: {e}']
        _log(f"{d}: {same if not problems else 'DIFFERENCES'}")
        for p in problems:
            _log(f'  {p}')
        bad += bool(problems)
    return 1 if bad else 0


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


def cmd_smoke_check(a) -> int:
    from .run import protocol
    paths = sorted(os.path.join(a.folder, d, 'record.json') for d in os.listdir(a.folder)
                   if os.path.isfile(os.path.join(a.folder, d, 'record.json')))
    problems, notes, judged = [], [], 0
    for path in paths:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        if rec['stop_spec']['adapter_stop'] not in ('none', 'reference'):
            continue                             # only these two stops have known answers
        if (rec.get('rules') or {}).get('applied') is False:
            problems.append(f"{rec['folder']}: made with --facts-only; run `stopkit summarize {a.folder}` first")
            continue
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
    from .run import e0
    passed, folder = e0.run(a.out, a.trials, a.fires, log=_log, binding=a.binding)
    _log(f"E0 {'PASSED' if passed else 'FAILED'}: {os.path.join(folder, 'E0.md')}")
    return 0 if passed else 1


def cmd_noninterference(a) -> int:
    from .run import noninterference
    passed, folder = noninterference.run_all(a.target, a.stop, a.out, log=_log)
    _log(f"N1–N7 {'PASSED' if passed else 'FAILED'}: {os.path.join(folder, 'NONINTERFERENCE.md')}")
    return 0 if passed else 1


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


def _runs(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return n


TARGET = 'an agent\'s config file (FILE.toml) or a ready-made binding: ' + ', '.join(bindings.NAMES)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='stopkit', description='Tests from outside whether stopping an AI agent '
                                'really stops its work. Run as root on a Linux machine with systemd.')
    p.add_argument('--version', action='version', version=f'stopkit {__version__}')
    sub = p.add_subparsers(dest='command', required=True, metavar='command')
    i = sub.add_parser('init', help='write a config file template for your agent')
    i.add_argument('file', nargs='?', default='agent.toml')
    i.set_defaults(func=cmd_init)
    d = sub.add_parser('doctor', help="check the machine and an agent's config file, without testing a stop")
    d.add_argument('file', help="the agent's config file")
    d.add_argument('--out', default='results')
    d.set_defaults(func=cmd_doctor)
    c = sub.add_parser('check', help="test an agent's stops with the ground truth")
    c.add_argument('target', help=TARGET)
    c.add_argument('--stop', action='append', help='a stop to test; give it several times for several (default: all)')
    c.add_argument('--runs', type=_runs, default=3, help='runs of each stop (default 3)')
    c.add_argument('--checks', help="checks in seconds after the stop (default: the scenario's)")
    c.add_argument('--facts-only', action='store_true', help="record what the kit read and what the work did, "
                   "without applying the kit's rules (no verdicts); `stopkit summarize` applies them later")
    c.add_argument('--out', default='results')
    c.set_defaults(func=cmd_check)
    w = sub.add_parser('watch', help='list what an account still runs around a stop you make yourself')
    w.add_argument('step', choices=WATCH_STEPS)
    w.add_argument('folder', nargs='?', help='the watch folder (made by the first step)')
    w.add_argument('--account', help='the account the agent runs as (first step only)')
    w.add_argument('--out', default='results')
    w.add_argument('--yes', action='store_true', help="confirm the restart of the account's service manager")
    w.set_defaults(func=cmd_watch)
    rp = sub.add_parser('report', help='write the report from run folders')
    rp.add_argument('folders', nargs='+')
    rp.add_argument('--out', required=True)
    rp.set_defaults(func=cmd_report)
    r = sub.add_parser('recheck', help='score run or watch folders again from their evidence')
    r.add_argument('folders', nargs='+')
    r.set_defaults(func=cmd_recheck)
    lg = sub.add_parser('logs', help="a run's raw logs, with none of the kit's rules applied")
    lg.add_argument('folders', nargs='+', help='run folders')
    lg.add_argument('--out', help='write them to this file instead of printing them')
    lg.set_defaults(func=cmd_logs)
    sm = sub.add_parser('summarize', help='apply the rules to a batch made with check --facts-only')
    sm.add_argument('folders', nargs='+', help='batch folders')
    sm.set_defaults(func=cmd_summarize)
    sub.add_parser('setup', help='create the accounts and sentinels').set_defaults(func=cmd_setup)
    sub.add_parser('status', help='show what the kit sees now').set_defaults(func=cmd_status)
    s = sub.add_parser('selftest', help='check the kit on known answers')
    s.add_argument('--runs', type=_runs, default=3)
    s.add_argument('--out', default='results')
    s.set_defaults(func=cmd_selftest)
    cl = sub.add_parser('cleanup', help='return the agent account to empty')
    cl.add_argument('--all', action='store_true', help='also remove the sentinels and the accounts')
    cl.set_defaults(func=cmd_cleanup)

    dev = sub.add_parser('dev', help='checks of the kit itself, for binding authors')
    dsub = dev.add_subparsers(dest='dev_command', required=True, metavar='check')
    e = dsub.add_parser('e0', help='check the ground truth on its own')
    e.add_argument('--trials', type=_runs, default=30)
    e.add_argument('--fires', action='store_true', help='also time real timers, cron and at (about 4 more minutes)')
    e.add_argument('--binding', choices=bindings.NAMES, help="also check the parts of the truth this binding's "
                   'scenario uses: work inside a process, the HTTP stand-in')
    e.add_argument('--out', default='results')
    e.set_defaults(func=cmd_e0)
    ni = dsub.add_parser('noninterference', help="check that the kit's observers and instruments do not change "
                         'what they measure (N1–N7)')
    ni.add_argument('target', help=TARGET)
    ni.add_argument('--stop', action='append', required=True, help='a stop to run with and without the instruments, '
                    'and at two polling rates; give it several times for several')
    ni.add_argument('--out', default='results')
    ni.set_defaults(func=cmd_noninterference)
    sc = dsub.add_parser('smoke-check', help="hold a binding's smoke batch to the known answers")
    sc.add_argument('folder', help='the batch folder that `check TARGET --stop none --stop reference` wrote')
    sc.set_defaults(func=cmd_smoke_check)
    t = dsub.add_parser('test', help='test stops of an adapter without the ground truth (record format 1)')
    t.add_argument('--adapter', default='pretend', choices=bindings.NAMES)
    t.add_argument('--stop', action='append', required=True, help='a stop of the adapter; give it several times for several')
    t.add_argument('--runs', type=_runs, default=1)
    t.add_argument('--at', default='0,60,300', help='checks, in seconds after the cut-off (default 0,60,300)')
    t.add_argument('--out', default='results')
    t.set_defaults(func=cmd_test)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in RENAMED:
        print(f'stopkit: `{argv[0]}` is now `{RENAMED[argv[0]]}`', file=sys.stderr)
        return 2
    a = parser().parse_args(argv)
    try:
        if a.command in ('status', 'recheck', 'report', 'logs', 'init') or \
                getattr(a, 'dev_command', None) == 'smoke-check':
            return a.func(a)                     # these only read, or write a file of their own
        with guards.single_run():
            return a.func(a)
    except KitError as e:
        print(f'stopkit: {e}', file=sys.stderr)
        return 2
