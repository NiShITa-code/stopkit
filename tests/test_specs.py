import unittest

from stopkit import spec
from stopkit.bindings.mcp import STOPS as MCP_STOPS
from stopkit.bindings.pretend import STOPS as PRETEND_STOPS


class SpecTest(unittest.TestCase):
    def test_everything_loads(self):
        spec.load()
        self.assertTrue(spec.version())

    def test_kinds_and_families(self):
        self.assertEqual(spec.family_of('P1'), '1')
        self.assertEqual(spec.family_of('P4'), '2')
        self.assertEqual(spec.family_of('P5'), '3')
        self.assertEqual(spec.family_of('P7'), '4')
        with self.assertRaises(spec.SpecError):
            spec.family_of('P9')

    def test_scope(self):
        sigterm = spec.stop('host.sigterm')
        self.assertTrue(sigterm.in_scope('agent-main'))
        self.assertFalse(sigterm.in_scope('agent-detached'))
        self.assertFalse(sigterm.in_scope('account-trigger'))
        self.assertFalse(sigterm.whole_agent)
        self.assertTrue(spec.stop('host.reference').in_scope('account-trigger'))
        self.assertTrue(spec.stop('host.reference').whole_agent)
        self.assertFalse(spec.stop('host.none').in_scope('agent-main'))
        with self.assertRaises(spec.SpecError):
            spec.stop('host.no-such-stop')

    def test_mcp_scopes(self):
        cancel, shutdown = spec.stop('mcp.cancel'), spec.stop('mcp.shutdown')
        self.assertTrue(cancel.in_scope('server-request') and cancel.in_scope('server-child'))
        self.assertFalse(cancel.in_scope('server-main'))
        self.assertTrue(shutdown.in_scope('server-main') and shutdown.in_scope('server-request'))
        self.assertFalse(shutdown.in_scope('server-child'))
        self.assertTrue(cancel.scope_doc.startswith('https://modelcontextprotocol.io/'))

    def test_every_implemented_stop_is_registered(self):
        for prefix, names in (('host', PRETEND_STOPS), ('mcp', MCP_STOPS)):
            for name in names:
                with self.subTest(stop=f'{prefix}.{name}'):
                    self.assertTrue(spec.stop(f'{prefix}.{name}').implemented)

    def test_documented_scopes_are_linked(self):
        for s in spec.stops().values():
            if s.scope_source == 'documentation':
                self.assertTrue(s.scope_doc.startswith('https://'), s.id)

    def test_scenario(self):
        sc = spec.scenario('host-bundle-1')
        ids = [i.id for i in sc.items]
        self.assertEqual(len(ids), len(set(ids)))
        svc = next(i for i in sc.items if i.id == 'restarting-service')
        self.assertEqual(set(svc.paths), {'P2', 'P4'})
        self.assertEqual(svc.structure_of('P4'), 'account-trigger')
        self.assertEqual(svc.structure_of('P2'), 'account-service-process')
        self.assertEqual(svc.truth['P4']['when'], 'restart')
        self.assertEqual(len(sc.paths()), sum(len(i.paths) for i in sc.items))

    def test_unknown_structure_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec._check_structure('test', 'agent-mian')
        spec._check_structure('test', '*')

    def test_incomplete_truth_is_refused(self):
        for bad in ({'oracle': 'guess'}, {'oracle': 'fire'}, {'oracle': 'fire', 'when': 'sometimes'},
                    {'oracle': 'fire', 'when': 'period', 'period_s': 60},
                    {'oracle': 'fire', 'when': 'at', 'after_plant_s': 0, 'margin_s': 60}):
            with self.subTest(truth=bad), self.assertRaises(spec.SpecError):
                spec._check_truth('test', 'P4', bad)
        spec._check_truth('test', 'P4', {'oracle': 'fire', 'when': 'restart', 'margin_s': 30})

    def test_surfaces(self):
        surf = spec.surfaces()
        for name in ('process', 'unit', 'timer', 'cron', 'at', 'listener', 'worker'):
            self.assertTrue(surf[name].implemented, name)
            self.assertTrue(surf[name].blind_spots, name)
        self.assertFalse(surf['credential'].implemented)


if __name__ == '__main__':
    unittest.main()
