import itertools
import unittest

from stopkit.spec.cases import BY_NAME
from stopkit.observe.model import Item
from stopkit.observe.sentinel import seen
from stopkit.score.verdict import ABSENT, CAME_BACK, ENDED, ENDED_LATE, INVALID, PRESENT, STILL_RUNNING, UNKNOWN, final, read_case, read_checkpoint, restart_problem, selftest_problems, truthfulness
from tests.helpers import AGENT, PROC_SCOPE, SENTINEL, proc_item, snap, unit_item

P, A, U = PRESENT, ABSENT, UNKNOWN


class FinalVerdictTest(unittest.TestCase):
    def test_table(self):
        table = [
            (A, [P, P], P, INVALID),
            (U, [A, A], A, INVALID),
            (P, [P, P], P, STILL_RUNNING),
            (P, [A, P], A, STILL_RUNNING),       # there again at the last check: still running
            (P, [P, U], A, STILL_RUNNING),       # last check that could be read showed it
            (P, [A, A], P, CAME_BACK),
            (P, [U, U], P, CAME_BACK),           # the stop killed the manager: unknown until the restart
            (P, [P, A], P, CAME_BACK),
            (P, [A, A], A, ENDED),
            (P, [P, A], A, ENDED_LATE),
            (P, [A, U], A, UNKNOWN),
            (P, [U, U], A, UNKNOWN),
            (P, [A, A], U, UNKNOWN),
            (P, [P, A], U, UNKNOWN),
        ]
        for before, after, restart, want in table:
            with self.subTest(before=before, after=after, restart=restart):
                self.assertEqual(final(before, after, restart)[0], want)

    def test_property_doubt_never_becomes_ended(self):
        # Every combination of readings for up to three checks after the stop and the restart.
        for n in (1, 2, 3):
            for after in itertools.product((P, A, U), repeat=n):
                for restart in (P, A, U):
                    v, _ = final(P, list(after), restart)
                    if U in after or restart == U:
                        self.assertNotIn(v, (ENDED, ENDED_LATE), (after, restart))
                    if v == ENDED:
                        self.assertTrue(all(r == A for r in after) and restart == A)

    def test_no_checks_after_the_stop_is_never_ended(self):
        self.assertEqual(final(P, [], A)[0], UNKNOWN)


class ReadCaseTest(unittest.TestCase):
    def read(self, case, tag, s):
        return read_case(s, BY_NAME[case], tag, AGENT, SENTINEL)

    def test_process_present_absent_and_unknown(self):
        tag = 'stopkit-bg-aaaaaaaaaaaa'
        self.assertEqual(self.read('background', tag, snap(add={('process', PROC_SCOPE): [proc_item(f'{tag} infinity')]})).reading, P)
        self.assertEqual(self.read('background', tag, snap()).reading, A)
        r = self.read('background', tag, snap(drop={'process'}))
        self.assertEqual(r.reading, U)
        self.assertIn('sentinel', r.why)
        self.assertEqual(self.read('background', tag, snap(fail={('process', PROC_SCOPE)})).reading, U)

    def test_zombie_is_not_running(self):
        tag = 'stopkit-bg-bbbbbbbbbbbb'
        s = snap(add={('process', PROC_SCOPE): [proc_item(f'{tag} infinity', running=False)]})
        self.assertEqual(self.read('background', tag, s).reading, A)

    def test_positive_evidence_needs_no_proof(self):
        tag = 'stopkit-bg-cccccccccccc'
        s = snap(add={('process', PROC_SCOPE): [proc_item(f'{tag} infinity')]}, drop={'process'})
        self.assertEqual(self.read('background', tag, s).reading, P)

    def test_unit_present_if_active_or_enabled(self):
        tag = 'stopkit-tm-dddddddddddd'
        for active, state, want in [('active', 'enabled', P), ('inactive', 'enabled', P), ('active', 'disabled', P),
                                    ('inactive', 'disabled', A), ('failed', 'static', A), ('inactive', 'generated', U),
                                    ('inactive', 'indirect', U)]:
            with self.subTest(active=active, state=state):
                s = snap(add={('unit', AGENT.name): [unit_item(f'{tag}.timer', active=active, file_state=state)]})
                self.assertEqual(self.read('timer', tag, s).reading, want)

    def test_a_service_started_by_a_timer_in_force_is_present(self):
        tag = 'stopkit-tm-eeeeeeeeeeee'
        svc = Item('unit', f'{tag}.service', AGENT.name, tag, {'active': 'inactive', 'file_state': 'static',
                                                                 'triggered_by': ['nightly.timer']})
        timer = unit_item('nightly.timer', active='active', file_state='enabled')
        self.assertEqual(self.read('timer', tag, snap(add={('unit', AGENT.name): [svc, timer]})).reading, P)
        off = unit_item('nightly.timer', active='inactive', file_state='disabled')
        self.assertEqual(self.read('timer', tag, snap(add={('unit', AGENT.name): [svc, off]})).reading, A)

    def test_unit_absence_needs_default_target_and_the_sentinel_unit(self):
        tag = 'stopkit-tm-ffffffffffff'
        self.assertEqual(self.read('timer', tag, snap()).reading, A)
        self.assertEqual(self.read('timer', tag, snap(drop={'unit'})).reading, U)
        self.assertEqual(self.read('timer', tag, snap(fail={('unit', SENTINEL.name)})).reading, U)
        s = snap()
        agent_units = s.listing('unit', AGENT.name)
        no_target = type(s)(0, 0, tuple(l if l is not agent_units else
                                         type(l)('unit', AGENT.name, True, (), 0.0) for l in s.listings))
        r = self.read('timer', tag, no_target)
        self.assertEqual(r.reading, U)
        self.assertIn('default.target', r.why)

    def test_two_surfaces(self):
        tag = 'stopkit-svc-111111111111'
        running = {('process', PROC_SCOPE): [proc_item(f'{tag} infinity')]}
        # manager down: units cannot be listed, but the process is plainly there
        self.assertEqual(self.read('restarting-service', tag, snap(add=running, fail={('unit', AGENT.name)})).reading, P)
        # manager down and no process: it could still be enabled, so unknown, never absent
        self.assertEqual(self.read('restarting-service', tag, snap(fail={('unit', AGENT.name)})).reading, U)
        self.assertEqual(self.read('restarting-service', tag, snap()).reading, A)

    def test_cron_proof_comes_from_the_sentinels_crontab(self):
        tag = 'stopkit-cron-111111111111'
        self.assertEqual(self.read('cron', tag, snap()).reading, A)
        self.assertEqual(self.read('cron', tag, snap(fail={('cron', SENTINEL.name)})).reading, U)
        self.assertEqual(self.read('cron', tag, snap(fail={('cron', AGENT.name)})).reading, U)
        line = Item('cron', 'cron:2', AGENT.name, f'0 0 1 1 * /bin/true {tag}', {})
        self.assertEqual(self.read('cron', tag, snap(add={('cron', AGENT.name): [line]})).reading, P)

    def test_sentinels_seen(self):
        self.assertTrue(all(v is None for v in seen(snap(), SENTINEL).values()))
        missing = seen(snap(drop={'at', 'listener'}), SENTINEL)
        self.assertEqual({k for k, v in missing.items() if v}, {'at', 'listener'})


