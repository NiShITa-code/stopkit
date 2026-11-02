import re
import unittest

from stopkit.observe.sockets import list_listeners, parse_ss, split_address
from tests.helpers import FakeRunner, result

SS = '\n'.join([
    'tcp   LISTEN 0      128          127.0.0.1:5000       0.0.0.0:*    users:(("python3",pid=4438,fd=6),("python3",pid=4432,fd=5))',
    'tcp   LISTEN 0      4096              [::]:8000          [::]:*    users:(("node",pid=77,fd=20))',
    'tcp   LISTEN 0      4096     127.0.0.53%lo:53         0.0.0.0:*    users:(("systemd-resolve",pid=385,fd=15))',
    'udp   UNCONN 0      0                    *:68              *:*    users:(("dhclient",pid=12,fd=6))',
    'udp   UNCONN 0      0      [fe80::1%eth0]:546          [::]:*    users:(("dhcp6",pid=13,fd=3))',
    'tcp   LISTEN 0      5                [::1]:631           [::]:*',
])

# The listener check used before (run_e2b.agent_listeners), kept here to show what it missed.
OLD = re.compile(r'(\d+\.\d+\.\d+\.\d+|\*):(\d+)\s')


class SocketsTest(unittest.TestCase):
    def test_every_address_form(self):
        self.assertEqual(split_address('[::]:22'), ('::', '22', None))
        self.assertEqual(split_address('[fe80::1%eth0]:546'), ('fe80::1', '546', 'eth0'))
        self.assertEqual(split_address('*:68'), ('*', '68', None))
        self.assertEqual(split_address('127.0.0.53%lo:53'), ('127.0.0.53', '53', 'lo'))
        with self.assertRaises(ValueError):
            split_address('garbage')

    def test_parse_all_lines_with_owners(self):
        rows, bad = parse_ss(SS)
        self.assertEqual(bad, [])
        self.assertEqual(len(rows), 6)
        self.assertEqual([o['pid'] for o in rows[0]['owners']], [4438, 4432])
        self.assertEqual(rows[1]['host'], '::')
        self.assertEqual(rows[5]['owners'], [])

    def test_regression_gap3_old_check_missed_ipv6_and_interface_suffix(self):
        missed = [l for l in SS.splitlines() if not OLD.search(l)]
        self.assertTrue(any('[::]:8000' in l for l in missed))
        self.assertTrue(any('%lo:53' in l for l in missed))
        listing = list_listeners(FakeRunner({('ss', '-H', '-tulpn'): result(SS)}))
        keys = {i.key for i in listing.items}
        self.assertIn('tcp:[::]:8000', keys)
        self.assertIn('tcp:127.0.0.53:53%lo', keys)

    def test_owner_command_line_carries_the_tag(self):
        def describe(pid):
            return ('stopkit-agent', 'python3 -m http.server stopkit-listener-ab12') if pid == 4438 else (None, '')
        listing = list_listeners(FakeRunner({('ss', '-H', '-tulpn'): result(SS)}), describe)
        [item] = listing.find('stopkit-listener-ab12')
        self.assertEqual((item.key, item.owner), ('tcp:127.0.0.1:5000', 'stopkit-agent'))

    def test_unreadable_line_fails_the_listing(self):
        listing = list_listeners(FakeRunner({('ss', '-H', '-tulpn'): result(SS + '\nthis is not ss output')}))
        self.assertFalse(listing.ok)

    def test_command_failure_fails_the_listing(self):
        listing = list_listeners(FakeRunner({('ss', '-H', '-tulpn'): result('', rc=1, err='boom')}))
        self.assertFalse(listing.ok)
        self.assertEqual(listing.items, ())


if __name__ == '__main__':
    unittest.main()
