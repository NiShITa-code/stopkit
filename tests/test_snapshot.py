import json
import unittest

from stopkit.accounts import Account
from stopkit.observe import snapshot
from stopkit.observe.units import user_manager_argv
from tests.helpers import FakeProc, FakeRunner, result

ACC, UID = 'stopkit-agent', 1001
AGENT = Account(ACC, UID, 1002, '/home/stopkit-agent')
BASE = tuple(user_manager_argv(AGENT))
MANAGER = ('systemctl', 'show', f'user@{UID}.service', '--no-pager', '-p',
           'ActiveState,SubState,Result,MainPID,InvocationID,NRestarts')
SS = 'tcp LISTEN 0 128 127.0.0.1:5000 0.0.0.0:* users:(("python3",pid=30,fd=6))\n'


def table(crontab_answers):
    return {
        BASE + ('list-units', '--all', '-o', 'json'): result('[]'),
        BASE + ('list-unit-files', '-o', 'json'): result('[]'),
        BASE + ('list-timers', '--all', '-o', 'json'): result(json.dumps([{'unit': 'stopkit-tm-1.timer', 'activates': 'x'}])),
        ('crontab', '-l', '-u', ACC): crontab_answers,
        ('systemctl', '--no-pager', 'list-timers', '--all', '-o', 'json'): result('[]'),
        ('atq',): result(''),
        ('ss', '-H', '-tulpn'): result(SS),
        MANAGER: result('ActiveState=active\nSubState=running\nResult=success\nMainPID=300\nInvocationID=ab\nNRestarts=0\n'),
    }


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.proc.add(30, 'python3', UID, ['python3', '-m', 'http.server', 'stopkit-listener-77'])

    def tearDown(self):
        self.proc.close()

    def test_every_surface_is_listed_and_tags_are_found(self):
        r = FakeRunner(table(result('* * * * * /bin/true stopkit-cron-55\n')))
        snap = snapshot.take([AGENT], r, self.proc.root)
        surfaces = sorted((l.surface, l.scope) for l in snap.listings)
        self.assertEqual(surfaces, sorted([('process', ACC), ('manager', ACC), ('unit', ACC), ('timer', ACC),
                                           ('cron', ACC), ('timer', 'system'), ('at', 'all'), ('listener', 'all')]))
        self.assertTrue(all(l.ok for l in snap.listings), snap.problems)
        self.assertEqual(len(snap.find('stopkit-cron-55')[('cron', ACC)]), 1)
        self.assertEqual(len(snap.find('stopkit-tm-1')[('timer', ACC)]), 1)
        self.assertEqual(len(snap.find('stopkit-listener-77')[('listener', 'all')]), 1)
        self.assertEqual(len(snap.find('stopkit-listener-77')[('process', ACC)]), 1)
        self.assertEqual(snap.listing('manager', ACC).items[0].fields['ActiveState'], 'active')

    def test_manager_listing_failure_is_reported(self):
        t = table(result('* * * * * /bin/true stopkit-cron-55\n'))
        t[MANAGER] = result('', rc=1, err='Failed to connect to bus')
        snap = snapshot.take([AGENT], FakeRunner(t), self.proc.root)
        self.assertFalse(snap.listing('manager', ACC).ok)

    def test_regression_gap4_every_snapshot_lists_again_from_the_system(self):
        crontab = [result('* * * * * /bin/true stopkit-cron-55\n'), result('', rc=1, err=f'no crontab for {ACC}\n')]
        r = FakeRunner(table(crontab))
        first = snapshot.take([AGENT], r, self.proc.root)
        second = snapshot.take([AGENT], r, self.proc.root)
        self.assertEqual(r.count(('crontab', '-l', '-u', ACC)), 2)
        self.assertEqual(r.count(('ss', '-H', '-tulpn')), 2)
        self.assertEqual(len(first.find('stopkit-cron-55')[('cron', ACC)]), 1)
        self.assertEqual(second.find('stopkit-cron-55')[('cron', ACC)], [])
        self.assertTrue(second.listing('cron', ACC).ok)

    def test_a_failed_surface_is_reported_not_hidden(self):
        t = table(result('', rc=1, err='crontab: permission denied\n'))
        snap = snapshot.take([AGENT], FakeRunner(t), self.proc.root)
        self.assertFalse(snap.listing('cron', ACC).ok)
        self.assertTrue(any(p.startswith('cron/') for p in snap.problems))


if __name__ == '__main__':
    unittest.main()
