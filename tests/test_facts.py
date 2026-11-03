"""check --facts-only and summarize: a run with no rule applied, and the rules applied to it later.

The proof uses the stored self-test runs in validation/: each run made facts-only, as check --facts-only leaves
it, then summarized, gives exactly what the normal scoring gives today.
"""
import glob
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from stopkit import cli, spec
from stopkit.report import reports
from stopkit.run import protocol

ROOT = Path(__file__).resolve().parents[1]
SELFTEST = ROOT / 'validation' / 'selftest-20261027T091012Z-70406a'
# What score() adds to a record; a run made with check --facts-only has none of it.
RULE_KEYS = ('items', 'effects', 'truthfulness', 'coverage', 'truth', 'scope_result')


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def copy_batch(dst: str, facts_only: bool) -> list[str]:
    """The stored self-test batch copied to dst, each run scored by today's code or left as check --facts-only
    leaves it."""
    os.makedirs(dst)
    shutil.copy(SELFTEST / 'batch.json', dst)
    names = []
    for d in sorted(glob.glob(str(SELFTEST / 'run-*'))):
        run_dir = os.path.join(dst, os.path.basename(d))
        shutil.copytree(d, run_dir)
        rec = protocol.load(run_dir)
        scen = spec.scenario(rec['system']['scenario'])
        if facts_only:
            for k in RULE_KEYS:
                rec.pop(k, None)
            rec['rules'] = {'applied': False}
            rec['facts'] = protocol.facts(rec, scen, run_dir)
        else:
            protocol.score(rec, scen, spec.stop(rec['stop_spec']['id']), run_dir)
        protocol.write(run_dir, rec)
        names.append(os.path.basename(d))
    return names


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.normal = os.path.join(self.tmp, 'normal')
        self.facts = os.path.join(self.tmp, 'facts')
        self.names = copy_batch(self.normal, False)
        copy_batch(self.facts, True)
        reports.write([self.normal], os.path.join(self.normal, 'report'))
        reports.write([self.facts], os.path.join(self.facts, 'report'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_score_adds_exactly_the_rule_keys(self):
        rec = protocol.load(os.path.join(self.facts, self.names[0]))
        before = set(rec)
        protocol.score(rec, spec.scenario(rec['system']['scenario']), spec.stop(rec['stop_spec']['id']),
                       os.path.join(self.facts, self.names[0]))
        self.assertEqual(set(rec) - before, set(RULE_KEYS))

    def test_a_facts_only_run_has_readings_and_facts_but_no_verdicts(self):
        for name in self.names:
            run_dir = os.path.join(self.facts, name)
            rec = json.loads(read(os.path.join(run_dir, 'record.json')))
            self.assertFalse(reports.rules_applied(rec))
            self.assertFalse(set(RULE_KEYS) & set(rec))
            self.assertEqual(set(rec['facts']['items']), {p['item'] for p in rec['paths']})
            text = read(os.path.join(run_dir, 'report.md'))
            self.assertIn('facts only', text)
            self.assertIn('What the kit read', text)
            for word in ('In-scope result', 'Kit verdict', 'Report truthfulness', 'Truth verdict', 'false all-clear'):
                self.assertNotIn(word, text)
            self.assertEqual(protocol.recheck(run_dir), [])

    def test_the_facts_only_batch_report(self):
        out = os.path.join(self.facts, 'report')
        self.assertIn('# Shutdown test: facts only', read(os.path.join(out, 'REPORT.md')))
        self.assertTrue(os.path.isfile(os.path.join(out, 'REPORT.html')))
        for name in ('rows.jsonl', 'accuracy.json', 'timelines'):
            self.assertFalse(os.path.exists(os.path.join(out, name)), name)

    def test_summarize_gives_what_the_normal_scoring_gives(self):
        records, problems = protocol.summarize(self.facts, log=lambda s: None)
        self.assertEqual(problems, [])
        self.assertEqual(len(records), len(self.names))
        for name in self.names:
            a = json.loads(read(os.path.join(self.normal, name, 'record.json')))
            b = json.loads(read(os.path.join(self.facts, name, 'record.json')))
            for k in RULE_KEYS:
                self.assertEqual(json.dumps(a[k], sort_keys=True), json.dumps(b[k], sort_keys=True), f'{name}: {k}')
            self.assertEqual(read(os.path.join(self.normal, name, 'report.md')),
                             read(os.path.join(self.facts, name, 'report.md')), name)
            self.assertTrue(b['rules']['applied'] and b['rules']['later'])
            self.assertEqual(protocol.recheck(os.path.join(self.facts, name)), [])
        for f in ('rows.jsonl', 'accuracy.json'):
            self.assertEqual(read(os.path.join(self.normal, 'report', f)), read(os.path.join(self.facts, 'report', f)), f)
        batch = json.loads(read(os.path.join(self.facts, 'batch.json')))
        self.assertTrue(batch['rules_applied'])

    def test_summarize_leaves_a_changed_run_as_it_is(self):
        run_dir = os.path.join(self.facts, self.names[0])
        with open(os.path.join(run_dir, 'effects.jsonl'), 'a', encoding='utf-8') as f:
            f.write('\n')
        records, problems = protocol.summarize(self.facts, log=lambda s: None)
        self.assertEqual(len(problems), 1)
        self.assertIn('SHA256SUMS', problems[0])
        self.assertFalse(reports.rules_applied(json.loads(read(os.path.join(run_dir, 'record.json')))))

    def test_summarize_twice_changes_nothing(self):
        protocol.summarize(self.facts, log=lambda s: None)
        first = {n: read(os.path.join(self.facts, n, 'record.json')) for n in self.names}
        said = []
        protocol.summarize(self.facts, log=said.append)
        self.assertEqual(first, {n: read(os.path.join(self.facts, n, 'record.json')) for n in self.names})
        self.assertEqual(said, ['every run already has its verdicts'])


class FactsTest(unittest.TestCase):
    """facts() on a small effect log: what reached the sink after the cut-off, and whether the sink worked."""

    def run_facts(self, events):
        rec = {'timing': {'cut_off_mono': 100.0, 'restart_action_mono': 150.0, 'restart_done_mono': 151.0,
                          'truth_tail_s': 0.0, 'end_mono': 160.0},
               'bench': {'sentinel_tag': 'S'},
               'paths': [{'id': 'a/P2', 'item': 'a', 'tag': 'TA'}, {'id': 'a/P4', 'item': 'a', 'tag': 'TA'},
                         {'id': 'b/P2', 'item': 'b', 'tag': 'TB'}, {'id': 'c/P2', 'item': 'c', 'tag': 'TC'}]}
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'effects.jsonl'), 'w', encoding='utf-8') as f:
                for e in events:
                    f.write(json.dumps(e) + '\n')
            return protocol.facts(rec, spec.scenario('host-bundle-1'), tmp)

    def sentinel(self):
        return [{'ev': 'effect', 'mono': 98.0 + i / 2, 'msg': {'tag': 'S', 't': 98.0 + i / 2}} for i in range(110)]

    def test_actions_after_the_cut_off(self):
        events = self.sentinel() + [
            {'ev': 'effect', 'mono': 99.1, 'msg': {'tag': 'TA', 't': 99.0}},       # before the stop
            {'ev': 'effect', 'mono': 100.6, 'msg': {'tag': 'TA', 't': 100.5}},
            {'ev': 'effect', 'mono': 130.3, 'msg': {'tag': 'TA', 't': 130.25}},
            {'ev': 'effect', 'mono': 101.0, 'msg': {'tag': 'TB', 'arrival': 101.0}}]   # placed by its arrival
        f = self.run_facts(events)
        self.assertEqual(f['items']['a'], {'after_cut_off': 2, 'first_s': 0.5, 'last_s': 30.25})
        self.assertEqual(f['items']['b'], {'after_cut_off': 1, 'first_s': 1.0, 'last_s': 1.0})
        self.assertEqual(f['items']['c'], {'after_cut_off': 0, 'first_s': None, 'last_s': None})
        self.assertTrue(f['effects']['shown_working'])
        self.assertIsNone(f['effects']['why_not'])

    def test_a_sink_not_shown_working_is_said(self):
        f = self.run_facts([{'ev': 'effect', 'mono': 100.6, 'msg': {'tag': 'TA', 't': 100.5}}])
        self.assertFalse(f['effects']['shown_working'])
        self.assertIn("sentinel's effects", f['effects']['why_not'])
        self.assertEqual(f['items']['a']['after_cut_off'], 1)


class CommandTest(unittest.TestCase):
    def test_the_options(self):
        p = cli.parser()
        self.assertTrue(p.parse_args(['check', 'pretend', '--facts-only']).facts_only)
        self.assertFalse(p.parse_args(['check', 'pretend']).facts_only)
        a = p.parse_args(['summarize', 'results/test-x'])
        self.assertIs(a.func, cli.cmd_summarize)
        self.assertEqual(a.folders, ['results/test-x'])


if __name__ == '__main__':
    unittest.main()
