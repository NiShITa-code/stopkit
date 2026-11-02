import unittest

from stopkit import guards


class GuardsTest(unittest.TestCase):
    def test_below_floor_is_a_problem(self):
        self.assertTrue(guards.disk_problems(('/',), floor=10 ** 18))

    def test_above_floor_is_fine(self):
        self.assertEqual(guards.disk_problems(('/',), floor=0), [])

    def test_missing_path_is_skipped_but_root_is_required(self):
        self.assertEqual(guards.disk_problems(('/nonexistent/stopkit',), floor=0), [])


if __name__ == '__main__':
    unittest.main()
