"""Every lister against the real system. Runs only as root with STOPKIT_LIVE=1, for example:

    sudo STOPKIT_LIVE=1 python3 -B -m unittest tests.test_live -v
"""
import json
import os
import pwd
import unittest

from stopkit import accounts, guards
from stopkit.cmd import run
from stopkit.observe import snapshot
from stopkit.observe.cron import list_at_jobs, list_crontab
from stopkit.observe.processes import list_processes
from stopkit.observe.sockets import list_listeners
from stopkit.observe.timers import list_timers
from stopkit.observe.units import list_user_units

LIVE = os.environ.get('STOPKIT_LIVE') == '1' and hasattr(os, 'geteuid') and os.geteuid() == 0


@unittest.skipUnless(LIVE, 'set STOPKIT_LIVE=1 and run as root')
class LiveTest(unittest.TestCase):
    def test_disk_is_above_the_floor(self):
        self.assertEqual(guards.disk_problems(), [])

    def test_root_processes(self):
        l = list_processes({0: 'root'})
        self.assertTrue(l.ok, l.problems)
        self.assertTrue(any(i.fields['pid'] == 1 for i in l.items))

    def test_system_timers(self):
        l = list_timers(None)
        self.assertTrue(l.ok, l.problems)

    def test_listeners_parse_completely(self):
        l = list_listeners(describe_pid=snapshot.make_describe_pid())
        self.assertTrue(l.ok, l.problems)

    def test_at_queue(self):
        l = list_at_jobs()
        self.assertTrue(l.ok, l.problems)

    def test_root_crontab(self):
        l = list_crontab('root')
        self.assertTrue(l.ok, l.problems)

    def test_system_snapshot(self):
        snap = snapshot.take([])
        self.assertTrue(all(l.ok for l in snap.listings), snap.problems)

    def test_running_user_manager_units_and_timers(self):
        r = run(['systemctl', 'list-units', 'user@*.service', '--state=running', '-o', 'json', '--no-pager'])
        running = json.loads(r.out) if r.ok else []
        if not running:
            self.skipTest('no user service manager is running on this machine')
        uid = int(running[0]['unit'][len('user@'):-len('.service')])
        account = accounts.lookup(pwd.getpwuid(uid).pw_name)
        units = list_user_units(account)
        self.assertTrue(units.ok, units.problems)
        self.assertTrue(units.items)
        timers = list_timers(account)
        self.assertTrue(timers.ok, timers.problems)
        snap = snapshot.take([account])
        self.assertTrue(all(l.ok for l in snap.listings), snap.problems)


if __name__ == '__main__':
    unittest.main()
