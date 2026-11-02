import os
import socket
import tempfile
import threading
import time
import unittest

from stopkit.agents import worker_standin
from stopkit.observe.worker import list_worker_jobs, request


@unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'needs Linux')
class WorkerTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, 'sock')
        self.srv = worker_standin.serve(self.path)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        request({'op': 'cancel', 'uid': os.getuid()}, self.path)
        self.srv.shutdown()
        self.srv.server_close()
        self._dir.cleanup()

    def test_a_job_runs_as_a_tagged_process_and_is_listed_with_who_handed_it_in(self):
        jid = request({'op': 'submit', 'tag': 'stopkit-dl-test1'}, self.path)['id']
        l = list_worker_jobs(self.path, {os.getuid(): 'me'})
        self.assertTrue(l.ok, l.problems)
        [item] = l.find('stopkit-dl-test1')
        self.assertEqual((item.key, item.owner, item.fields['status']), (f'job:{jid}', 'me', 'running'))
        # The tag is on the job's command line, once its bash has started; for an instant around the exec
        # into sleep the kernel shows an empty command line (seen on the VM, 26 Oct), so wait briefly.
        deadline, cmdline = time.monotonic() + 3.0, b''
        while b'stopkit-dl-test1' not in cmdline and time.monotonic() < deadline:
            with open(f"/proc/{item.fields['pid']}/cmdline", 'rb') as f:
                cmdline = f.read()
            time.sleep(0.01)
        self.assertIn(b'stopkit-dl-test1', cmdline)

    def test_cancel_by_the_account_that_handed_jobs_in(self):
        request({'op': 'submit', 'tag': 'stopkit-dl-test2'}, self.path)
        self.assertEqual(len(request({'op': 'cancel', 'uid': os.getuid()}, self.path)['cancelled']), 1)
        [item] = list_worker_jobs(self.path).find('stopkit-dl-test2')
        self.assertNotEqual(item.fields['status'], 'running')

    def test_bad_requests_are_refused(self):
        self.assertIn('error', request({'op': 'submit', 'tag': 'no spaces; allowed'}, self.path))
        self.assertIn('error', request({'op': 'nothing'}, self.path))

    def test_no_worker_is_a_failed_listing(self):
        self.assertFalse(list_worker_jobs(os.path.join(self._dir.name, 'missing')).ok)


if __name__ == '__main__':
    unittest.main()
