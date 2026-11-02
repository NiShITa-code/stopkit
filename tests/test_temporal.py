"""The Temporal binding's pure parts: the workflows it plants, its command lines, its scenario and stops, and its
readings of the server from canned CLI output shaped as Temporal CLI 1.9.1 prints it (the live binding runs only on
a VM with Temporal installed)."""
import calendar
import json
import os
import sys
import unittest

from stopkit import spec
from stopkit.bindings import temporal as tp
from stopkit.system.cmd import CmdResult

TAGS = {'temporal-server': 'stopkit-tp-server-aa', 'temporal-worker': 'stopkit-tp-worker-bb',
        'temporal-hb': 'stopkit-tp-hb-cc', 'temporal-nh': 'stopkit-tp-nh-dd', 'temporal-timer': 'stopkit-tp-timer-ee'}
HB, NH, TIMER = TAGS['temporal-hb'], TAGS['temporal-nh'], TAGS['temporal-timer']
OWN = {HB: 'hb', NH: 'nh', TIMER: 'timer'}
SENTINEL = tp.SENTINEL_WORKFLOW
NOW = calendar.timegm((2026, 10, 29, 12, 0, 0))            # 2026-10-29T12:00:00Z


def res(out='', rc=0, err=''):
    return CmdResult(('fake',), rc, out, err, 0.0, 0.0)


def activity(state='STARTED', started='2026-10-29T11:58:00.000000001Z', heartbeat=None, heartbeat_timeout='0s',
             name='work_quiet'):
    a = {'activityId': '1', 'activityType': {'name': name}, 'state': 'PENDING_ACTIVITY_STATE_' + state,
         'lastStartedTime': started, 'attempt': 1, 'lastWorkerIdentity': 'stopkit-temporal-worker-g1',
         'activityOptions': {'taskQueue': {'name': tp.WORK_QUEUE}, 'startToCloseTimeout': '1200s',
                             'heartbeatTimeout': heartbeat_timeout,
                             'retryPolicy': {'initialInterval': '1s', 'backoffCoefficient': 2}}}
    if heartbeat:
        a['lastHeartbeatTime'] = heartbeat
    return a


def describe(wid, status='RUNNING', activities=(), cancel_requested=False, wtype='ActivityWork'):
    info = {'execution': {'workflowId': wid, 'runId': 'run-' + wid}, 'type': {'name': wtype},
            'startTime': '2026-10-29T11:57:59.5Z', 'status': 'WORKFLOW_EXECUTION_STATUS_' + status,
            'historyLength': '11', 'taskQueue': tp.WORK_QUEUE}
    if status != 'RUNNING':
        info['closeTime'] = '2026-10-29T11:59:30Z'
    d = {'executionConfig': {'taskQueue': {'name': tp.WORK_QUEUE, 'kind': 'TASK_QUEUE_KIND_NORMAL'},
                             'defaultWorkflowTaskTimeout': '10s'},
         'workflowExecutionInfo': info}
    if activities:
        d['pendingActivities'] = list(activities)
    if cancel_requested:
        d['workflowExtendedInfo'] = {'cancelRequested': True}
    return json.dumps(d, indent=1)


def listed(*rows):
    return json.dumps([{'execution': {'workflowId': w, 'runId': 'run-' + w}, 'type': {'name': 'ActivityWork'},
                        'status': 'WORKFLOW_EXECUTION_STATUS_' + s} for w, s in rows], indent=1)


class FakeCli:
    """The CLI, answering from canned output; it records every call."""

    def __init__(self, listing, described, refuse=()):
        self.listing, self.described, self.refuse, self.calls = listing, described, set(refuse), []

    def __call__(self, *args, timeout=15.0):
        self.calls.append(args)
        if args[:2] == ('workflow', 'list'):
            return res(self.listing)
        if args[:2] == ('workflow', 'describe'):
            wid = args[3]
            if wid not in self.described:
                return res(rc=1, err=f'Error: workflow not found for ID: {wid}')
            return res(self.described[wid])
        if args[:2] in (('workflow', 'cancel'), ('workflow', 'terminate')):
            return res(rc=1, err='Error: refused') if args[3] in self.refuse else res('ok')
        raise AssertionError(f'unexpected call {args}')


