"""The recorder, the effect sink and the reporter as real processes: what E0 relies on (FRAMEWORK.md 3.4)."""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import json

from stopkit import truth
from stopkit.observe import effects

PKG = os.path.dirname(os.path.abspath(truth.__file__))  # the stopkit package, in any layout
RECORDER = os.path.join(PKG, 'truth_recorder.py')
SINK = os.path.join(PKG, 'observe', 'effect_sink.py')
BEAT = os.path.join(PKG, 'adapters', 'beat.py')


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@unittest.skipUnless(hasattr(os, 'pidfd_open') and sys.platform.startswith('linux'), 'needs Linux')
class RecorderLiveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sock, self.log = f'{self.dir}/truth.sock', f'{self.dir}/truth.jsonl'
        self.sink, self.slog = f'{self.dir}/sink.sock', f'{self.dir}/effects.jsonl'
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
            p.stderr.close()

    def start(self, argv, ready=None):
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        if ready:
            self.assertEqual(p.stdout.readline().strip(), ready)
        return p

    def recorder(self, uids=None):
        return self.start([sys.executable, '-I', '-B', RECORDER, self.sock, self.log, str(uids or os.getuid())],
                          'stopkit-truth-ready')

    def events(self):
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def watching(self, tag):
        return wait_for(lambda: any(e['ev'] == 'watch' and e.get('tag') == tag for e in self.events()))

    def beat(self, tag, period='0.05'):
        return self.start([sys.executable, '-I', '-B', BEAT, 'run', self.sock, self.sink, tag, tag, period])

    def test_an_exit_is_recorded_within_milliseconds(self):
        rec = self.recorder()
        sink = self.start([sys.executable, '-I', '-B', SINK, self.sink, self.slog], 'stopkit-sink-ready')
        w = self.beat('tag-kill')
        self.assertTrue(self.watching('tag-kill'))
        time.sleep(0.3)
        t_kill = time.monotonic()
        os.kill(w.pid, signal.SIGKILL)
        w.wait()
        time.sleep(0.2)
        rec.terminate(), sink.terminate()
        rec.wait(5), sink.wait(5)
        lg = truth.load(self.log)
        exits = [e for e in lg.events if e['ev'] == 'exit' and e['tag'] == 'tag-kill']
        self.assertEqual(len(exits), 1)
        self.assertGreaterEqual(exits[0]['mono'], t_kill)
        self.assertLess(exits[0]['mono'] - t_kill, 0.05)
        self.assertEqual(lg.events[-1]['ev'], 'end')
        sent = lg.msgs('tag-kill', 'beat')
        self.assertGreater(len(sent), 3)
        ev, problems = effects.load(self.slog)
        self.assertEqual(problems, [])
        self.assertEqual(len(effects.arrivals(ev, 'tag-kill')), len(sent))     # nothing lost either way

    def test_signals_act_as_on_a_plain_program(self):
        self.recorder()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE):
            w = self.beat(f'tag-{sig.name}')
            self.assertTrue(self.watching(f'tag-{sig.name}'))
            os.kill(w.pid, sig)
            self.assertEqual(w.wait(5), -sig, sig.name)
            self.assertNotIn('Traceback', w.stderr.read())

    def test_a_stopped_process_is_not_ended(self):
        rec = self.recorder()
        w = self.beat('tag-stop')
        self.assertTrue(self.watching('tag-stop'))
        os.kill(w.pid, signal.SIGSTOP)
        time.sleep(0.5)
        self.assertFalse(any(e['ev'] == 'exit' for e in self.events()))
        os.kill(w.pid, signal.SIGKILL)
        w.wait()
        self.assertTrue(wait_for(lambda: any(e['ev'] == 'exit' for e in self.events())))
        rec.terminate()
        rec.wait(5)

    def test_reports_from_other_accounts_and_forged_hellos_are_refused(self):
        self.recorder(uids=str(os.getuid() + 12345))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps({'type': 'beat', 'tag': 'x', 't': 1.0}).encode(), self.sock)
        self.assertTrue(wait_for(lambda: any(e['ev'] == 'bad' and 'not one of the run' in e['why'] for e in self.events())))

    def test_a_hello_naming_another_process_is_refused(self):
        self.recorder()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps({'type': 'hello', 'tag': 'forged', 'pid': 1, 'start': 0, 't': 1.0}).encode(), self.sock)
        self.assertTrue(wait_for(lambda: any(e['ev'] == 'bad' and 'another process' in e['why'] for e in self.events())))
        self.assertFalse(any(e['ev'] == 'watch' for e in self.events()))


if __name__ == '__main__':
    unittest.main()
