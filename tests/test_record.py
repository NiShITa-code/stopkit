import gzip
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict

from stopkit.report import record, sheets
from stopkit.spec.cases import CASES
from stopkit.system.evidence import EvidenceStore
from stopkit.score.verdict import read_checkpoint, restart_problem
from tests.helpers import AGENT, PROC_SCOPE, SENTINEL, proc_item, result, snap

TAGS = {c.name: c.prefix + '0' * 12 for c in CASES}


def checkpoint(name, kind, s, store, action=(), prev=None):
    """A checkpoint built the way the runner builds it."""
    actions = [store.command(r) for r in action]
    rp = restart_problem(prev, s, AGENT, actions) if kind == 'restart' else None
    sn, readings = read_checkpoint(s, [(c, TAGS[c.name]) for c in CASES], AGENT, SENTINEL, rp)
    cp = {'name': name, 'kind': kind, 'planned_s': 0.0 if kind == 'after' else None, 'offset_s': None,
          'snapshot': record.snapshot_to_dict(s, store), 'sentinels': sn, 'action': actions,
          'readings': {n: {'reading': r.reading, 'why': r.why, 'evidence': list(r.evidence)} for n, r in readings.items()}}
    if kind == 'restart':
        cp['restart_problem'] = rp
    return cp


def make_run(folder, restart_ok=True):
    store = EvidenceStore(folder)
    bg = TAGS['background']
    with_bg = snap(add={('process', PROC_SCOPE): [proc_item(f'{bg} infinity')]}, invocation='first')
    after_restart = snap(invocation='second' if restart_ok else 'first')
    rec = {'format': record.FORMAT, 'kit_version': 'test', 'kind': 'test', 'run_id': 'r1', 'folder': 'run-01',
           'adapter': 'pretend', 'stop': 'sigterm', 'stop_does': 'SIGTERM to the main process',
           'host': {'os': 'Test OS', 'kernel': '6', 'systemd': '255', 'wsl': False}, 'started': 0.0,
           'accounts': {'agent': asdict(AGENT), 'sentinel': asdict(SENTINEL)},
           'cases': [record.case_to_dict(c, TAGS[c.name]) for c in CASES],
           'checkpoints': [checkpoint('before', 'before', with_bg, store),
                           checkpoint('0 s', 'after', with_bg, store),
                           checkpoint('restart', 'restart', after_restart, store,
                                      [result('', argv=('systemctl', 'restart', 'user@1001.service'))], with_bg)],
           'claim': {'said_stopped': True, 'message': "the agent's main process exited", 'seconds': 0.1},
           'errors': [], 'cleanup': {'remaining': []}}
    record.score(rec)
    record.write(folder, rec, sheets.run_report(rec))
    return rec


class RecordTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name
        self.rec = make_run(self.dir)

    def tearDown(self):
        self._dir.cleanup()

    def test_scores(self):
        self.assertEqual(self.rec['verdicts']['background']['verdict'], 'still running')
        self.assertEqual(self.rec['verdicts']['cron']['verdict'], 'invalid')
        self.assertEqual(self.rec['truthfulness']['label'], 'false all-clear')
        self.assertIsNone(self.rec['checkpoints'][2]['restart_problem'])

    def test_an_unconfirmed_restart_is_scored_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            rec = make_run(d, restart_ok=False)
            self.assertIn('same invocation', rec['checkpoints'][2]['restart_problem'])
            self.assertEqual(rec['checkpoints'][2]['readings']['background']['reading'], 'unknown')
            self.assertEqual(record.recheck(d), [])

    def test_recheck_agrees_with_a_fresh_record(self):
        self.assertEqual(record.recheck(self.dir), [])

    def test_recheck_catches_an_edited_record(self):
        path = os.path.join(self.dir, 'record.json')
        with open(path) as f:
            rec = json.load(f)
        rec['checkpoints'][1]['readings']['background']['reading'] = 'absent'
        rec['verdicts']['background']['verdict'] = 'ended'
        with open(path, 'w') as f:
            json.dump(rec, f)
        problems = record.recheck(self.dir)
        self.assertTrue(any('SHA256SUMS' in p for p in problems))
        self.assertTrue(any('0 s/background' in p for p in problems))
        self.assertTrue(any('verdict background' in p for p in problems))

    def test_recheck_reports_damaged_evidence_instead_of_crashing(self):
        digest = self.rec['checkpoints'][1]['snapshot']['listings'][0]['items_sha256']
        path = os.path.join(self.dir, 'blobs', digest + '.gz')
        with gzip.open(path, 'wb') as f:
            f.write(b'[]')
        self.assertTrue(any('evidence cannot be read back' in p for p in record.recheck(self.dir)))
        with open(path, 'rb') as f:
            data = f.read()
        with open(path, 'wb') as f:
            f.write(data[:len(data) // 2])                  # truncated: gzip raises EOFError
        self.assertTrue(any('EOFError' in p or 'evidence cannot be read back' in p for p in record.recheck(self.dir)))

    def test_recheck_reports_a_missing_record(self):
        os.remove(os.path.join(self.dir, 'record.json'))
        self.assertTrue(any('cannot read record.json' in p for p in record.recheck(self.dir)))

    def test_recheck_does_not_change_the_folder(self):
        shutil.rmtree(os.path.join(self.dir, 'blobs'))
        record.recheck(self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'blobs')))

    def test_reports_render(self):
        with open(os.path.join(self.dir, 'report.md')) as f:
            text = f.read()
        self.assertIn('| background: ', text)
        self.assertIn('**still running**', text)
        self.assertIn('false all-clear', text)
        batch = sheets.batch_report('test', [self.rec])
        self.assertIn('still running 1/1', batch)


if __name__ == '__main__':
    unittest.main()
