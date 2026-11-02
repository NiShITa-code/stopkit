"""The parts of the core that platform bindings need: the effect surface, the beats oracle, the HTTP stand-in,
the in-process reporter, the component listing and its restart confirmation, and the egress rule."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from stopkit import codehash, egress, specs, truth
from stopkit.observe import effects
from stopkit.observe.components import SENTINEL_KEY, list_components
from stopkit.observe.model import Item, Listing
from stopkit.observe.snapshot import Snapshot
from stopkit.sentinel import seen as sentinel_seen
from stopkit.verdict import restart_problem
from tests.helpers import AGENT, SENTINEL, FakeRunner, result, snap

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(os.path.abspath(truth.__file__))  # the stopkit package, in any layout
S = 'stopkit-sentinel-beat'
H = 'stopkit-sentinel-http'


def effect(tag, t=None, arrival=None, mono=None, **kw):
    msg = {'tag': tag, 'item': tag, 'n': 1, **kw}
    if t is not None:
        msg['t'] = t
    if arrival is not None:
        msg['arrival'] = arrival
        msg['via'] = 'http'
    return {'ev': 'effect', 'mono': mono if mono is not None else (arrival if arrival is not None else t) + 0.001, 'msg': msg}


def sentinel_effects(tag, a, b, every=0.2):
    out, t = [], a
    while t <= b:
        out.append(effect(tag, t=t))
        t = round(t + every, 6)
    return out


def write_log(events):
    f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False, encoding='utf-8')
    for e in events:
        f.write(json.dumps(e) + '\n')
    f.close()
    return f.name


class EffectSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            os.unlink(p)

    def listing(self, events, now, since=None, lookback=2.0, sentinels=(S,), delay=0.0):
        p = write_log(events)
        self.paths.append(p)
        return effects.list_effects(p, now, since, lookback, list(sentinels), 1.0, delay)

    def test_work_acting_in_the_window_is_listed_and_the_window_is_proven(self):
        l = self.listing(sentinel_effects(S, 90, 110) + [effect('W', t=99.5)], now=100.0)
        self.assertTrue(l.ok)
        self.assertEqual([i.key for i in l.items], ['W', 'stopkit-sentinel-effect'])
        self.assertTrue(l.items[0].fields['in_force'])

    def test_silence_proves_an_absence_only_after_a_whole_lookback_since_the_stop(self):
        evs = sentinel_effects(S, 90, 120) + [effect('W', t=100.1)]
        early = self.listing(evs, now=101.5, since=100.6)          # 0.9 s of window: too soon
        self.assertEqual([i.key for i in early.items], [])
        late = self.listing(evs, now=103.0, since=100.6)
        self.assertEqual([i.key for i in late.items], ['stopkit-sentinel-effect'])

    def test_an_effect_before_the_cut_off_plus_grace_does_not_count_after_the_stop(self):
        evs = sentinel_effects(S, 90, 120) + [effect('W', t=100.5)]
        self.assertNotIn('W', [i.key for i in self.listing(evs, now=103.0, since=100.6).items])
        self.assertIn('W', [i.key for i in self.listing(evs + [effect('W', t=101.2)], now=103.0, since=100.6).items])

    def test_effects_that_had_not_arrived_are_not_known(self):
        evs = sentinel_effects(S, 90, 120) + [effect('W', t=102.9, mono=103.5)]
        self.assertNotIn('W', [i.key for i in self.listing(evs, now=103.0).items])

    def test_a_dead_sentinel_leaves_the_window_unproven(self):
        evs = sentinel_effects(S, 90, 101.5)
        self.assertEqual([i.key for i in self.listing(evs, now=103.0).items], [])

    def test_every_path_must_be_shown(self):
        evs = sentinel_effects(S, 90, 120)
        self.assertEqual([i.key for i in self.listing(evs, now=103.0, sentinels=(S, H)).items], [])
        evs += [effect(H, arrival=t) for t in [90 + i * 0.2 for i in range(150)]]
        self.assertEqual([i.key for i in self.listing(evs, now=103.0, sentinels=(S, H)).items], ['stopkit-sentinel-effect'])

    def test_work_placed_by_its_arrival_counts_only_when_surely_inside(self):
        evs = sentinel_effects(S, 90, 120) + [effect('W', arrival=101.0)]
        # It may have been issued as early as 100.95: inside a window from 100.6, but not from 100.98.
        self.assertIn('W', [i.key for i in self.listing(evs, now=103.0, since=100.6, lookback=3.0, delay=0.05).items])
        self.assertNotIn('W', [i.key for i in self.listing(evs, now=103.0, since=100.98, lookback=3.0, delay=0.05).items])


class TruthWindowTest(unittest.TestCase):
    def test_the_truth_reads_the_window_the_effect_surface_read(self):
        from stopkit import protocol
        raw = json.dumps({'now': 103.0, 'since': 101.0, 'lookback_s': 2.0})
        eff = Listing('effect', 'all', True, (), 0.0, (), (('effect-window', raw),))
        proc = Listing('process', 'x', True, (), 0.0)
        s = Snapshot(0.0, 0.0, (proc, eff), (100.0, 104.0), ((102.9, 103.0), (103.0, 103.01)))
        w = protocol._surface_windows(s, AGENT, ['effect', 'process'])
        self.assertEqual(w['effect'], [101.0, 103.0])            # the window it read, not the moment it ran
        self.assertEqual(w['process'], [102.9, 103.0])


class ActedAfterTest(unittest.TestCase):
    def test_stamped_effects_are_unchanged(self):
        evs = [effect('W', t=t) for t in (99.0, 100.3, 101.0, 101.5)]
        got = effects.acted_after(evs, 'W', 100.0, 0.6, None)
        self.assertEqual(got, {'count': 2, 'within_grace': 1, 'last_s': 1.5, 'received': 4, 'unproven': None})

    def test_an_arrival_that_may_be_before_the_grace_is_indeterminate(self):
        evs = [effect('W', arrival=a) for a in (100.62, 100.9, 101.5)]
        got = effects.acted_after(evs, 'W', 100.0, 0.6, None, delay=0.05)
        self.assertEqual((got['count'], got['indeterminate']), (2, 1))


def tlog(*events, until=200.0):
    evs = [{'ev': 'start', 'mono': 80.0}]
    t = 80.0
    while t <= until:
        evs.append({'ev': 'tick', 'mono': t})
        evs.append({'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': 5, 'uid': 1002},
                    'msg': {'type': 'beat', 'tag': S, 'item': 'channel', 'n': 1, 't': t}})
        t = round(t + 0.2, 6)
    evs += list(events)
    return truth.Log(tuple(sorted(evs, key=lambda e: e['mono'])), ())


def tmsg(kind, t, inst='i1', pid=40, tag='W', **kw):
    m = {'type': kind, 'tag': tag, 'item': tag, 't': t, 'inst': inst, **kw}
    return {'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': pid, 'uid': 1001}, 'msg': m}


def tbeats(a, b, inst='i1', every=0.2):
    out, t = [], a
    while t <= b:
        out.append(tmsg('beat', t, inst))
        t = round(t + every, 6)
    return out


class BeatsOracleTest(unittest.TestCase):
    def read(self, lg, w, delay=0.0):
        return truth.read_beats(lg, 'W', w, delay, 0.05, S)

    def test_alive_through_the_window(self):
        lg = tlog(tmsg('hello', 90.0), *tbeats(90.1, 150))
        self.assertEqual(self.read(lg, (100, 101)), truth.PRESENT)

    def test_an_end_hook_before_the_window_reads_absent(self):
        lg = tlog(*tbeats(90.1, 99.0), tmsg('end', 99.1))
        self.assertEqual(self.read(lg, (100, 101)), truth.ABSENT)

    def test_silence_without_an_end_is_indefinite(self):
        lg = tlog(*tbeats(90.1, 99.0))
        self.assertEqual(self.read(lg, (100, 101)), truth.INDEFINITE)

    def test_the_end_of_the_hosting_process_ends_the_work(self):
        hello = {'ev': 'msg', 'mono': 90.001, 'cred': {'pid': 40, 'uid': 1001},
                 'msg': {'type': 'hello', 'tag': 'W', 'item': 'W', 'pid': 40, 'start': 7, 't': 90.0, 'inst': 'i1', 'inproc': True}}
        exit_ = {'ev': 'exit', 'mono': 99.2, 'pid': 40, 'start': 7, 'tag': 'W', 'item': 'W'}
        lg = tlog(hello, *tbeats(90.1, 99.0), exit_)
        self.assertEqual(self.read(lg, (100, 101)), truth.ABSENT)
        self.assertEqual(self.read(lg, (98, 99.1)), truth.INDEFINITE)

    def test_a_retry_is_a_run_of_its_own(self):
        lg = tlog(*tbeats(90.1, 99.0, 'i1'), tmsg('end', 99.1, 'i1'), *tbeats(100.5, 150, 'i2'))
        self.assertEqual(self.read(lg, (100, 101)), truth.INDEFINITE)     # a run started inside the window
        self.assertEqual(self.read(lg, (110, 111)), truth.PRESENT)

    def test_work_placed_by_its_arrival(self):
        evs = [{'ev': 'msg', 'mono': a + 0.002, 'cred': {'pid': 3, 'uid': 0},
                'msg': {'type': 'beat', 'tag': 'W', 'item': 'W', 'n': 1, 't': None, 'arrival': a, 'inst': 'e7', 'via': 'http'}}
               for a in [90 + i for i in range(30)]]
        lg = tlog(*evs)
        self.assertEqual(self.read(lg, (100, 101), delay=0.05), truth.PRESENT)

    def test_scored_entry(self):
        lg = tlog(*tbeats(90.1, 101.4), tmsg('end', 101.5))
        times = truth.Times(100.0, 85.0, None, 190.0, (('before', 'before'), ('3 s', 'after')),
                            {'before': {'effect': (99, 99.5)}, '3 s': {'effect': (103, 103.1)}})
        got = truth.score(lg, [('w', 'P1', 'W', {'oracle': 'beats', 'surfaces': ['effect']})], times, S, 0.6, 0.05)
        e = got['items']['w']['P1']
        self.assertEqual((e['readings']['before'], e['readings']['3 s'], e['verdict']), ('present', 'absent', 'ended'))
        self.assertEqual(e['effects_after'], 4)                 # 100.8, 101.0, 101.2, 101.4
        self.assertEqual(e['exit_s'], 1.5)


class RestartReadTest(unittest.TestCase):
    def test_an_item_the_restart_starts_again_is_not_read_after_it(self):
        hello = [{'ev': 'msg', 'mono': 90.001, 'cred': {'pid': 40, 'uid': 1001},
                  'msg': {'type': 'hello', 'tag': 'C', 'item': 'C', 'pid': 40, 'start': 7, 't': 90.0}},
                 {'ev': 'watch', 'mono': 90.002, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'},
                 {'ev': 'exit', 'mono': 100.1, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'},
                 {'ev': 'msg', 'mono': 150.001, 'cred': {'pid': 41, 'uid': 1001},       # the restart starts it again
                  'msg': {'type': 'hello', 'tag': 'C', 'item': 'C', 'pid': 41, 'start': 9, 't': 150.0}},
                 {'ev': 'watch', 'mono': 150.002, 'pid': 41, 'start': 9, 'tag': 'C', 'item': 'C'}]
        lg = tlog(*hello)
        cps = (('before', 'before'), ('10 s', 'after'), ('restart', 'restart'))
        times = truth.Times(100.0, 85.0, 140.0, 190.0, cps, {'before': {'component': (99, 99.5)},
                                                             '10 s': {'component': (110, 110.1)},
                                                             'restart': {'component': (160, 160.1)}})
        spec = {'oracle': 'pidfd', 'surfaces': ['component']}
        read = truth.score(lg, [('c', 'P1', 'C', spec)], times, S, 0.6, 0.05)['items']['c']['P1']
        self.assertEqual(read['verdict'], 'came back')
        skip = truth.score(lg, [('c', 'P1', 'C', dict(spec, restart_read=False))], times, S, 0.6, 0.05)['items']['c']['P1']
        self.assertEqual((skip['verdict'], skip['definite']), ('ended', True))

    def test_nor_is_its_report_judged_after_it(self):
        from stopkit import protocol
        from stopkit.verdict import truthfulness
        # As in n8n's reference runs of 29 Oct: the stop said everything had ended, and the restart action started
        # n8n's main process again. That made the report a false all-clear, though the verdict was ended.
        main = {'id': 'main', 'kind': 'P1', 'verdict': 'ended',
                'readings': {'before': 'present', '10 s': 'absent', 'restart': 'present'}}
        after, restart = [{'name': '10 s'}], {'name': 'restart'}
        self.assertEqual(protocol.seen_after([main], after, restart, set()), {'main/P1': True})
        seen = protocol.seen_after([main], after, restart, {'main'})
        self.assertEqual(seen, {'main/P1': False})
        self.assertEqual(truthfulness(True, {'main/P1': 'ended'}, seen)[0], 'true')
        self.assertEqual(protocol.seen_after([dict(main, readings={**main['readings'], '10 s': 'present'})], after,
                                             restart, {'main'}), {'main/P1': True})


class RunTimingTest(unittest.TestCase):
    """The run's waits around the 'before' listing and after the last listing (protocol.before_wait,
    recording_end), held to what the truth needs to read there."""
    def read(self, lg, w):
        return truth.read_beats(lg, 'W', w, 0.0, 0.05, S)

    def wait(self, surfaces=('effect',), lookback=2.0):
        from types import SimpleNamespace
        from stopkit import protocol
        return protocol.before_wait(SimpleNamespace(effect_lookback_s=lookback), list(surfaces))

    def test_only_work_read_on_the_effect_surface_waits(self):
        self.assertEqual(self.wait(('effect', 'process')), 2.0)
        self.assertEqual(self.wait(('process',)), 0.0)
        self.assertEqual(self.wait(('process',), lookback=None), 0.0)

    def test_work_that_began_just_before_readiness_is_confirmed_one_lookback_later(self):
        # As in each MCP run of 29 Oct: the work first reported 1.2 s before readiness (100.0).
        lg = tlog(tmsg('hello', 98.8), *tbeats(98.8, 150))
        self.assertEqual(self.read(lg, (98.0, 100.0)), truth.INDEFINITE)
        listed = 100.0 + self.wait()
        self.assertEqual(self.read(lg, (listed - 2.0, listed)), truth.PRESENT)

    def test_work_the_stop_ends_at_once_is_confirmed_by_acting_before_the_cut_off(self):
        # As in n8n's reference run of 29 Oct: the stop came 20 ms after the listing and ended the work, whose
        # end the truth could not see (no end hook ran), before it acted again.
        listed = 100.0
        self.assertEqual(self.read(tlog(*tbeats(50.0, listed)), (98.0, listed)), truth.INDEFINITE)
        cut = listed + self.wait()
        self.assertEqual(self.read(tlog(*tbeats(50.0, cut)), (98.0, listed)), truth.PRESENT)

    def test_the_recording_outlasts_the_last_listing_enough_to_read_it(self):
        from stopkit import protocol
        # As in each MCP run of 29 Oct (no trigger, so no trigger window): the recording ended 0.3 s after the
        # restart listing, so a process that had ended read indefinite there, and so did work still running,
        # torn down before it acted again.
        restarted, listed = 405.0, 411.0
        ended = [{'ev': 'msg', 'mono': 90.001, 'cred': {'pid': 40, 'uid': 1001},
                  'msg': {'type': 'hello', 'tag': 'C', 'item': 'C', 'pid': 40, 'start': 7, 't': 90.0}},
                 {'ev': 'watch', 'mono': 90.002, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'},
                 {'ev': 'exit', 'mono': 100.1, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'}]
        self.assertEqual(truth.read_pidfd(tlog(*ended, until=listed + 0.3), 'C', (410.0, listed), 0.05, S),
                         truth.INDEFINITE)
        self.assertEqual(self.read(tlog(*tbeats(90.0, listed + 0.05), until=listed + 0.3), (409.0, listed)),
                         truth.INDEFINITE)
        end = protocol.recording_end(restarted, 0.0, listed)
        self.assertEqual(end, listed + 2.5)
        self.assertEqual(truth.read_pidfd(tlog(*ended, until=end), 'C', (410.0, listed), 0.05, S), truth.ABSENT)
        self.assertEqual(self.read(tlog(*tbeats(90.0, end), until=end), (409.0, listed)), truth.PRESENT)
        self.assertEqual(protocol.recording_end(restarted, 120.0, listed), 526.0)     # a trigger's window is later

    def gap(self, *truths):
        from types import SimpleNamespace
        from stopkit import protocol
        return protocol.restart_gap([(SimpleNamespace(truth={'P': t}), 'P') for t in truths])

    def test_what_the_restart_starts_again_is_seen_to_start_after_the_last_listing(self):
        # As in n8n's kill and reference runs of 29 Oct: the main process had ended before the last check, and
        # the restart, begun 0.07 s after that listing, started it again 0.2 s after it.
        gone = [{'ev': 'msg', 'mono': 90.001, 'cred': {'pid': 40, 'uid': 1001},
                 'msg': {'type': 'hello', 'tag': 'C', 'item': 'C', 'pid': 40, 'start': 7, 't': 90.0}},
                {'ev': 'watch', 'mono': 90.002, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'},
                {'ev': 'exit', 'mono': 100.1, 'pid': 40, 'start': 7, 'tag': 'C', 'item': 'C'}]

        def again(t):
            return [{'ev': 'msg', 'mono': t + 0.001, 'cred': {'pid': 41, 'uid': 1001},
                     'msg': {'type': 'hello', 'tag': 'C', 'item': 'C', 'pid': 41, 'start': 9, 't': t}},
                    {'ev': 'watch', 'mono': t + 0.002, 'pid': 41, 'start': 9, 'tag': 'C', 'item': 'C'}]
        w = (119.0, 120.0)
        self.assertEqual(truth.read_pidfd(tlog(*gone, *again(120.2)), 'C', w, 0.05, S), truth.INDEFINITE)
        self.assertEqual(self.gap({'oracle': 'pidfd'}, {'oracle': 'beats'}), 1.5)
        self.assertEqual(truth.read_pidfd(tlog(*gone, *again(120.0 + 1.5 + 0.2)), 'C', w, 0.05, S), truth.ABSENT)

    def test_a_trigger_still_armed_can_fire_before_the_restart(self):
        # As in n8n's runs of 29 Oct: the schedule, still armed, fires every 60 s (margin 10 s); the last
        # listing ended at 120.0, and the restart began at once, before it could fire again.
        spec = {'oracle': 'fire', 'when': 'period', 'period_s': 60, 'margin_s': 10}
        lg = tlog(*[tmsg('fire', t, tag='T') for t in (55.0, 115.0, 175.0)], until=260.0)

        def read(restart):
            times = truth.Times(100.0, 50.0, restart, 260.0, (('120 s', 'after'),), {'120 s': {'t': (119.0, 120.0)}})
            return truth.read_fire(lg, 'T', spec, (119.0, 120.0), 'after', times, S)
        self.assertEqual(read(120.07), truth.INDEFINITE)
        self.assertEqual(self.gap(spec, {'oracle': 'fire', 'when': 'restart', 'margin_s': 30}), 70)
        self.assertEqual(read(120.0 + 70), truth.PRESENT)


class SmokeCheckTest(unittest.TestCase):
    """protocol.smoke_problems: the known answers of a binding's smoke run, and the one unknown it accepts."""

    def rec(self, stop='reference', after='unknown', ok=False, restart='absent', truth_after='absent', verdict=None):
        from stopkit import verdict as v
        readings = {'before': 'present', '45 s': after, 'restart': restart}
        cps = [{'name': 'before', 'kind': 'before', 'snapshot': {'listings': [{'surface': 'temporal-workflow', 'ok': True}]}},
               {'name': '45 s', 'kind': 'after', 'snapshot': {'listings': [{'surface': 'temporal-workflow', 'ok': ok}]}},
               {'name': 'restart', 'kind': 'restart', 'snapshot': {'listings': [{'surface': 'temporal-workflow', 'ok': True}]}}]
        item = {'id': 'timer', 'kind': 'P4', 'surfaces': ['temporal-workflow'], 'readings': readings,
                'verdict': verdict or v.final('present', [after], restart)[0]}
        tr = {'status': 'ok', 'items': {'timer': {'P4': {'readings': {'before': 'present', '45 s': truth_after,
                                                                      'restart': 'absent'}}}}}
        return {'run_id': 'r1', 'folder': 'run-1', 'stop_spec': {'id': f'temporal.{stop}', 'adapter_stop': stop},
                'errors': [], 'checkpoints': cps, 'items': [item], 'truth': tr}

    def test_an_unknown_while_the_store_was_down_is_accepted_and_noted(self):
        from stopkit import protocol
        problems, notes = protocol.smoke_problems(self.rec())
        self.assertEqual(problems, [])
        self.assertTrue(any('accepted unknown' in n for n in notes))

    def test_an_unknown_without_a_failed_listing_fails(self):
        from stopkit import protocol
        self.assertTrue(protocol.smoke_problems(self.rec(ok=True))[0])

    def test_an_unknown_that_comes_back_after_the_restart_fails(self):
        from stopkit import protocol
        self.assertTrue(protocol.smoke_problems(self.rec(restart='present'))[0])

    def test_an_unknown_the_truth_saw_present_fails(self):
        from stopkit import protocol
        self.assertTrue(protocol.smoke_problems(self.rec(truth_after='present'))[0])

    def test_a_false_absent_fails_and_a_false_present_is_noted(self):
        from stopkit import protocol
        problems, _ = protocol.smoke_problems(self.rec(after='absent', ok=True, truth_after='present'))
        self.assertTrue(any('kit absent, truth present' in p for p in problems))
        problems, notes = protocol.smoke_problems(self.rec(stop='none', after='present', ok=True, restart='present',
                                                           truth_after='absent'))
        self.assertEqual(problems, [])
        self.assertTrue(any('kit present, truth absent' in n for n in notes))

    def test_the_null_stop_wants_still_running(self):
        from stopkit import protocol
        problems, _ = protocol.smoke_problems(self.rec(stop='none', after='absent', ok=True))
        self.assertTrue(any('ended under none' in p for p in problems))


