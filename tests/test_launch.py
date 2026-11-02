import os
import tempfile
import unittest

from stopkit.system import procfs


STATUS = """Name:\tsleep
Umask:\t0022
State:\tS (sleeping)
Uid:\t1001\t1001\t1001\t1001
SigQ:\t0/31409
SigPnd:\t0000000000000000
ShdPnd:\t0000000000000000
SigBlk:\t0000000000010000
SigIgn:\t0000000000000003
SigCgt:\t0000000180000000
"""


class SignalMasksTest(unittest.TestCase):
    def test_masks_are_read_by_name(self):
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(f'{proc}/42')
            with open(f'{proc}/42/status', 'w') as f:
                f.write(STATUS)
            m = procfs.signal_masks(proc, 42)
        self.assertEqual(m['comm'], 'sleep')
        self.assertEqual(m['ignored'], ['SIGHUP', 'SIGINT'])      # bits 0 and 1: what nohup ... & leaves behind
        self.assertEqual(m['blocked'], ['SIGCHLD'])
        self.assertEqual(m['caught'], ['SIG32', 'SIG33'])

    def test_gone_is_none_and_garbage_is_an_error(self):
        with tempfile.TemporaryDirectory() as proc:
            self.assertIsNone(procfs.signal_masks(proc, 42))
            os.makedirs(f'{proc}/43')
            with open(f'{proc}/43/status', 'w') as f:
                f.write('Name:\tx\nSigBlk:\tzz\n')
            with self.assertRaises(procfs.ReadError):
                procfs.signal_masks(proc, 43)

    @unittest.skipUnless(os.path.exists('/proc/self/status'), 'needs Linux')
    def test_this_process(self):
        m = procfs.signal_masks('/proc', os.getpid())
        self.assertIn('python', m['comm'])
        self.assertIsInstance(m['ignored'], list)


if __name__ == '__main__':
    unittest.main()
