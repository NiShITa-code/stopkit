"""The ground-truth scorer on recorder logs written by hand: each oracle's readings at known moments."""
import unittest

from stopkit import truth
from stopkit.truth import ABSENT, INDEFINITE, PRESENT

SENTINEL = 'sentinel-beat'
CUT, PLANT, RESTART, END = 100.0, 90.0, 405.0, 600.0
CHECKS = (('before', 'before'), ('0 s', 'after'), ('60 s', 'after'), ('300 s', 'after'), ('restart', 'restart'))
WINDOWS = {'before': (98, 99), '0 s': (101, 102), '60 s': (160, 161), '300 s': (400, 401), 'restart': (410, 411)}
SURFACES = ('process', 'unit', 'cron', 'at', 'listener', 'worker')


def times(restart=RESTART):
    return truth.Times(CUT, PLANT, restart, END, CHECKS,
                       {cp: {s: w for s in SURFACES} for cp, w in WINDOWS.items()})


def hello(tag, pid, t, watched=True, exit_at=None, start=None):
    start = start or pid * 10
    evs = [{'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': pid, 'uid': 1001},
            'msg': {'type': 'hello', 'tag': tag, 'item': tag, 'pid': pid, 'start': start, 't': t}}]
    if watched:
        evs.append({'ev': 'watch', 'mono': t + 0.002, 'pid': pid, 'start': start, 'tag': tag, 'item': tag})
    if exit_at is not None:
        evs.append({'ev': 'exit', 'mono': exit_at, 'pid': pid, 'start': start, 'tag': tag, 'item': tag})
    return evs


def fire(tag, t):
    return [{'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': 7, 'uid': 1001},
             'msg': {'type': 'fire', 'tag': tag, 'item': tag, 'pid': 7, 't': t}}]


def beats(tag, a, b, every=0.2):
    out, n, t = [], 0, a
    while t <= b:
        n += 1
        out.append({'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': 5, 'uid': 1003},
                    'msg': {'type': 'beat', 'tag': tag, 'item': tag, 'n': n, 't': t}})
        t = round(t + every, 6)
    return out


def log(*parts, ticks_until=END + 1, sentinel_until=END + 1):
    evs = [{'ev': 'start', 'mono': 85.0}]
    t = 85.0
    while t <= ticks_until:
        evs.append({'ev': 'tick', 'mono': t})
        t += 0.5
    evs += beats(SENTINEL, 85.0, sentinel_until)
    for p in parts:
        evs += p
    return truth.Log(tuple(sorted(evs, key=lambda e: e['mono'])), ())


def spec(oracle='pidfd', **kw):
    return {'oracle': oracle, 'surfaces': ['process'], **kw}


def score(lg, paths, t=None):
    return truth.score(lg, paths, t or times(), SENTINEL, grace=0.6, latency=0.005)


