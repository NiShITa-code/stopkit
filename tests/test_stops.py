import json
import os
import subprocess
import tempfile
import time
import unittest

from stopkit import procfs, stops
from stopkit.accounts import Account, as_account_argv
from stopkit.observe.units import user_manager_argv
from tests.helpers import FakeProc, FakeRunner, result

ACC = Account('stopkit-test', 999999, 999999, '/home/stopkit-test')
MAINPID = ('systemctl', 'show', 'user@999999.service', '-p', 'MainPID', '--value')


def find_argv(d):
    return tuple(as_account_argv(ACC, ['find', '-H', d, '-mindepth', '1', '-maxdepth', '2', '-printf', r'%d\t%y\t%P\n']))


def missing(d):
    return result('', rc=1, err=f"find: '{d}': No such file or directory\n")


def scan_table(config_out):
    dirs = stops.own_unit_dirs(ACC)
    return {find_argv(dirs[0]): result(config_out), find_argv(dirs[1]): missing(dirs[1]),
            find_argv(dirs[2]): missing(dirs[2])}


CONFIG = ('1\tf\ta.service\n'
          '1\tf\tnotes.txt\n'
          '1\td\tdefault.target.wants\n'
          '2\tl\tdefault.target.wants/b.service\n'
          '1\td\tc.service.d\n'
          '2\tf\tc.service.d/override.conf\n')


class OwnUnitsTest(unittest.TestCase):
    def test_scan_finds_unit_files_and_enablement_links(self):
        found, problems, _ = stops.scan_own_dirs(ACC, FakeRunner(scan_table(CONFIG)))
        self.assertEqual(set(found), {'a.service', 'b.service'})
        self.assertTrue(found['b.service'].startswith('enabled in'))
        self.assertEqual(problems, [])

    def test_scan_runs_as_the_account(self):
        r = FakeRunner(scan_table(CONFIG))
        stops.scan_own_dirs(ACC, r)
        self.assertTrue(all(argv[0] == 'setpriv' and '--reuid=999999' in argv for argv in r.calls))

    def test_a_failed_scan_is_a_problem_not_an_empty_answer(self):
        t = scan_table(CONFIG)
        t[find_argv(stops.own_unit_dirs(ACC)[0])] = result('', rc=1, err="find: '/x': Permission denied\n")
        _, problems, _ = stops.scan_own_dirs(ACC, FakeRunner(t))
        self.assertEqual(len(problems), 1)

    def test_own_units_rule(self):
        base = tuple(user_manager_argv(ACC))
        names = ['default.target', 'init.scope', 'x.service', 'y.service', 'z.service', 'w.service']
        show = '\n'.join([
            'Id=default.target\nFragmentPath=/usr/lib/systemd/user/default.target\nTransient=no\nActiveState=active\nUnitFileState=static\n',
            'Id=init.scope\nFragmentPath=\nTransient=yes\nActiveState=active\nUnitFileState=\n',
            'Id=x.service\nFragmentPath=/run/user/999999/systemd/transient/x.service\nTransient=yes\nActiveState=active\nUnitFileState=transient\n',
            'Id=y.service\nFragmentPath=/home/stopkit-test/elsewhere/y.service\nTransient=no\nActiveState=inactive\nUnitFileState=disabled\n',
            'Id=z.service\nFragmentPath=/usr/lib/systemd/user/z.service\nTransient=no\nActiveState=active\nUnitFileState=enabled\n',
            'Id=w.service\nFragmentPath=/run/user/999999/systemd/generator/w.service\nTransient=no\nActiveState=inactive\nUnitFileState=generated\n',
        ])
        t = scan_table(CONFIG)
        t[base + ('list-units', '--all', '-o', 'json')] = result(json.dumps([{'unit': n} for n in names]))
        t[base + ('show', '-p', 'Id,ActiveState,UnitFileState,FragmentPath,Transient', '--', *names)] = result(show)
        own, problems, _ = stops.own_units(ACC, FakeRunner(t))
        self.assertEqual(set(own), {'x.service', 'y.service', 'a.service', 'b.service'})
        self.assertTrue(own['x.service']['transient'] and own['x.service']['present'])
        self.assertFalse(own['y.service']['present'])          # disabled and stopped: its file alone is not present
        self.assertTrue(own['b.service']['present'])           # linked into default.target.wants: it will start
        self.assertFalse(own['a.service']['present'])
        self.assertEqual(problems, [])

    def test_manager_down_is_a_problem_not_an_empty_answer(self):
        t = scan_table('')
        t[tuple(user_manager_argv(ACC)) + ('list-units', '--all', '-o', 'json')] = \
            result('', rc=1, err='Failed to connect to bus: No such file or directory')
        _, problems, _ = stops.own_units(ACC, FakeRunner(t))
        self.assertTrue(any('service manager is not running' in p for p in problems))

    def test_made_by_system(self):
        self.assertTrue(stops.made_by_system('/usr/lib/systemd/user/a.service', ACC))
        self.assertTrue(stops.made_by_system('/run/user/999999/systemd/generator.late/a.service', ACC))
        self.assertFalse(stops.made_by_system('/run/user/999999/systemd/user/a.service', ACC))
        self.assertFalse(stops.made_by_system('/home/stopkit-test/.config/systemd/user/a.service', ACC))