class ComponentsTest(unittest.TestCase):
    SHOW = ('Id=stopkit-n8n-main-x.service\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n'
            'MainPID=10\nInvocationID={inv}\nNRestarts=0\n\n'
            'Id=stopkit-worker.service\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n'
            'MainPID=11\nInvocationID=w\nNRestarts=0\n')

    def listing(self, inv='a'):
        fr = FakeRunner({('systemctl', 'show', '--no-pager', '-p', 'Id,LoadState,ActiveState,SubState,Result,MainPID,'
                          'InvocationID,NRestarts', '--', 'stopkit-n8n-main-x.service', 'stopkit-worker.service'):
                         result(self.SHOW.format(inv=inv))})
        return list_components(['stopkit-n8n-main-x.service'], fr)

    def test_listing_and_its_proof(self):
        l = self.listing()
        self.assertTrue(l.ok)
        self.assertEqual([i.key for i in l.items], ['stopkit-n8n-main-x.service', SENTINEL_KEY])
        self.assertTrue(l.items[0].fields['in_force'])
        s = Snapshot(0.0, 0.0, (l,))
        self.assertIsNone(sentinel_seen(s, SENTINEL)['component'])

    def test_restart_is_confirmed_by_new_invocations(self):
        a, b = Snapshot(0, 0, (self.listing('a'),)), Snapshot(0, 0, (self.listing('b'),))
        ok = [{'rc': 0, 'error': None, 'argv': ['systemctl', 'restart']}]
        self.assertIsNone(restart_problem(a, b, AGENT, ok, 'components'))
        self.assertIn('same invocation', restart_problem(a, a, AGENT, ok, 'components'))
        self.assertIn('failed', restart_problem(a, b, AGENT, [{'rc': 1, 'error': None, 'argv': ['x']}], 'components'))

    def test_the_manager_restart_is_unchanged(self):
        ok = [{'rc': 0, 'error': None}]
        self.assertIsNone(restart_problem(snap(invocation='a'), snap(invocation='b'), AGENT, ok))


