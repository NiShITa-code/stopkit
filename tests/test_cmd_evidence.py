import os
import tempfile
import unittest
from unittest import mock

from stopkit import cmd
from stopkit.evidence import EvidenceStore


class RunTest(unittest.TestCase):
    def test_success_keeps_output_and_exit_code(self):
        r = cmd.run(['sh', '-c', 'echo out; echo err >&2'])
        self.assertTrue(r.ok)
        self.assertEqual((r.rc, r.out, r.err), (0, 'out\n', 'err\n'))

    def test_nonzero_exit_is_not_ok_and_does_not_raise(self):
        r = cmd.run(['sh', '-c', 'exit 3'])
        self.assertFalse(r.ok)
        self.assertEqual(r.rc, 3)
        self.assertIsNone(r.error)

    def test_missing_command_is_an_error_not_an_exception(self):
        r = cmd.run(['/nonexistent/stopkit-command'])
        self.assertFalse(r.ok)
        self.assertIsNone(r.rc)
        self.assertIn('not found', r.error)

    def test_timeout_is_an_error(self):
        r = cmd.run(['sleep', '5'], timeout=0.2)
        self.assertFalse(r.ok)
        self.assertIn('timed out', r.error)

    def test_cut_output_is_never_ok(self):
        with mock.patch.object(cmd, 'MAX_KEEP', 3):
            r = cmd.run(['sh', '-c', 'echo 0123456789'])
        self.assertTrue(r.truncated)
        self.assertFalse(r.ok)


class EvidenceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_round_trip_and_stores_once(self):
        a = self.store.put('hello\n' * 1000)
        b = self.store.put('hello\n' * 1000)
        self.assertEqual(a, b)
        self.assertEqual(self.store.get(a), 'hello\n' * 1000)
        self.assertEqual(len(os.listdir(self.store.blobs)), 1)

    def test_corruption_is_detected(self):
        import gzip
        d = self.store.put('original')
        with gzip.open(os.path.join(self.store.blobs, d + '.gz'), 'wb') as f:
            f.write(b'tampered')
        with self.assertRaises(ValueError):
            self.store.get(d)

    def test_command_keeps_full_output(self):
        r = cmd.run(['sh', '-c', 'echo full output; echo full error >&2; exit 2'])
        rec = self.store.command(r)
        self.assertEqual(rec['rc'], 2)
        self.assertEqual(self.store.get(rec['out_sha256']), 'full output\n')
        self.assertEqual(self.store.get(rec['err_sha256']), 'full error\n')


if __name__ == '__main__':
    unittest.main()
