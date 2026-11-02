import os
import tempfile
import unittest
from dataclasses import asdict

from stopkit.run import watch
from stopkit.report import record
from stopkit.system.evidence import EvidenceStore
from stopkit.observe.model import Item
from stopkit.observe.sentinel import seen
from tests.helpers import AGENT, PROC_SCOPE, SENTINEL, proc_item, result, snap

UNIT = Item('unit', 'agent-job.service', AGENT.name, 'agent-job.service',
            {'active': 'active', 'file_state': 'enabled', 'fragment': '/home/stopkit-agent/.config/systemd/user/agent-job.service',
             'transient': False, 'triggered_by': []})
VENDOR = Item('unit', 'gpg-agent.socket', AGENT.name, 'gpg-agent.socket',
              {'active': 'active', 'file_state': 'enabled', 'fragment': '/usr/lib/systemd/user/gpg-agent.socket',
               'transient': False, 'triggered_by': []})
CRON = Item('cron', 'cron:1', AGENT.name, '*/5 * * * * /home/stopkit-agent/sync.sh', {})
MANAGER_PROC = proc_item('/usr/lib/systemd/systemd --user', pid=300)


def folder_with(steps):
    """A check folder whose listings are the given (kind, snapshot) steps, as `check add` would write them."""
    d = tempfile.mkdtemp()
    store = EvidenceStore(d)
    doc = {'format': watch.FORMAT, 'kit_version': 'test', 'account': asdict(AGENT), 'sentinel': asdict(SENTINEL),
           'host': {}, 'created': 0, 'checkpoints': []}
    counts = {}
    for t, (kind, s) in enumerate(steps):
        counts[kind] = counts.get(kind, 0) + 1
        action = [store.command(result('', argv=('systemctl', 'restart', 'user@1001.service')))] if kind == 'restart' else []
        doc['checkpoints'].append({'name': kind if kind != 'after' else f'after {counts[kind]}', 'kind': kind,
                                   'taken_at': float(t), 'snapshot': record.snapshot_to_dict(s, store),
                                   'sentinels': seen(s, SENTINEL), 'action': action})
    watch._save(d, doc)
    return d


class CheckTest(unittest.TestCase):
    def test_followed_by_identity_and_scored(self):
        before = snap(add={('process', PROC_SCOPE): [proc_item('python3 agent.py', pid=10), MANAGER_PROC],
                           ('unit', AGENT.name): [UNIT, VENDOR], ('cron', AGENT.name): [CRON]}, invocation='a')
        after = snap(add={('process', PROC_SCOPE): [MANAGER_PROC, proc_item('python3 new.py', pid=11)],
                          ('unit', AGENT.name): [UNIT, VENDOR]}, invocation='a')
        restart = snap(add={('unit', AGENT.name): [UNIT, VENDOR]}, invocation='b')
        d = folder_with([('before', before), ('after', after), ('restart', restart)])
        watch.report(d)
        doc, store = watch._load(d)
        got = {r['surface']: (r['text'], r['verdict']) for r in doc['results']['results']}
        self.assertEqual(got, {'process': ('python3 agent.py', 'ended'), 'unit': ('agent-job.service', 'still running'),
                               'cron': (CRON.text, 'ended')})
        self.assertEqual([n['text'] for n in doc['results']['new']], ['python3 new.py'])
        self.assertEqual(watch.recheck(d), [])
        self.assertTrue(os.path.exists(os.path.join(d, 'report.md')))

    def test_the_manager_and_system_units_are_not_the_agents_work(self):
        before = snap(add={('process', PROC_SCOPE): [MANAGER_PROC], ('unit', AGENT.name): [VENDOR]})
        d = folder_with([('before', before), ('after', snap())])
        watch.report(d)
        doc, _ = watch._load(d)
        self.assertEqual(doc['results']['results'], [])

    def test_a_baseline_removes_what_the_account_normally_has(self):
        base = snap(add={('cron', AGENT.name): [CRON]})
        before = snap(add={('cron', AGENT.name): [CRON], ('unit', AGENT.name): [UNIT]})
        d = folder_with([('baseline', base), ('before', before), ('after', before)])
        watch.report(d)
        doc, _ = watch._load(d)
        self.assertEqual([r['key'] for r in doc['results']['results']], ['agent-job.service'])

    def test_unproven_absence_is_unknown_and_an_unconfirmed_restart_counts_for_nothing(self):
        before = snap(add={('cron', AGENT.name): [CRON]}, invocation='a')
        d = folder_with([('before', before), ('after', snap(fail={('cron', SENTINEL.name)}, invocation='a')),
                         ('restart', snap(invocation='a'))])                 # same invocation: not restarted
        watch.report(d)
        doc, _ = watch._load(d)
        [r] = doc['results']['results']
        self.assertEqual(r['verdict'], 'unknown')
        self.assertIn('same invocation', doc['results']['restart_problems']['restart'])

    def test_a_system_restart_between_listings_makes_absence_unknown(self):
        # 26 Oct: WSL powers its system off when no window is open, which ends every process by itself.
        before = snap(add={('process', PROC_SCOPE): [proc_item('python3 agent.py', pid=10), MANAGER_PROC]})
        d = folder_with([('before', before), ('after', snap(add={('process', PROC_SCOPE): [MANAGER_PROC]}))])
        doc, _ = watch._load(d)
        doc['checkpoints'][0]['system_start'] = 'boot-a/100'
        doc['checkpoints'][1]['system_start'] = 'boot-a/9000'                 # process 1 started again
        watch._save(d, doc)
        watch.report(d)
        doc, _ = watch._load(d)
        [r] = doc['results']['results']
        self.assertEqual(r['verdict'], 'unknown')
        self.assertIn('restarted', r['readings']['after 1']['why'])

    def test_recheck_catches_an_edited_result(self):
        d = folder_with([('before', snap(add={('cron', AGENT.name): [CRON]})), ('after', snap())])
        watch.report(d)
        doc, _ = watch._load(d)
        doc['results']['results'][0]['verdict'] = 'still running'
        watch._save(d, doc)
        self.assertTrue(watch.recheck(d))


if __name__ == '__main__':
    unittest.main()