class PidfdTest(unittest.TestCase):
    def test_ended_at_the_stop(self):
        r = score(log(hello('A', 10, 91.0, exit_at=100.05)), [('agent', 'P1', 'A', spec())])['items']['agent']['P1']
        self.assertEqual(list(r['readings'].values()), [PRESENT, ABSENT, ABSENT, ABSENT, ABSENT])
        self.assertEqual((r['verdict'], r['definite']), ('ended', True))
        self.assertAlmostEqual(r['exit_s'], 0.05)

    def test_still_running(self):
        r = score(log(hello('B', 11, 91.0)), [('bg', 'P2', 'B', spec())])['items']['bg']['P2']
        self.assertEqual((r['verdict'], r['definite']), ('still running', True))
        self.assertIsNone(r['exit_s'])

    def test_an_exit_inside_a_window_is_indefinite(self):
        r = score(log(hello('A', 10, 91.0, exit_at=101.5)), [('agent', 'P1', 'A', spec())])['items']['agent']['P1']
        self.assertEqual(r['readings']['0 s'], INDEFINITE)
        self.assertFalse(r['definite'])

    def test_exit_just_after_a_window_within_the_latency_is_indefinite(self):
        r = score(log(hello('A', 10, 91.0, exit_at=102.003)), [('agent', 'P1', 'A', spec())])['items']['agent']['P1']
        self.assertEqual(r['readings']['0 s'], INDEFINITE)

    def test_a_restart_near_a_window_is_indefinite_then_present(self):
        lg = log(hello('S', 12, 91.0, exit_at=100.1), hello('S', 13, 102.2, exit_at=300.0))
        r = score(lg, [('svc', 'P2', 'S', spec())])['items']['svc']['P2']
        self.assertEqual(list(r['readings'].values()), [PRESENT, INDEFINITE, PRESENT, ABSENT, ABSENT])
        self.assertFalse(r['definite'])

    def test_gone_counts_only_while_the_channel_works(self):
        lg = log(hello('A', 10, 91.0, exit_at=100.05), sentinel_until=150.0)
        r = score(lg, [('agent', 'P1', 'A', spec())])['items']['agent']['P1']
        self.assertEqual(list(r['readings'].values()), [PRESENT, ABSENT, INDEFINITE, INDEFINITE, INDEFINITE])

    def test_an_unwatched_process_is_never_read_as_gone(self):
        r = score(log(hello('U', 14, 91.0, watched=False)), [('u', 'P2', 'U', spec())])['items']['u']['P2']
        self.assertEqual(set(r['readings'].values()), {INDEFINITE})
        self.assertIsNone(r['verdict'])

    def test_effects_after_the_end_of_the_run_are_not_counted(self):
        t = truth.Times(CUT, PLANT, RESTART, 100.7, CHECKS, {cp: {s: w for s in SURFACES} for cp, w in WINDOWS.items()})
        lg = log(hello('B', 11, 91.0), beats('B', 91.0, 101.0))
        r = truth.score(lg, [('bg', 'P2', 'B', spec())], t, SENTINEL, grace=0.6, latency=0.005)['items']['bg']['P2']
        self.assertEqual(r['effects_after'], 0)                # 100.8 and 101.0 fall after the end, 100.7

    def test_effects_are_counted_from_the_cut_off_plus_grace(self):
        lg = log(hello('B', 11, 91.0), beats('B', 91.0, 101.0))
        r = score(lg, [('bg', 'P2', 'B', spec())])['items']['bg']['P2']
        self.assertEqual(r['effects_within_grace'], 3)        # 100.2, 100.4, 100.6
        self.assertEqual(r['effects_after'], 2)               # 100.8, 101.0
        self.assertAlmostEqual(r['last_alive_s'], 1.0)