class SpecTest(unittest.TestCase):
    def test_the_mcp_scenario_loads(self):
        sc = specs.scenario('mcp-bundle-1')
        self.assertEqual(sc.effect_lookback_s, 2.0)
        self.assertEqual([i.truth[k]['oracle'] for i in sc.items for k in i.paths], ['pidfd', 'beats', 'pidfd'])
        self.assertGreaterEqual(min(sc.checks_s), sc.grace_s + sc.effect_lookback_s)

    def test_the_host_scenario_keeps_its_defaults(self):
        sc = specs.scenario('host-bundle-1')
        self.assertIsNone(sc.effect_lookback_s)
        self.assertEqual((sc.effect_channels, sc.delivery_bound_s, sc.restart_settle_s), (('datagram',), 0.0, 3.0))

    def test_the_new_surfaces(self):
        surf = specs.surfaces()
        self.assertTrue(surf['effect'].implemented and surf['component'].implemented)


class CodeHashTest(unittest.TestCase):
    def test_stable_and_sensitive(self):
        self.assertEqual(codehash.code_sha256(), codehash.code_sha256())
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.py'), 'w') as f:
                f.write('x = 1\n')
            h1 = codehash.code_sha256(d)
            with open(os.path.join(d, 'a.py'), 'w') as f:
                f.write('x = 2\n')
            self.assertNotEqual(h1, codehash.code_sha256(d))


