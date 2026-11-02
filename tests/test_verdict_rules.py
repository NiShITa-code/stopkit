"""The verdict code against the versioned rules in spec/verdicts.json."""
import itertools
import unittest

from stopkit import spec
from stopkit.score import verdict


class VerdictRulesTest(unittest.TestCase):
    def test_vocabulary_matches_the_code(self):
        self.assertEqual(set(spec.VERDICTS), {verdict.INVALID, verdict.STILL_RUNNING, verdict.CAME_BACK,
                                               verdict.ENDED, verdict.ENDED_LATE, verdict.UNKNOWN})
        self.assertEqual(set(spec.READINGS), {verdict.PRESENT, verdict.ABSENT, verdict.UNKNOWN})
        self.assertEqual(set(verdict.NOT_ENDED), set(spec.VERDICTS) - {'invalid', 'ended', 'unknown'})

    def test_every_example(self):
        for ex in spec.verdict_rules()['examples']:
            with self.subTest(ex=ex):
                got, why = verdict.final(ex['before'], ex['after'], ex['restart'])
                self.assertEqual(got, ex['verdict'])
                self.assertTrue(why)

    def test_every_scorable_pair_has_a_category(self):
        acc = spec.verdict_rules()['accuracy']
        scorable = ('ended', 'ended late', 'still running', 'came back')
        for kit, truth in itertools.product(scorable, scorable):
            self.assertIn(f'{kit}|{truth}', acc['verdict_pairs'])
        for kit, truth in itertools.product(spec.READINGS, ('present', 'absent')):
            self.assertIn(f'{kit}|{truth}', acc['reading_pairs'])

    def test_agreement_only_on_the_diagonal(self):
        acc = spec.verdict_rules()['accuracy']
        for key, cat in list(acc['verdict_pairs'].items()) + list(acc['reading_pairs'].items()):
            kit, truth = key.split('|')
            self.assertEqual(cat == 'agree', kit == truth, key)

    def test_every_family_with_verdicts_has_words(self):
        words = spec.verdict_rules()['words']
        for fam in spec.kinds()['families']:
            if fam != '4':
                self.assertEqual(set(words[fam]), set(spec.VERDICTS), fam)


if __name__ == '__main__':
    unittest.main()
