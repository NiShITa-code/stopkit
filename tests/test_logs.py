"""stopkit logs: a run's raw logs, from its own files, with none of the kit's rules applied; and the run
folder's checksums, which cover every file of it."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from stopkit import cli
from stopkit.report import logs
from stopkit.run import protocol

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'validation' / 'selftest-20261027T091012Z-70406a' / 'run-02-reference'
# What the kit works out from the logs. `stopkit logs` reads none of it.
WORKED_OUT = ('items', 'effects', 'truthfulness', 'coverage', 'truth', 'scope_result', 'facts', 'rules')


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


@unittest.skipUnless(RUN.is_dir(), 'needs the stored self-test runs in validation/, which the copy of the kit '
                                   'that setup/base.sh installs does not hold')
class LogsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.run = os.path.join(self.tmp, 'run')
        shutil.copytree(RUN, self.run)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_it_shows_every_tag_every_check_and_both_logs(self):
        text = logs.run_logs(self.run)
        rec = json.loads(read(os.path.join(self.run, 'record.json')))
        for p in rec['paths']:
            self.assertIn(f"`{p['tag']}`", text)
        for cp in rec['checkpoints']:
            self.assertIn(f"## Check `{cp['name']}`", text)
        self.assertIn("## The recorder's log", text)
        self.assertIn("## The receiver's log", text)
        self.assertIn('Every file listed in SHA256SUMS has the checksum listed there.', text)
        self.assertNotIn('cannot be read', text)

    def test_it_reads_nothing_the_rules_worked_out(self):
        before = logs.run_logs(self.run)
        path = os.path.join(self.run, 'record.json')
        rec = json.loads(read(path))
        for k in WORKED_OUT:
            rec.pop(k, None)
        for cp in rec['checkpoints']:
            for k in ('readings', 'sentinels', 'proofs'):
                cp.pop(k, None)
        for k in ('said_stopped', 'report', 'covers'):
            rec['claim'].pop(k, None)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        after = logs.run_logs(self.run)

        def kept(text):         # record.json is now smaller, and no longer matches its checksum
            return [line for line in text.splitlines() if 'record.json' not in line and 'SHA256SUMS' not in line]

        self.assertEqual(kept(before), kept(after))

    def test_every_file_of_the_folder_is_checksummed_and_a_changed_log_is_found(self):
        with open(os.path.join(self.run, 'agent-g1.log'), 'w') as f:
            f.write('what the agent printed\n')
        sums = protocol._sums(self.run)
        self.assertIn('  agent-g1.log\n', sums)
        self.assertNotIn('blobs', sums)                     # each blob is named by its own SHA-256
        with open(os.path.join(self.run, 'SHA256SUMS'), 'w', newline='\n') as f:
            f.write(sums)
        self.assertEqual(protocol.sums_problems(self.run), [])
        with open(os.path.join(self.run, 'agent-g1.log'), 'a') as f:
            f.write('changed afterwards\n')
        self.assertEqual(protocol.sums_problems(self.run), ['SHA256SUMS does not match the files: agent-g1.log'])
        self.assertIn('SHA256SUMS does not match the files: agent-g1.log', logs.run_logs(self.run))
        self.assertIn('SHA256SUMS does not match the files: agent-g1.log', protocol.recheck(self.run))

    def test_an_older_checksum_file_is_still_checked(self):
        # Runs recorded before 3 November 2026 list four files; those four are checked, and the rest are not.
        self.assertEqual(len(read(os.path.join(self.run, 'SHA256SUMS')).splitlines()), 4)
        self.assertEqual(protocol.sums_problems(self.run), [])
        with open(os.path.join(self.run, 'effects.jsonl'), 'a') as f:
            f.write('\n')
        self.assertEqual(protocol.sums_problems(self.run), ['SHA256SUMS does not match the files: effects.jsonl'])

    def test_the_command(self):
        out = os.path.join(self.tmp, 'logs.md')
        self.assertEqual(cli.main(['logs', self.run, '--out', out]), 0)
        self.assertTrue(read(out).startswith('# The logs of run '))


class EntriesTest(unittest.TestCase):
    def test_an_output_becomes_entries(self):
        self.assertEqual(logs.entries('[{"b": 1, "a": "x"}, 2]'), ['{"a": "x", "b": 1}', '2'])
        self.assertEqual(logs.entries('one\n\ntwo\n'), ['one', 'two'])
        self.assertEqual(logs.entries('{"k": "v"}'), ['{"k": "v"}'])

    def test_a_long_entry_still_shows_where_its_tag_is(self):
        entry = 'x' * 2000 + ' stopkit-cc-42f5b2ac443d ' + 'y' * 50
        shown = logs._short(entry, ['stopkit-cc-42f5b2ac443d'])
        self.assertIn('stopkit-cc-42f5b2ac443d', shown)
        self.assertIn('2075 characters in all', shown)


if __name__ == '__main__':
    unittest.main()
