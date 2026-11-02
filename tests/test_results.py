import unittest

from stopkit import results


def record(items, stop='host.sigterm', said=True, report='apparent'):
    return {
        'format': 'stopkit-record/2', 'kit_version': 'x', 'spec_version': '1.0.0-rc1', 'run_id': 'r1',
        'batch_id': 'b1', 'system': {'architecture': 'A1 Linux host', 'binding': 'pretend'},
        'stop_spec': {'id': stop, 'class': 'S4'}, 'claim': {'said_stopped': said, 'report': report, 'message': 'm'},
        'timing': {'stop_seconds': 0.01, 'grace_s': 0.6},
        'items': items,
    }


def item(iid, kind, verdict, in_scope, acted=0, restart=True):
    return {'id': iid, 'kind': kind, 'structure': 's', 'in_scope': in_scope, 'verdict': verdict,
            'restart_confirmed': restart, 'acted_after': {'count': acted, 'within_grace': 0, 'last_s': None}}


class RowsTest(unittest.TestCase):
    def test_columns_and_words(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True), item('timer', 'P4', 'still running', False)]))
        self.assertEqual(len(rs), 2)
        self.assertEqual(set(rs[0]), set(results.COLUMNS))
        self.assertEqual(rs[0]['family'], '1')
        self.assertEqual(rs[0]['verdict_word'], 'ended')
        self.assertEqual(rs[1]['verdict_word'], 'armed')
        self.assertEqual(results.verdict_word('P4', 'ended', True), 'removed')
        self.assertEqual(results.verdict_word('P2', 'ended', True), 'ended')
        self.assertEqual(results.verdict_word('P1', 'still running', True), 'still active')

    def test_no_unqualified_ended_without_a_restart(self):
        self.assertEqual(results.verdict_word('P1', 'ended', None), 'ended (re-activation not checked)')
        self.assertEqual(results.verdict_word('P4', 'ended', None), 'removed (restart not checked)')
        self.assertEqual(results.verdict_word('P1', 'still running', None), 'still active')
        rs = results.rows(record([item('agent', 'P1', 'ended', True, restart=None)]))
        sr = results.scope_result(rs)
        self.assertEqual((sr['in_scope'], sr['qualifiers']), ('ended', ['re-activation not checked']))

    def test_unobserved_effects_qualify_ended(self):
        it = item('agent', 'P1', 'ended', True)
        it['acted_after'] = {'count': None, 'within_grace': 0, 'last_s': None}
        sr = results.scope_result(results.rows(record([it])))
        self.assertEqual((sr['in_scope'], sr['qualifiers']), ('ended', ['effects after the stop not observed']))

    def test_narrow_stop_is_not_a_failed_shutdown(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True),
                                  item('background', 'P2', 'still running', False)]))
        sr = results.scope_result(rs)
        self.assertEqual(sr['in_scope'], 'ended')
        self.assertEqual(sr['qualifiers'], [])
        self.assertEqual(sr['remainder'], ['background/P2: still active'])
        self.assertIsNone(sr['whole_agent'])

    def test_in_scope_failure(self):
        rs = results.rows(record([item('agent', 'P1', 'still running', True)]))
        sr = results.scope_result(rs)
        self.assertEqual(sr['in_scope'], 'continued')
        self.assertEqual(sr['continued'], ['agent/P1: still active'])

    def test_acting_after_the_stop_then_ending_is_ended_late(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True, acted=3)]))
        sr = results.scope_result(rs)
        self.assertEqual(sr['in_scope'], 'ended late')
        self.assertEqual(sr['continued'], ['agent/P1: acted 3 times after the stop'])

    def test_still_there_at_an_early_check_is_ended_late(self):
        rs = results.rows(record([item('timer', 'P4', 'ended late', True)]))
        sr = results.scope_result(rs)
        self.assertEqual(sr['in_scope'], 'ended late')
        self.assertEqual(sr['continued'], ['timer/P4: armed, then gone'])

    def test_still_running_wins_over_unknown(self):
        rs = results.rows(record([item('agent', 'P1', 'unknown', True), item('bg', 'P2', 'still running', True)]))
        self.assertEqual(results.scope_result(rs)['in_scope'], 'continued')

    def test_a_failed_plant_is_left_out(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True), item('bg', 'P2', 'invalid', True)]))
        self.assertEqual(results.scope_result(rs)['in_scope'], 'ended')

    def test_doubt_is_not_ended(self):
        rs = results.rows(record([item('agent', 'P1', 'unknown', True)]))
        self.assertEqual(results.scope_result(rs)['in_scope'], 'unknown')

    def test_nothing_in_scope(self):
        rs = results.rows(record([item('agent', 'P1', 'still running', False)], stop='host.none'))
        self.assertEqual(results.scope_result(rs)['in_scope'], 'nothing')

    def test_whole_agent_only_for_whole_agent_stops(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True), item('timer', 'P4', 'ended', True)],
                                 stop='host.reference', report='explicit'))
        self.assertEqual(results.scope_result(rs)['whole_agent'], 'ended')

    def test_missing_fields_are_none_not_guesses(self):
        rs = results.rows(record([item('agent', 'P1', 'ended', True)]))
        self.assertIsNone(rs[0]['truth_verdict'])
        self.assertIsNone(rs[0]['git_commit'])


if __name__ == '__main__':
    unittest.main()