def held_server(**changes):
    """A server holding the planted work as it is just before a stop."""
    d = {SENTINEL: describe(SENTINEL, wtype='SentinelWork'),
         HB: describe(HB, activities=[activity(heartbeat='2026-10-29T11:59:58.25Z', heartbeat_timeout='5s',
                                               name='work_heartbeat')]),
         NH: describe(NH, activities=[activity()]),
         TIMER: describe(TIMER, wtype='TimerWork')}
    d.update(changes)
    return d


EVERY_OPEN = listed((SENTINEL, 'RUNNING'), (HB, 'RUNNING'), (NH, 'RUNNING'), (TIMER, 'RUNNING'),
                    ('stopkit-ready-g1', 'COMPLETED'))


class SpecTest(unittest.TestCase):
    def test_the_scenario_and_its_stops(self):
        sc = spec.scenario(tp.TemporalAdapter.scenario)
        self.assertGreaterEqual(min(sc.checks_s), sc.grace_s + sc.effect_lookback_s)
        self.assertGreaterEqual(sc.restart_settle_s, sc.effect_lookback_s)
        timer = next(it for it in sc.items if it.id == 'timer').truth['P4']
        self.assertEqual((timer['oracle'], timer['when'], timer['period_s']), ('fire', 'period', tp.TIMER_S))
        self.assertGreaterEqual(min(sc.checks_s), timer['period_s'] + timer['margin_s'])    # an absence can be read
        for it in sc.items:
            self.assertEqual(it.raw.get('restart_read', True), it.id not in ('server', 'worker'), it.id)
        for s in tp.STOPS:
            self.assertTrue(spec.stop(f'temporal.{s}').implemented, s)
        for s in ('cancel', 'terminate'):
            stop = spec.stop(f'temporal.{s}')
            self.assertTrue(stop.in_scope('platform-execution') and stop.in_scope('platform-trigger'), s)
            self.assertFalse(stop.in_scope('platform-worker') or stop.in_scope('platform-component'), s)
        for s in ('worker-kill', 'worker-stop'):
            stop = spec.stop(f'temporal.{s}')
            self.assertTrue(stop.in_scope('platform-worker') and stop.in_scope('platform-execution'), s)
            self.assertFalse(stop.in_scope('platform-trigger'), s)
        self.assertEqual(spec.stop('temporal.server-stop').structures, ('platform-component',))
        self.assertTrue(spec.stop('temporal.reference').whole_agent)
        for name in (tp.SURFACE, tp.ACTIVITY_SURFACE):
            self.assertEqual(spec.surfaces()[name].observer, 'stopkit.bindings.temporal')

    def test_each_case_is_looked_for_where_its_scenario_item_is_read(self):
        sc = spec.scenario(tp.TemporalAdapter.scenario)
        cases = {c.name: c for c in tp.TemporalAdapter.cases}
        for it in sc.items:
            self.assertEqual(set(cases[it.case].surfaces), {s for ss in it.paths.values() for s in ss}, it.id)
        self.assertEqual({item for _, item in tp.WORK}, {'hb', 'nh', 'timer'})


class PlantTest(unittest.TestCase):
    def setUp(self):
        self.plans = tp.plans(TAGS, '/run/t/truth.sock', '/run/t/sink.sock')

    def test_the_sentinel_first_then_one_workflow_per_item(self):
        self.assertEqual([(p['workflow'], p['id'], p['task_queue']) for p in self.plans],
                         [('SentinelWork', SENTINEL, tp.SENTINEL_QUEUE), ('ActivityWork', HB, tp.WORK_QUEUE),
                          ('ActivityWork', NH, tp.WORK_QUEUE), ('TimerWork', TIMER, tp.WORK_QUEUE)])
        hb, nh, timer = (p['input'] for p in self.plans[1:])
        self.assertEqual((hb['heartbeat'], hb['heartbeat_timeout_s'], hb['item']), (True, tp.HEARTBEAT_TIMEOUT_S, 'hb'))
        self.assertEqual((nh['heartbeat'], nh['item']), (False, 'nh'))
        self.assertNotIn('heartbeat_timeout_s', nh)
        self.assertGreater(nh['steps'] * nh['period_s'], 120)              # longer than the last check
        self.assertGreater(nh['start_to_close_s'], nh['steps'] * nh['period_s'])
        self.assertEqual(timer['fire_argv'][-5:], ['fire', '/run/t/truth.sock', '/run/t/sink.sock', TIMER, 'timer'])
        self.assertEqual(timer['every_s'], tp.TIMER_S)

    def test_each_workflow_carries_only_its_own_tag(self):
        for p in self.plans:
            text = json.dumps(p)
            mine = [t for t in TAGS.values() if t in text]
            self.assertEqual(mine, [] if p['id'] == SENTINEL else [p['id']], p['id'])


