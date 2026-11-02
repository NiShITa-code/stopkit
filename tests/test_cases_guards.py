import os
import subprocess
import tempfile
import unittest

from stopkit import guards
from stopkit.accounts import Account
from stopkit.adapters import pretend
from stopkit.cases import CASES, new_tags, plan_text
from stopkit.config import SENTINEL_TAGS, KitError
from stopkit.observe.processes import command_text
from tests.helpers import result


class CasesTest(unittest.TestCase):
    def test_tags_are_fresh_and_never_contain_each_other(self):
        a, b = new_tags(), new_tags()
        self.assertEqual(set(a), {c.name for c in CASES})
        self.assertTrue(all(a[n] != b[n] for n in a))
        tags = list(a.values()) + list(SENTINEL_TAGS.values())
        for t in tags:
            self.assertEqual([u for u in tags if t in u], [t])

    def test_plan_lines(self):
        text = plan_text({'agent': 'stopkit-main-1', 'cron': 'stopkit-cron-2'})
        self.assertEqual(text, 'agent stopkit-main-1\ncron stopkit-cron-2\n')

    def test_the_agent_script_knows_every_case(self):
        with open(pretend.SCRIPT, 'rb') as f:
            data = f.read()
        self.assertNotIn(b'\r', data)
        for c in CASES:
            if c.name != 'agent':
                self.assertIn(f"plant_{c.name.replace('-', '_')}()".encode(), data)

    @unittest.skipUnless(os.path.exists('/bin/bash'), 'needs bash')
    def test_the_agent_script_parses(self):
        r = subprocess.run(['/bin/bash', '-n', pretend.SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class CommandTextTest(unittest.TestCase):
    def test_a_tag_is_never_masked_away(self):
        # Review 26 Oct: 'task-<tag>' matched the 'sk-' API-key pattern, so the tag vanished.
        tag = 'stopkit-bg-0123456789ab'
        for args in (['worker', '--name', f'task-{tag}'], ['app', '--app=flask-' + tag], ['x', '--token', tag],
                     ['x', 'a' * 2100, tag]):
            with self.subTest(args=args[:2]):
                self.assertIn(tag, command_text(args))

    def test_secrets_stay_masked(self):
        text = command_text(['curl', '--token', 'sk-abcdefghijklmnop'])
        self.assertNotIn('abcdefghijklmnop', text)


class PlacesTest(unittest.TestCase):
    ACC = Account('stopkit-test', 999999, 999999, '/home/stopkit-test')

    def runner(self, writable):
        def answer(argv):
            if 'sh' in argv:                                    # the `test -w` loop over the given paths
                paths = list(argv[argv.index('sh', argv.index('-c')) + 1:])
                return result(''.join(p + '\n' for p in paths if p in writable))
            if 'find' in argv:
                return result('')
            if 'test' in argv:
                return result('', rc=0 if argv[-1] in writable else 1)
            return result('', rc=None, error='unexpected')
        return _Fn(answer)

    def test_a_writable_folder_or_parent_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, 'kit')
            os.mkdir(sub)
            self.assertEqual(guards.changeable_by(self.ACC, [sub], self.runner(set())), [])
            self.assertTrue(guards.changeable_by(self.ACC, [sub], self.runner({sub})))
            self.assertTrue(guards.changeable_by(self.ACC, [sub], self.runner({d})))      # it could replace sub

    @unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0, 'setting the sticky bit needs root here')
    def test_a_sticky_parent_protects_entries_the_account_does_not_own(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o1777)
            sub = os.path.join(d, 'kit')
            os.mkdir(sub)                                      # owned by root, not by the account
            self.assertEqual(guards.changeable_by(self.ACC, [sub], self.runner({d})), [])

    def test_writable_windows_drives(self):
        with tempfile.TemporaryDirectory() as d:
            mounts = os.path.join(d, 'mounts')
            with open(mounts, 'w') as f:
                f.write('C:\\ /mnt/c 9p rw,noatime,aname=drvfs;path=C:\\;uid=0 0 0\n'
                        '/dev/sdd / ext4 rw,relatime 0 0\n')
            self.assertEqual(guards.writable_windows_drives(self.ACC, self.runner({'/mnt/c'}), mounts), ['/mnt/c'])
            self.assertEqual(guards.writable_windows_drives(self.ACC, self.runner(set()), mounts), [])


class _Fn:
    """A runner that answers every command through one function."""
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, argv, **_kw):
        return self.fn(tuple(argv))


class LockTest(unittest.TestCase):
    def test_a_second_command_is_refused_while_the_first_runs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'lock')
            with guards.single_run(path):
                with self.assertRaises(KitError):
                    with guards.single_run(path):
                        pass
            with guards.single_run(path):          # free again afterwards
                pass


if __name__ == '__main__':
    unittest.main()