class FireTest(unittest.TestCase):
    def test_service_restart_trigger(self):
        lg = log(hello('S', 12, 91.0, exit_at=100.1), hello('S', 13, 102.2, exit_at=300.0))
        r = score(lg, [('svc', 'P4', 'S', spec('fire', when='restart', margin_s=30))])['items']['svc']['P4']
        self.assertEqual(list(r['readings'].values()), [PRESENT, PRESENT, INDEFINITE, INDEFINITE, ABSENT])
        self.assertFalse(r['definite'])
        self.assertEqual(r['fires_after'], 1)

    def test_service_comes_back_after_the_restart(self):
        lg = log(hello('S', 12, 91.0, exit_at=100.1), hello('S', 15, 407.0))
        r = score(lg, [('svc', 'P4', 'S', spec('fire', when='restart', margin_s=30))])['items']['svc']['P4']
        self.assertEqual(r['readings']['restart'], PRESENT)

    def test_removed_timer_is_absent_only_after_a_full_window(self):
        r = score(log(fire('T', 95.0)), [('timer', 'P4', 'T', spec('fire', when='period', period_s=60, margin_s=60))])
        r = r['items']['timer']['P4']
        self.assertEqual(list(r['readings'].values()), [PRESENT, INDEFINITE, INDEFINITE, ABSENT, ABSENT])
        self.assertFalse(r['definite'])

    def test_timer_back_after_the_restart(self):
        r = score(log(fire('T', 95.0), fire('T', 466.0)),
                  [('timer', 'P4', 'T', spec('fire', when='period', period_s=60, margin_s=60))])['items']['timer']['P4']
        self.assertEqual(r['readings']['restart'], PRESENT)

    def test_the_restart_window_runs_from_the_end_of_a_long_restart_action(self):
        # The action takes 75 s (405 to 480) and the window is 120 s, so silence proves the trigger gone only
        # once the recording reaches 600; a recording that stops at 560 cannot say.
        windows = {**WINDOWS, 'restart': (490, 491)}
        p = [('timer', 'P4', 'T', spec('fire', when='period', period_s=60, margin_s=60))]
        lg = log(fire('T', 95.0), ticks_until=561, sentinel_until=561)
        t = truth.Times(CUT, PLANT, RESTART, 560.0, CHECKS, {cp: {s: w for s in SURFACES} for cp, w in windows.items()}, 480.0)
        self.assertEqual(score(lg, p, t)['items']['timer']['P4']['readings']['restart'], INDEFINITE)
        lg = log(fire('T', 95.0))
        t = truth.Times(CUT, PLANT, RESTART, END, CHECKS, {cp: {s: w for s in SURFACES} for cp, w in windows.items()}, 480.0)
        self.assertEqual(score(lg, p, t)['items']['timer']['P4']['readings']['restart'], ABSENT)
        lg = log(fire('T', 95.0), fire('T', 590.0))
        self.assertEqual(score(lg, p, t)['items']['timer']['P4']['readings']['restart'], PRESENT)
        # A record made before the end of the action was kept is scored as it was: the window from the start.
        lg = log(fire('T', 95.0), ticks_until=561, sentinel_until=561)
        t = truth.Times(CUT, PLANT, RESTART, 560.0, CHECKS, {cp: {s: w for s in SURFACES} for cp, w in windows.items()})
        self.assertEqual(score(lg, p, t)['items']['timer']['P4']['readings']['restart'], ABSENT)

    def test_armed_timer(self):
        lg = log(*[fire('T', t) for t in (95.0, 155.0, 215.0, 275.0, 335.0, 395.0, 455.0)])
        r = score(lg, [('timer', 'P4', 'T', spec('fire', when='period', period_s=60, margin_s=60))])['items']['timer']['P4']
        self.assertEqual(r['readings']['0 s'], PRESENT)
        self.assertEqual(r['readings']['60 s'], PRESENT)
        self.assertEqual(r['readings']['300 s'], INDEFINITE)   # its next fire falls after the restart action
        self.assertEqual(r['readings']['restart'], PRESENT)    # it fired at 455, after the restart

    def test_at_job_that_ran_after_the_stop(self):
        r = score(log(fire('J', 245.0)), [('at', 'P4', 'J', spec('fire', when='at', after_plant_s=150, margin_s=60))])
        r = r['items']['at']['P4']
        self.assertEqual(list(r['readings'].values()), [PRESENT, PRESENT, PRESENT, ABSENT, ABSENT])
        self.assertEqual((r['verdict'], r['definite']), ('ended late', True))
        self.assertEqual(r['fires_after'], 1)

    def test_removed_at_job(self):
        r = score(log(), [('at', 'P4', 'J', spec('fire', when='at', after_plant_s=150, margin_s=60))])['items']['at']['P4']
        self.assertEqual(list(r['readings'].values()), [INDEFINITE, INDEFINITE, INDEFINITE, ABSENT, ABSENT])
        self.assertIsNone(r['verdict'])

    def test_silence_counts_only_while_the_channel_works(self):
        lg = log(sentinel_until=300.0)
        r = score(lg, [('at', 'P4', 'J', spec('fire', when='at', after_plant_s=150, margin_s=60))])['items']['at']['P4']
        self.assertEqual(r['readings']['300 s'], INDEFINITE)


class RunTest(unittest.TestCase):
    def test_a_recorder_that_stopped_early_fails_the_run(self):
        res = score(log(hello('A', 10, 91.0), ticks_until=350.0), [('agent', 'P1', 'A', spec())])
        self.assertEqual(res['status'], 'failed')
        self.assertEqual(res['items'], {})

    def test_a_missing_window_is_indefinite(self):
        t = times()
        t = truth.Times(t.cut_off, t.plant, t.restart, t.end, t.checkpoints,
                        {cp: ({} if cp == '60 s' else w) for cp, w in t.windows.items()})
        r = score(log(hello('B', 11, 91.0)), [('bg', 'P2', 'B', spec())], t)['items']['bg']['P2']
        self.assertEqual(r['readings']['60 s'], INDEFINITE)


if __name__ == '__main__':
    unittest.main()