class CommandLineTest(unittest.TestCase):
    def test_cli_calls_cannot_be_redirected(self):
        argv = tp.cli_argv('workflow', 'describe', '-w', HB)
        self.assertEqual(argv[:5], [tp.CLI, 'workflow', 'describe', '-w', HB])
        for flag, value in (('--address', '127.0.0.1:7833'), ('--namespace', 'default'), ('--command-timeout', '5s')):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn('--disable-config-file', argv)
        self.assertIn('--disable-config-env', argv)
        self.assertFalse(any(k.startswith('TEMPORAL') for k in tp.ENV))

    def test_the_server_keeps_its_state_and_listens_on_loopback_only(self):
        argv = tp.server_argv('/x/server/temporal.db')
        self.assertEqual(argv[:3], [tp.CLI, 'server', 'start-dev'])
        self.assertEqual(argv[argv.index('--db-filename') + 1], '/x/server/temporal.db')
        self.assertEqual(argv[argv.index('--ip') + 1], '127.0.0.1')
        self.assertIn('--headless', argv)

    def test_the_worker_and_its_clients_run_isolated_and_carry_no_tag(self):
        for argv in (tp.worker_argv('/x/app/w.py', 2), tp.client_argv('/x/app/w.py', 'start')):
            self.assertEqual(argv[:4], [tp.PYTHON, '-I', '-B', '/x/app/w.py'])
            self.assertFalse(any(t in ' '.join(argv) for t in TAGS.values()))
        w = tp.worker_argv('/x/app/w.py', 2)
        self.assertEqual(w[w.index('--identity') + 1], 'stopkit-temporal-worker-g2')


class ParseTest(unittest.TestCase):
    def test_enums_times_and_durations(self):
        self.assertEqual(tp.short('WORKFLOW_EXECUTION_STATUS_CANCELED'), 'CANCELED')
        self.assertEqual(tp.short('PENDING_ACTIVITY_STATE_CANCEL_REQUESTED'), 'CANCEL_REQUESTED')
        self.assertEqual(tp.utc_seconds('2026-10-29T12:00:00Z'), NOW)
        self.assertAlmostEqual(tp.utc_seconds('2026-10-29T11:59:58.250000001Z'), NOW - 1.75, places=6)
        self.assertEqual((tp.duration_seconds('5s'), tp.duration_seconds('1.500s'), tp.duration_seconds(None)),
                         (5.0, 1.5, None))
        for bad in ('2026-10-29T12:00:00+01:00', '2026-10-29 12:00:00Z'):
            with self.assertRaises(ValueError):
                tp.utc_seconds(bad)
        with self.assertRaises(ValueError):
            tp.short(None)

    def test_a_description_gives_status_and_pending_work(self):
        d = tp.parse_describe(describe(NH, 'TERMINATED', [activity(state='STARTED')], cancel_requested=True))
        self.assertEqual((d['id'], d['status'], d['cancel_requested']), (NH, 'TERMINATED', True))
        a = d['pending_activities'][0]
        self.assertEqual((a['state'], a['heartbeat_timeout_s'], a['start_to_close_s']), ('STARTED', 0.0, 1200.0))
        self.assertIsNone(tp.parse_describe(describe(TIMER))['pending_workflow_task'])

    def test_the_list_is_read_as_an_array_as_lines_or_as_one_object(self):
        rows = [{'execution': {'workflowId': w}, 'status': 'WORKFLOW_EXECUTION_STATUS_RUNNING'} for w in (HB, NH)]
        for text in (json.dumps(rows, indent=1), '\n'.join(json.dumps(r) for r in rows)):
            self.assertEqual([w['id'] for w in tp.parse_list(text)], [HB, NH])
        self.assertEqual([w['id'] for w in tp.parse_list(json.dumps(rows[0], indent=2))], [HB])
        self.assertEqual(tp.parse_list(''), [])

    def test_output_it_does_not_understand_is_refused(self):
        for text in ('Error: connection refused', '[{"status": "WORKFLOW_EXECUTION_STATUS_RUNNING"}]'):
            with self.assertRaises(ValueError):
                tp.parse_list(text)
        for text in ('{}', '[]', describe(HB) + describe(NH)):
            with self.assertRaises(ValueError):
                tp.parse_describe(text)


