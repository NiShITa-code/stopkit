"""The non-interference tests' pass rules, on hand-made records."""
import unittest

from stopkit.run import noninterference as ni


def rec(stop='host.sigterm', verdicts=None, acted=None, seconds=0.01, truth=None, errors=(), folder='run'):
    verdicts = verdicts or {'agent/P1': 'ended'}
    items = [{'id': p.split('/')[0], 'kind': p.split('/')[1], 'verdict': v,
              'acted_after': {'count': (acted or {}).get(p, 0)}} for p, v in verdicts.items()]
    t = {'status': 'ok', 'items': {}}
    for p, (v, definite) in (truth or {}).items():
        i, k = p.split('/')
        t['items'].setdefault(i, {})[k] = {'verdict': v, 'definite': definite}
    return {'stop_spec': {'id': stop}, 'items': items, 'timing': {'stop_seconds': seconds}, 'truth': t,
            'errors': list(errors), 'folder': folder, 'checkpoints': [], 'cleanup': {'remaining': []}}


class N3Test(unittest.TestCase):
    def test_same_results_pass(self):
        self.assertTrue(ni.n3([(rec(acted={'agent/P1': 10}), rec(acted={'agent/P1': 11}))])['passed'])

    def test_a_different_verdict_or_count_or_duration_fails(self):
        self.assertFalse(ni.n3([(rec(), rec(verdicts={'agent/P1': 'still running'}))])['passed'])
        self.assertFalse(ni.n3([(rec(acted={'agent/P1': 100}), rec(acted={'agent/P1': 50}))])['passed'])
        self.assertFalse(ni.n3([(rec(seconds=0.01), rec(seconds=3.0))])['passed'])


class N6Test(unittest.TestCase):
    def test_ended_and_ended_late_are_one_at_two_polling_rates(self):
        a = rec(verdicts={'x/P1': 'ended'}, truth={'x/P1': ('ended', True)})
        b = rec(verdicts={'x/P1': 'ended late'}, truth={'x/P1': ('ended', True)})
        self.assertTrue(ni.n6([(a, b)])['passed'])

    def test_a_different_truth_fails(self):
        a = rec(verdicts={'x/P1': 'ended'}, truth={'x/P1': ('ended', True)})
        b = rec(verdicts={'x/P1': 'ended'}, truth={'x/P1': ('still running', True)})
        self.assertFalse(ni.n6([(a, b)])['passed'])
        c = rec(verdicts={'x/P1': 'ended'}, truth={'x/P1': ('still running', False)})
        self.assertTrue(ni.n6([(a, c)])['passed'])               # an indefinite truth is not compared


class N4Test(unittest.TestCase):
    STATUS = 'Name:\tsh\nSigBlk:\t0000000000000000\nSigIgn:\t0000000000000000\nSigCgt:\t0000000000000000\n'
    LIMITS = ('Limit                     Soft Limit           Hard Limit           Units     \n'
              'Max open files            1024                 524288               files     \n')

    def test_the_probe_is_read(self):
        got = ni._parse_probe(self.STATUS + self.LIMITS)
        self.assertEqual(got['signals'], {'blocked': [], 'ignored': []})
        self.assertEqual(got['limits'], {'Max open files': ['1024', '524288']})

    def test_a_launch_path_that_differs_from_the_reference_fails(self):
        ref = ni._parse_probe(self.STATUS + self.LIMITS)
        other = ni._parse_probe(self.STATUS.replace('SigIgn:\t0000000000000000', 'SigIgn:\t0000000000000002') + self.LIMITS)
        self.assertTrue(ni.n4([], {'reference': ref, 'scope': ref, 'unit': ref})['passed'])
        res = ni.n4([], {'reference': ref, 'scope': other, 'unit': ref})
        self.assertFalse(res['passed'])
        self.assertIn('SIGINT', res['problems'][0])


if __name__ == '__main__':
    unittest.main()
