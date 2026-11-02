import unittest

from stopkit.system import procfs
from stopkit.observe.processes import list_processes
from tests.helpers import FakeProc


class ProcfsTest(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()

    def tearDown(self):
        self.proc.close()

    def test_stat_with_parenthesis_and_spaces_in_name(self):
        self.proc.add(10, 'odd (name) x', 1001, ['x'], start=4242)
        st = procfs.read_stat(self.proc.root, 10)
        self.assertEqual((st['comm'], st['start']), ('odd (name) x', 4242))

    def test_gone_process_is_none_not_an_error(self):
        self.assertIsNone(procfs.read_stat(self.proc.root, 999))

    def test_table_keeps_only_given_accounts_and_skips_kernel_threads(self):
        self.proc.add(10, 'sleep', 1001, ['sleep', '100', 'stopkit-bg-aa11'])
        self.proc.add(11, 'bash', 0, ['bash'])
        self.proc.add(12, 'kworker', 1001, [], flags=procfs.PF_KTHREAD)
        table = procfs.read_table(self.proc.root, {1001})
        self.assertEqual(sorted(table), [10])
        self.assertEqual(table[10]['args'], ['sleep', '100', 'stopkit-bg-aa11'])

    def test_unreadable_owner_of_running_process_is_a_problem_not_a_skip(self):
        self.proc.add(20, 'mystery', 1001, ['x'], status=False)
        problems = []
        table = procfs.read_table(self.proc.root, {1001}, problems)
        self.assertEqual(table, {})
        self.assertEqual(len(problems), 1)

    def test_listing_is_not_ok_when_an_entry_cannot_be_read(self):
        self.proc.add(10, 'sleep', 1001, ['sleep', '1'])
        self.proc.add(20, 'mystery', 1001, ['x'], status=False)
        l = list_processes({1001: 'stopkit-agent'}, self.proc.root, self.proc.mounts)
        self.assertFalse(l.ok)
        self.assertTrue(l.problems)

    def test_listing_finds_tag_and_records_identity(self):
        self.proc.add(10, 'sleep', 1001, ['sleep', '100', 'stopkit-bg-aa11'], start=777, cgroup='/user.slice/x.scope')
        l = list_processes({1001: 'stopkit-agent'}, self.proc.root, self.proc.mounts)
        self.assertTrue(l.ok)
        [item] = l.find('stopkit-bg-aa11')
        self.assertEqual((item.key, item.owner, item.fields['cgroup']), ('10:777', 'stopkit-agent', '/user.slice/x.scope'))

    def test_missing_proc_table_fails_the_listing(self):
        l = list_processes({1001: 'a'}, '/nonexistent/proc', '/nonexistent/mounts')
        self.assertFalse(l.ok)
        self.assertEqual(l.items, ())

    def test_redact_masks_secrets_but_keeps_kit_tags(self):
        out = procfs.redact(['tool', '--api-key', 'abc', 'TOKEN=xyz', 'sk-' + 'A' * 20, 'stopkit-bg-0f9e'])
        self.assertNotIn('abc', out)
        self.assertNotIn('xyz', out)
        self.assertNotIn('A' * 20, out)
        self.assertIn('stopkit-bg-0f9e', out)

    def test_agent_cgroup_refuses_system_wide_groups(self):
        for bad in ['/', 'system.slice', '/user.slice/user-1001.slice/user@1001.service',
                    '/user.slice/user-1001.slice/session-3.scope', '/system.slice/sshd.service']:
            self.assertIsNotNone(procfs.agent_cgroup(bad)[1], bad)
        self.assertIsNone(procfs.agent_cgroup('/system.slice/stopkit-run-1.scope')[1])


if __name__ == '__main__':
    unittest.main()