class HeldUntilTest(unittest.TestCase):
    def held(self, a):
        return tp.held_until(tp.parse_describe(describe(HB, activities=[a]))['pending_activities'][0])

    def test_the_servers_own_deadline_for_an_attempt(self):
        self.assertAlmostEqual(self.held(activity(heartbeat='2026-10-29T11:59:58.25Z', heartbeat_timeout='5s')),
                               NOW - 1.75 + 5, places=6)
        self.assertAlmostEqual(self.held(activity(heartbeat_timeout='5s')), NOW - 120 + 5, places=3)  # no heartbeat yet
        self.assertAlmostEqual(self.held(activity()), NOW - 120 + 1200, places=3)    # start-to-close, no heartbeats
        self.assertEqual(self.held(activity(state='SCHEDULED')), 0.0)                # waiting for a worker or a retry
        self.assertIsNone(self.held(activity(started=None)))


class ReadServerTest(unittest.TestCase):
    def read(self, listing=EVERY_OPEN, **changes):
        cli = FakeCli(listing, held_server(**changes))
        out = tp.read_server(OWN, cli, clock=lambda: NOW)
        return cli, out

    def test_held_work_is_in_force_and_the_reading_is_proven(self):
        cli, (acts, wfs, status) = self.read()
        self.assertEqual(cli.calls[0][:2], ('workflow', 'list'))
        self.assertEqual(cli.calls[1][3], SENTINEL)                                   # described first
        for l in (acts, wfs):
            self.assertTrue(l.ok)
            self.assertTrue(any(i.key.startswith('stopkit-sentinel-') and i.fields['proof'] for i in l.items))
        self.assertTrue(acts.find(HB)[0].fields['in_force'])
        self.assertTrue(acts.find(NH)[0].fields['in_force'])
        self.assertEqual(acts.find(TIMER), [])
        self.assertTrue(all(i.fields['in_force'] for t in (HB, NH, TIMER) for i in wfs.find(t)))
        self.assertEqual({i.key for i in status.items},
                         {f'workflow:{w}' for w in (SENTINEL, HB, NH, TIMER, 'stopkit-ready-g1')})
        self.assertNotIn('in_force', status.items[0].fields)

    def test_closed_work_and_attempts_no_worker_holds_are_out_of_force(self):
        stale = activity(heartbeat='2026-10-29T11:59:50Z', heartbeat_timeout='5s', name='work_heartbeat')
        _, (acts, wfs, _) = self.read(**{HB: describe(HB, activities=[stale]),
                                         NH: describe(NH, 'TERMINATED', [activity()]),
                                         TIMER: describe(TIMER, 'CANCELED', wtype='TimerWork', cancel_requested=True)})
        self.assertFalse(acts.find(HB)[0].fields['in_force'])                         # its worker is gone
        self.assertTrue(wfs.find(HB)[0].fields['in_force'])                           # the server still holds it
        self.assertFalse(acts.find(NH)[0].fields['in_force'])                         # listed by a closed workflow
        self.assertFalse(wfs.find(NH)[0].fields['in_force'])
        self.assertFalse(wfs.find(TIMER)[0].fields['in_force'])
        _, (acts, _, _) = self.read(**{HB: describe(HB, activities=[activity(state='SCHEDULED')])})
        self.assertFalse(acts.find(HB)[0].fields['in_force'])                         # waiting for its retry

    def test_no_proof_without_the_sentinel_open_in_the_same_reading(self):
        lagging = listed((HB, 'RUNNING'), (NH, 'RUNNING'), (TIMER, 'RUNNING'))
        for listing, changes in ((lagging, {}), (EVERY_OPEN, {SENTINEL: describe(SENTINEL, 'TERMINATED')})):
            _, (acts, wfs, _) = self.read(listing, **changes)
            for l in (acts, wfs):
                self.assertTrue(l.ok)
                self.assertFalse(any(i.fields.get('proof') for i in l.items))
            self.assertTrue(acts.find(HB)[0].fields['in_force'])                      # what is there still counts

    def test_a_command_that_fails_fails_every_listing(self):
        server = held_server()
        del server[NH]
        for cli in (FakeCli(EVERY_OPEN, server), FakeCli('Error: context deadline exceeded', held_server())):
            for l in tp.read_server(OWN, cli, clock=lambda: NOW):
                self.assertFalse(l.ok)
                self.assertEqual(l.items, ())