@unittest.skipUnless(hasattr(os, 'pidfd_open') and os.path.exists('/proc/self/stat'), 'needs Linux')
class WaitTest(unittest.TestCase):
    def test_the_wait_ends_when_the_process_ends(self):
        # 26 Oct timing review: waking every 50 ms made every short stop measure about 0.05 s.
        p = subprocess.Popen(['sleep', '0.2'])
        start = procfs.read_stat('/proc', p.pid)['start']
        t0 = time.monotonic()
        left = stops.wait_gone([(p.pid, start)], 5.0, poll=p.poll)
        took = time.monotonic() - t0
        self.assertEqual(left, [])
        self.assertLess(took, 0.2 + 0.03)

    def test_a_process_that_does_not_end_is_returned_after_the_timeout(self):
        p = subprocess.Popen(['sleep', '5'])
        try:
            start = procfs.read_stat('/proc', p.pid)['start']
            self.assertEqual(stops.wait_gone([(p.pid, start)], 0.2, poll=p.poll), [(p.pid, start)])
        finally:
            p.kill()
            p.wait()


class LeftoversTest(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        mgr = '/user.slice/user-999999.slice/user@999999.service/init.scope'
        self.proc.add(100, 'systemd', 999999, ['/usr/lib/systemd/systemd', '--user'], cgroup=mgr)
        self.proc.add(101, '(sd-pam)', 999999, ['(sd-pam)'], ppid=100, cgroup=mgr)
        self.proc.add(102, 'sleep', 999999, ['stopkit-bg-1 infinity'], cgroup='/system.slice/stopkit-run-1.scope')
        self.proc.add(103, 'sleep', 999999, ['dead'], state='Z')
        self.proc.add(104, 'bash', 0, ['bash'])                 # root's: never the account's
        base = tuple(user_manager_argv(ACC))
        self.table = {
            **scan_table(''),
            MAINPID: result('100\n'),
            base + ('list-units', '--all', '-o', 'json'): result('[]'),
            ('crontab', '-l', '-u', 'stopkit-test'): result('', rc=1, err='no crontab for stopkit-test\n'),
            ('atq',): result(''),
            ('ss', '-H', '-tulpn'): result(''),
        }

    def tearDown(self):
        self.proc.close()

    def test_only_running_processes_outside_the_manager_are_left(self):
        left = stops.leftovers(ACC, FakeRunner(self.table), self.proc.root, worker_socket='/nonexistent/sock')
        self.assertEqual(left, ['process 102: stopkit-bg-1 infinity'])

    def test_a_process_moved_into_the_managers_group_still_counts(self):
        # Review 26 Oct: the manager was told apart by its control group, which the account can join.
        self.proc.add(105, 'sleep', 999999, ['stopkit-bg-2 infinity'], ppid=1,
                      cgroup='/user.slice/user-999999.slice/user@999999.service/init.scope')
        left = stops.leftovers(ACC, FakeRunner(self.table), self.proc.root, worker_socket='/nonexistent/sock')
        self.assertIn('process 105: stopkit-bg-2 infinity', left)

    def test_an_unknown_manager_is_never_an_empty_account(self):
        t = dict(self.table)
        t[MAINPID] = result('', rc=1, err='Failed to connect to bus')
        left = stops.leftovers(ACC, FakeRunner(t), self.proc.root, worker_socket='/nonexistent/sock')
        self.assertTrue(any(x.startswith('cannot tell (processes)') for x in left))

    def test_a_failed_listing_is_never_an_empty_account(self):
        t = dict(self.table)
        t[('crontab', '-l', '-u', 'stopkit-test')] = result('', rc=1, err='crontab: permission denied')
        left = stops.leftovers(ACC, FakeRunner(t), self.proc.root, worker_socket='/nonexistent/sock')
        self.assertTrue(any(x.startswith('cannot tell (crontab)') for x in left))


if __name__ == '__main__':
    unittest.main()