class EgressTest(unittest.TestCase):
    def test_the_rule_names_the_account_and_spares_loopback(self):
        fr = FakeRunner({('nft', 'delete', 'table', 'inet', egress.TABLE): result('', rc=1),
                         ('nft', '-f', '-'): result('')})
        out = egress.block(AGENT, fr)
        self.assertTrue(out[-1].ok)
        self.assertEqual(fr.calls[-1], ('nft', '-f', '-'))


class Datagrams:
    """Two Unix datagram sockets standing in for the truth recorder and the effect sink."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.socks = {}
        for name in ('truth', 'sink'):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.bind(os.path.join(self.dir, name))
            s.settimeout(5)
            self.socks[name] = s

    def path(self, name):
        return os.path.join(self.dir, name)

    def get(self, name):
        return json.loads(self.socks[name].recv(65536))

    def close(self):
        for s in self.socks.values():
            s.close()
        for name in self.socks:
            os.unlink(self.path(name))
        os.rmdir(self.dir)


@unittest.skipUnless(hasattr(socket, 'AF_UNIX') and sys.platform.startswith('linux'), 'Linux only')
class ReportersTest(unittest.TestCase):
    def setUp(self):
        self.d = Datagrams()

    def tearDown(self):
        self.d.close()

    def test_the_in_process_reporter(self):
        sys.path.insert(0, os.path.join(KIT, 'adapters'))
        try:
            from work_report import Work
        finally:
            sys.path.pop(0)
        w = Work(self.d.path('truth'), self.d.path('sink'), 'T', 'request')
        w.hello()
        w.beat()
        w.end('done')
        hello, beat, end = (self.d.get('truth') for _ in range(3))
        self.assertEqual((hello['type'], hello['pid'], hello['inproc']), ('hello', os.getpid(), True))
        self.assertEqual((beat['type'], beat['n'], beat['inst']), ('beat', 1, hello['inst']))
        self.assertEqual((end['type'], end['why']), ('end', 'done'))
        self.assertEqual(self.d.get('sink')['n'], 1)

    def test_exec_reports_then_becomes_the_program(self):
        p = subprocess.run([sys.executable, '-I', '-B', os.path.join(KIT, 'adapters', 'beat.py'), 'exec',
                            self.d.path('truth'), 'T', 'server', '--', '/bin/sh', '-c', 'echo $$'],
                           capture_output=True, text=True, timeout=20)
        hello = self.d.get('truth')
        self.assertEqual(hello['type'], 'hello')
        self.assertEqual(int(p.stdout.strip()), hello['pid'])      # the same process became the program

    def test_the_http_stand_in_passes_reports_on(self):
        log = os.path.join(self.d.dir, 'standin.jsonl')
        p = subprocess.Popen([sys.executable, '-I', '-B', os.path.join(KIT, 'standin_http.py'), self.d.path('truth'),
                              self.d.path('sink'), log], stdout=subprocess.PIPE, text=True)
        try:
            port = int(p.stdout.readline().split()[1])
            body = json.dumps({'tag': 'W', 'item': 'loop', 'n': 3, 'inst': 'e9', 'kind': 'beat'}).encode()
            req = urllib.request.Request(f'http://127.0.0.1:{port}/effect', data=body,
                                         headers={'Content-Type': 'application/json'})
            self.assertEqual(urllib.request.urlopen(req, timeout=5).read(), b'ok')
            t, s = self.d.get('truth'), self.d.get('sink')
            self.assertEqual((t['type'], t['inst'], t['via'], t['t']), ('beat', 'e9', 'http', None))
            self.assertIsInstance(t['arrival'], float)
            self.assertEqual((s['tag'], s['arrival']), ('W', t['arrival']))
            bad = urllib.request.Request(f'http://127.0.0.1:{port}/effect', data=b'[1]')
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(bad, timeout=5)
        finally:
            p.terminate()
            p.wait(10)
            os.unlink(log)


if __name__ == '__main__':
    unittest.main()
