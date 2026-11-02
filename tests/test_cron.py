import unittest

from stopkit.observe.cron import list_at_jobs, list_crontab
from tests.helpers import FakeRunner, result

ACC = 'stopkit-agent'
CRONTAB = ('# a comment, which never runs\n'
           'MAILTO=""\n'
           '\n'
           '* * * * * /bin/true stopkit-cron-aa11\n'
           '@reboot /usr/bin/env sleep 1\n')
ATQ = '5\tSun Oct 25 12:00:00 2026 a stopkit-agent\n'
AT_C = '#!/bin/sh\n# atrun uid=1001 gid=1001\numask 22\ncd /home/stopkit-agent || exit 1\n/bin/true stopkit-at-bb22\n'


class CrontabTest(unittest.TestCase):
    def test_only_runnable_lines_are_listed(self):
        l = list_crontab(ACC, FakeRunner({('crontab', '-l', '-u', ACC): result(CRONTAB)}))
        self.assertTrue(l.ok)
        self.assertEqual([i.text for i in l.items], ['* * * * * /bin/true stopkit-cron-aa11', '@reboot /usr/bin/env sleep 1'])
        self.assertEqual(len(l.find('stopkit-cron-aa11')), 1)

    def test_no_crontab_is_a_successful_empty_listing(self):
        r = FakeRunner({('crontab', '-l', '-u', ACC): result('', rc=1, err=f'no crontab for {ACC}\n')})
        l = list_crontab(ACC, r)
        self.assertEqual((l.ok, l.items), (True, ()))

    def test_other_failures_prove_nothing(self):
        r = FakeRunner({('crontab', '-l', '-u', ACC): result('', rc=1, err='crontab: user does not exist\n')})
        self.assertFalse(list_crontab(ACC, r).ok)
        self.assertFalse(list_crontab(ACC, FakeRunner({})).ok)


class AtTest(unittest.TestCase):
    def test_jobs_with_their_commands(self):
        r = FakeRunner({('atq',): result(ATQ), ('at', '-c', '5'): result(AT_C)})
        l = list_at_jobs(r)
        self.assertTrue(l.ok)
        [item] = l.find('stopkit-at-bb22')
        self.assertEqual((item.key, item.owner, item.fields['queue']), ('at:5', 'stopkit-agent', 'a'))

    def test_a_tag_early_in_a_long_job_is_found(self):
        # Review 26 Oct: only the last 20 lines were kept, so a tagged command early in a long job was missed.
        long_job = AT_C.replace('/bin/true stopkit-at-bb22\n', '/bin/true stopkit-at-cc33\n' + 'echo filler\n' * 40)
        r = FakeRunner({('atq',): result(ATQ), ('at', '-c', '5'): result(long_job)})
        self.assertEqual(len(list_at_jobs(r).find('stopkit-at-cc33')), 1)

    def test_empty_queue(self):
        l = list_at_jobs(FakeRunner({('atq',): result('')}))
        self.assertEqual((l.ok, l.items), (True, ()))

    def test_job_that_ran_meanwhile_is_simply_gone(self):
        r = FakeRunner({('atq',): [result(ATQ), result('')], ('at', '-c', '5'): result('', rc=1, err='Cannot find jobid 5')})
        l = list_at_jobs(r)
        self.assertEqual((l.ok, l.items), (True, ()))

    def test_job_still_queued_but_unreadable_proves_nothing(self):
        r = FakeRunner({('atq',): result(ATQ), ('at', '-c', '5'): result('', rc=1, err='Permission denied')})
        self.assertFalse(list_at_jobs(r).ok)


if __name__ == '__main__':
    unittest.main()
