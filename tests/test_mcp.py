import subprocess
import sys
import unittest

from stopkit.report import record
from stopkit.bindings.mcp import _Client

# A stand-in server: answers each request line with a result carrying the same id, ignores notifications.
ECHO = ('import json,sys\n'
        'for line in sys.stdin:\n'
        '    m=json.loads(line)\n'
        '    if "id" in m: print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"ok":True}}),flush=True)\n')


class ClientTest(unittest.TestCase):
    def test_replies_are_matched_by_id(self):
        p = subprocess.Popen([sys.executable, '-c', ECHO], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        try:
            c = _Client(p)
            c.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            c.send({'jsonrpc': '2.0', 'id': 7, 'method': 'ping'})
            self.assertEqual(c.wait_reply(7, 5.0)['result'], {'ok': True})
            self.assertIsNone(c.wait_reply(8, 0.2))
            self.assertEqual(len(c.log()), 1)
        finally:
            p.stdin.close()
            p.wait(timeout=5)


class CoversTest(unittest.TestCase):
    def rec(self, covers):
        cps = [{'kind': 'before', 'readings': {'agent': {'reading': 'present'}, 'mcp-inflight': {'reading': 'present'}}},
               {'kind': 'after', 'readings': {'agent': {'reading': 'present'}, 'mcp-inflight': {'reading': 'absent'}}},
               {'kind': 'restart', 'readings': {'agent': {'reading': 'present'}, 'mcp-inflight': {'reading': 'absent'}}}]
        r = {'checkpoints': cps, 'cases': [{'name': 'agent'}, {'name': 'mcp-inflight'}],
             'claim': {'said_stopped': True, 'covers': covers}}
        record.score(r)
        return r['truthfulness']['label']

    def test_a_cancel_is_judged_only_on_the_calls_work(self):
        # The server running on after a cancel is normal: the cancel claims only that the call's work stopped.
        self.assertEqual(self.rec(['mcp-inflight']), 'true')
        self.assertEqual(self.rec(None), 'false all-clear')


if __name__ == '__main__':
    unittest.main()
