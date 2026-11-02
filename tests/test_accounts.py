import os
import tempfile
import unittest

from stopkit.system import accounts
from stopkit.system.accounts import Account
from stopkit.system import KitError
from tests.helpers import FakeRunner


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.marker = os.path.join(self._dir.name, 'accounts.json')

    def tearDown(self):
        self._dir.cleanup()

    def test_an_account_the_kit_did_not_create_is_refused(self):
        r = FakeRunner()
        with self.assertRaises(KitError):
            accounts.ensure('root', r, self.marker)
        self.assertEqual(r.calls, [])                       # nothing was run against it
        with self.assertRaises(KitError):
            accounts.owned('root', self.marker)

    def test_a_missing_account_is_refused(self):
        with self.assertRaises(KitError):
            accounts.owned('stopkit-no-such-account-x', self.marker)

    def test_marker_round_trip(self):
        accounts.save_marker({'stopkit-agent': 1001}, self.marker)
        self.assertEqual(accounts.load_marker(self.marker), {'stopkit-agent': 1001})

    def test_an_id_that_owns_files_elsewhere_is_reported(self):
        def answer(argv):
            from tests.helpers import result
            return result('/opt/node-v24.21.0/bin/node\n' if '1001' in argv else '')
        r = _Fn(answer)
        self.assertEqual(accounts.owned_elsewhere(1001, '/home/stopkit-agent', r), ['/opt/node-v24.21.0/bin/node'])
        self.assertEqual(accounts.owned_elsewhere(59901, '/home/stopkit-agent', r), [])

    def test_ids_outside_the_safe_range_are_unsafe(self):
        a = Account('stopkit-agent', 1001, 1002, '/home/stopkit-agent')
        self.assertIn('outside', accounts.unsafe(a, _Fn(lambda argv: None)))

    def test_commands_run_as_the_account_with_its_own_environment_only(self):
        a = Account('stopkit-agent', 1001, 1002, '/home/stopkit-agent')
        argv = accounts.as_account_argv(a, ['id'])
        self.assertEqual(argv[:9], ['setpriv', '--reuid=1001', '--regid=1002', '--init-groups', '--', 'env', '-i',
                                    '--default-signal', '--chdir=/home/stopkit-agent'])
        self.assertIn('XDG_RUNTIME_DIR=/run/user/1001', argv)
        self.assertEqual(argv[-1], 'id')


class _Fn:
    """A runner that answers every command through one function."""
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, argv, **_kw):
        return self.fn(tuple(argv))


if __name__ == '__main__':
    unittest.main()