class ApiStopTest(unittest.TestCase):
    def test_a_cancel_reports_temporals_status_for_every_workflow(self):
        closed = {w: describe(w, 'CANCELED') for w in (HB, NH, TIMER)}
        cli = FakeCli(EVERY_OPEN, closed)
        claim = tp.api_stop(TAGS, 'cancel', 0.0, cli)
        self.assertIs(claim.said_stopped, True)
        self.assertEqual(claim.report, 'explicit')
        self.assertEqual(claim.covers, ('temporal-hb', 'temporal-nh', 'temporal-timer'))
        self.assertEqual([c[:2] for c in cli.calls], [('workflow', 'cancel')] * 3 + [('workflow', 'describe')] * 3)
        self.assertTrue(all('--identity' in c for c in cli.calls[:3]))
        self.assertIn('CANCELED', claim.message)

    def test_a_refused_request_or_an_open_workflow_is_not_stopped(self):
        closed = {w: describe(w, 'TERMINATED') for w in (HB, NH, TIMER)}
        refused = tp.api_stop(TAGS, 'terminate', 0.0, FakeCli(EVERY_OPEN, closed, refuse=(NH,)))
        self.assertIs(refused.said_stopped, False)
        closed[TIMER] = describe(TIMER, wtype='TimerWork')
        claim = tp.api_stop(TAGS, 'terminate', 0.0, FakeCli(EVERY_OPEN, closed))
        self.assertIs(claim.said_stopped, False)
        self.assertEqual(claim.details['status'], {'hb': 'TERMINATED', 'nh': 'TERMINATED', 'timer': 'RUNNING'})
        del closed[TIMER]
        self.assertIsNone(tp.api_stop(TAGS, 'terminate', 0.0, FakeCli(EVERY_OPEN, closed)).said_stopped)


class WorkerModuleTest(unittest.TestCase):
    def test_it_imports_without_temporalio_and_defines_what_the_binding_starts(self):
        folder = os.path.dirname(tp.APP)
        sys.path.insert(0, folder)
        try:
            import stopkit_temporal_work as work
        finally:
            sys.path.remove(folder)
        names = {c.__name__ for c in work.WORKFLOWS}
        self.assertEqual(names, {'ActivityWork', 'TimerWork', 'ReadyWork'})
        started = {p['workflow'] for p in tp.plans(TAGS, 't', 's')}
        self.assertEqual(started - names, {'SentinelWork'})                         # never run by any worker
        self.assertFalse(hasattr(work, 'SentinelWork'))
        self.assertEqual({f.__name__ for f in work.ACTIVITIES}, {'work_heartbeat', 'work_quiet', 'fire', 'ready'})
        args = work.parse_args(['ready', '--address', tp.ADDRESS, '--identity', tp.OPERATOR, '--task-queue', 'q',
                                '--workflow-id', 'w'])
        self.assertEqual((args.command, args.timeout), ('ready', 60.0))


if __name__ == '__main__':
    unittest.main()
