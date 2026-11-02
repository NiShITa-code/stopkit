import unittest

from stopkit.score import coverage
from stopkit import spec


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.sc = spec.scenario('host-bundle-1')
        used = sorted({s for it in self.sc.items for ss in it.paths.values() for s in ss})
        ok = {s: None for s in used}
        self.proofs = {'before': ok, '0 s': {**ok, 'unit': 'the listing failed: Failed to connect to bus'},
                       '60 s': {s: None for s in used if s != 'cron'}}

    def test_surfaces(self):
        m = coverage.manifest(self.sc, self.proofs)
        self.assertEqual(m['checkpoints'], ['before', '0 s', '60 s'])
        self.assertEqual(m['surfaces']['process']['proven'], {'before': True, '0 s': True, '60 s': True})
        self.assertEqual(m['surfaces']['unit']['proven']['0 s'], 'the listing failed: Failed to connect to bus')
        self.assertEqual(m['surfaces']['cron']['proven']['60 s'], coverage.NOT_LISTED)
        self.assertTrue(m['surfaces']['listener']['blind_spots'])

    def test_kinds(self):
        k = coverage.manifest(self.sc, self.proofs)['kinds']
        self.assertEqual(k['P1']['status'], 'tested')
        self.assertTrue(k['P1']['proven_at_every_checkpoint'])
        self.assertFalse(k['P4']['proven_at_every_checkpoint'])        # the unit listing failed at 0 s
        self.assertEqual(k['P4']['surfaces'], ['at', 'cron', 'unit'])
        self.assertEqual(k['P5']['status'], 'not tested: no implemented observer')
        self.assertEqual(k['P7']['status'], 'not tested: no implemented observer')
        self.assertIsNone(k['P5']['proven_at_every_checkpoint'])


if __name__ == '__main__':
    unittest.main()
