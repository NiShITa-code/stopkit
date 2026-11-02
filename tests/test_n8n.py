"""The n8n binding's pure parts: the workflows it plants, and its listings of n8n's stores read from canned
output (the live binding runs only on a VM with n8n installed)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from stopkit import specs
from stopkit.adapters import n8n
from stopkit.cmd import CmdResult

TAGS = {'n8n-main': 'stopkit-n8n-main-aa', 'n8n-worker': 'stopkit-n8n-worker-bb', 'n8n-loop': 'stopkit-n8n-loop-cc',
        'n8n-child': 'stopkit-n8n-child-dd', 'n8n-schedule': 'stopkit-n8n-sched-ee'}
URL = 'http://127.0.0.1:40000/effect'


def res(out='', rc=0):
    return CmdResult(('fake',), rc, out, '', 0.0, 0.0)


class Ctx:
    def __init__(self, effects_log):
        self.tags = TAGS
        self.bench = type('B', (), {'effects_log': effects_log, 'http_url': URL})()
        self.run_id = '20261029T000000Z-abcdef'


class WorkflowsTest(unittest.TestCase):
    def test_the_agent_workflow_starts_its_child_without_waiting_then_loops(self):
        wf = n8n.workflows(URL, TAGS, 'stopkit-x', child_id='7')['agent']
        names = [n['name'] for n in wf['nodes']]
        self.assertEqual(names, ['Webhook', 'StartChild', 'Loop'])
        start = wf['nodes'][1]['parameters']
        self.assertEqual(start['workflowId']['value'], '7')
        self.assertIs(start['options']['waitForSubWorkflow'], False)
        self.assertEqual(wf['connections']['Webhook']['main'][0][0]['node'], 'StartChild')
        self.assertIn(TAGS['n8n-loop'], wf['name'])

    def test_the_loop_reports_each_effect_and_its_end(self):
        js = n8n.loop_js(URL, 'T', 'loop', 600)
        self.assertIn("kind: 'beat'", js)
        self.assertIn("kind: 'end'", js)
        self.assertIn('finally', js)
        self.assertIn('String($execution.id)', js)
        self.assertIn(URL, js)

    def test_the_schedule_fires_every_minute_and_the_sentinel_once_a_year(self):
        wfs = n8n.workflows(URL, TAGS, 'stopkit-x')
        self.assertNotIn('agent', wfs)
        cron = wfs['schedule']['nodes'][0]['parameters']['rule']['interval'][0]['expression']
        self.assertEqual(cron, '0 * * * * *')
        self.assertEqual(wfs['sentinel']['name'], n8n.SENTINEL_WORKFLOW)
        self.assertIn(TAGS['n8n-schedule'], wfs['schedule']['name'])

    def test_the_scenario_and_its_stops(self):
        sc = specs.scenario('n8n-bundle-1')
        self.assertEqual(sc.effect_channels, ('http',))
        self.assertGreaterEqual(min(sc.checks_s), sc.grace_s + sc.effect_lookback_s)
        for s in n8n.STOPS:
            self.assertTrue(specs.stop(f'n8n.{s}').implemented, s)
        self.assertFalse(specs.stop('n8n.stop-by-id').in_scope('platform-child-execution'))
        self.assertTrue(specs.stop('n8n.worker-stop').in_scope('platform-child-execution'))
        self.assertFalse(specs.stop('n8n.worker-stop').in_scope('platform-component'))


class ListingsTest(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        for ev in ({'ev': 'effect', 'mono': 1.0, 'msg': {'tag': TAGS['n8n-loop'], 'inst': '12', 'n': 1}},
                   {'ev': 'effect', 'mono': 1.1, 'msg': {'tag': TAGS['n8n-child'], 'inst': '13', 'n': 1}},
                   {'ev': 'effect', 'mono': 1.2, 'msg': {'tag': 'stopkit-sentinel-http', 'n': 1}}):
            f.write(json.dumps(ev) + '\n')
        f.close()
        self.log = f.name
        self.ctx = Ctx(self.log)
        self.handle = type('H', (), {'client': {'sentinel': 's3cr'}, 'details': {'database': 'n8n_x'}})()

    def tearDown(self):
        os.unlink(self.log)

    def test_executions_are_named_by_the_work_whose_effects_carry_their_id(self):
        a = n8n.N8nAdapter()
        self.assertEqual(a._executions_by_tag(self.ctx), {'12': TAGS['n8n-loop'], '13': TAGS['n8n-child']})

    def test_the_queue_listing_and_its_proof(self):
        out = 's3cr\n5\n{"executionId":"12","loadStaticData":false}\n41000\n6\n{"executionId":"99"}\n-2\n'
        with mock.patch.object(n8n, '_redis', return_value=res(out)):
            l = n8n.N8nAdapter()._queue_listing(self.ctx, self.handle)
        self.assertTrue(l.ok)
        keys = [i.key for i in l.items]
        self.assertEqual(keys, ['stopkit-sentinel-n8n-queue', 'job:5', 'job:6'])
        self.assertTrue(l.find(TAGS['n8n-loop'])[0].fields['in_force'])           # its worker holds the lock
        self.assertFalse(next(i for i in l.items if i.key == 'job:6').fields['in_force'])   # no lock: no worker holds it
        with mock.patch.object(n8n, '_redis', return_value=res('other\n')):
            l = n8n.N8nAdapter()._queue_listing(self.ctx, self.handle)
        self.assertEqual([i.key for i in l.items], [])                  # an empty queue, not proven

    def test_the_trigger_listing_and_its_proof(self):
        out = f"1\t{TAGS['n8n-child']} child\tt\n2\t{TAGS['n8n-schedule']} schedule\tf\n3\t{n8n.SENTINEL_WORKFLOW}\tt\n"
        with mock.patch.object(n8n, '_psql', return_value=res(out)):
            l = n8n.N8nAdapter()._trigger_listing(self.ctx, self.handle)
        self.assertEqual([i.key for i in l.items], ['workflow:1', 'workflow:2', 'stopkit-sentinel-n8n-trigger'])
        self.assertFalse(l.find(TAGS['n8n-schedule'])[0].fields['in_force'])
        with mock.patch.object(n8n, '_psql', return_value=res('', rc=2)):
            self.assertFalse(n8n.N8nAdapter()._trigger_listing(self.ctx, self.handle).ok)


if __name__ == '__main__':
    unittest.main()