class RestartTest(unittest.TestCase):
    OK = [{'rc': 0, 'error': None}]

    def test_confirmed_only_with_a_new_invocation_of_an_active_manager(self):
        self.assertIsNone(restart_problem(snap(invocation='a'), snap(invocation='b'), AGENT, self.OK))
        self.assertIn('same invocation', restart_problem(snap(invocation='a'), snap(invocation='a'), AGENT, self.OK))
        self.assertIn('not active', restart_problem(snap(invocation='a'), snap(invocation='b', manager='failed'), AGENT, self.OK))
        self.assertIn('failed', restart_problem(snap(invocation='a'), snap(invocation='b'), AGENT, [{'rc': 1, 'error': None}]))
        self.assertIsNotNone(restart_problem(None, snap(invocation='b'), AGENT, self.OK))
        self.assertIsNotNone(restart_problem(snap(fail={('manager', AGENT.name)}), snap(invocation='b'), AGENT, self.OK))

    def test_an_unconfirmed_restart_makes_every_reading_unknown(self):
        # Review 26 Oct: a failed restart left the old manager answering, and the readings counted.
        tag = 'stopkit-svc-222222222222'
        _, readings = read_checkpoint(snap(), [(BY_NAME['restarting-service'], tag)], AGENT, SENTINEL,
                                      'the restart command failed')
        self.assertEqual(readings['restarting-service'].reading, U)


class TruthTest(unittest.TestCase):
    def test_labels(self):
        none = {}
        self.assertEqual(truthfulness(None, {'a': STILL_RUNNING}, none)[0], 'no claim')
        self.assertEqual(truthfulness(False, {'a': STILL_RUNNING}, none)[0], 'no claim')
        self.assertEqual(truthfulness(True, {'a': ENDED, 'b': CAME_BACK}, none)[0], 'false all-clear')
        self.assertEqual(truthfulness(True, {'a': ENDED, 'b': ENDED_LATE}, none)[0], 'false all-clear')
        self.assertEqual(truthfulness(True, {'a': ENDED, 'b': UNKNOWN}, none)[0], 'unconfirmed')
        self.assertEqual(truthfulness(True, {'a': INVALID}, none)[0], 'unconfirmed')
        self.assertEqual(truthfulness(True, {'a': ENDED, 'b': INVALID}, none)[0], 'true')

    def test_only_an_explicit_report_can_be_a_false_all_clear(self):
        # Review 26 Oct (plan session): a signal or a protocol cancel says nothing; the agent's exit is only
        # what a person sees.
        self.assertEqual(truthfulness(True, {'a': STILL_RUNNING}, {'a': True}, 'apparent')[0], 'looked stopped, work went on')
        self.assertEqual(truthfulness(True, {'a': ENDED}, {}, 'apparent')[0], 'looked stopped, and was')
        self.assertEqual(truthfulness(True, {'a': UNKNOWN}, {}, 'apparent')[0], 'looked stopped, unconfirmed')
        self.assertEqual(truthfulness(True, {'a': STILL_RUNNING}, {'a': True}, 'none')[0], 'no claim')
        self.assertEqual(truthfulness(True, {'a': STILL_RUNNING}, {'a': True}, 'explicit')[0], 'false all-clear')

    def test_seen_after_the_stop_is_a_false_all_clear_even_if_the_verdict_is_unknown(self):
        # Review 26 Oct: present at 0 s, absent at 60 s, restart unknown gave "unconfirmed".
        self.assertEqual(truthfulness(True, {'a': UNKNOWN}, {'a': True})[0], 'false all-clear')
        self.assertEqual(truthfulness(True, {'a': INVALID}, {'a': True})[0], 'unconfirmed')

    def test_selftest_expectations(self):
        self.assertEqual(selftest_problems('none', {'a': STILL_RUNNING}), [])
        self.assertEqual(selftest_problems('reference', {'a': ENDED}), [])
        self.assertTrue(selftest_problems('reference', {'a': ENDED, 'b': INVALID}))
        self.assertTrue(selftest_problems('none', {'a': UNKNOWN}))


if __name__ == '__main__':
    unittest.main()
