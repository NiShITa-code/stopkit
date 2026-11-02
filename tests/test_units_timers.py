import json
import unittest

from stopkit.system.accounts import Account
from stopkit.observe.timers import list_timers
from stopkit.observe.units import SHOW_PROPS, list_user_units, unit_in_force, user_manager_argv
from tests.helpers import FakeRunner, result

ACC = Account('stopkit-agent', 1001, 1002, '/home/stopkit-agent')
BASE = tuple(user_manager_argv(ACC))
UNITS = json.dumps([
    {'unit': 'stopkit-svc-aa11.service', 'load': 'loaded', 'active': 'active', 'sub': 'running', 'description': 'svc'},
    # a temporary unit, as `systemd-run --user` makes: never enabled, but running
    {'unit': 'run-u42.service', 'load': 'loaded', 'active': 'active', 'sub': 'running', 'description': '/bin/sleep 1000 stopkit-tr-bb22'},
    {'unit': 'dbus.socket', 'load': 'loaded', 'active': 'active', 'sub': 'listening', 'description': 'D-Bus'},
])
FILES = json.dumps([
    {'unit_file': 'stopkit-svc-aa11.service', 'state': 'enabled', 'preset': 'enabled'},
    {'unit_file': 'stopkit-old-cc33.service', 'state': 'enabled', 'preset': 'enabled'},   # enabled, not loaded
])
TIMERS = json.dumps([{'next': 1, 'left': 1, 'last': 0, 'passed': 0, 'unit': 'stopkit-tm-dd44.timer',
                      'activates': 'stopkit-tm-dd44.service'}])


SHOW = ('Id=dbus.socket\nUnitFileState=static\nTriggeredBy=\n\n'
        'Id=run-u42.service\nUnitFileState=transient\nTriggeredBy=\n\n'
        'Id=stopkit-svc-aa11.service\nUnitFileState=enabled\nTriggeredBy=\n')


def show_argv(names):
    return BASE + ('show', '-p', SHOW_PROPS, '--', *sorted(names))


def units_runner(units=UNITS, files=FILES, show=SHOW):
    names = [u['unit'] for u in json.loads(units)] if units.startswith('[') else []
    return FakeRunner({BASE + ('list-units', '--all', '-o', 'json'): result(units),
                       BASE + ('list-unit-files', '-o', 'json'): result(files),
                       show_argv(names): result(show)})


class UnitsTest(unittest.TestCase):
    def test_lists_loaded_and_unit_files(self):
        l = list_user_units(ACC, units_runner())
        self.assertTrue(l.ok)
        keys = {i.key for i in l.items}
        self.assertEqual(keys, {'stopkit-svc-aa11.service', 'run-u42.service', 'dbus.socket', 'stopkit-old-cc33.service'})
        old = next(i for i in l.items if i.key == 'stopkit-old-cc33.service')
        self.assertEqual((old.fields['loaded'], old.fields['file_state']), (False, 'enabled'))

    def test_regression_gap2_temporary_unit_is_found(self):
        # The old check kept only units whose is-enabled said "enabled"; a systemd-run unit never is.
        old_kept = [u['unit'] for u in json.loads(UNITS)
                    if {f['unit_file']: f['state'] for f in json.loads(FILES)}.get(u['unit']) == 'enabled']
        self.assertNotIn('run-u42.service', old_kept)
        l = list_user_units(ACC, units_runner())
        [item] = l.find('stopkit-tr-bb22')
        self.assertEqual((item.key, item.fields['active']), ('run-u42.service', 'active'))

    def test_manager_not_running_proves_nothing(self):
        r = FakeRunner({BASE + ('list-units', '--all', '-o', 'json'): result('', rc=1, err='Failed to connect to bus: No such file or directory'),
                        BASE + ('list-unit-files', '-o', 'json'): result(FILES)})
        l = list_user_units(ACC, r)
        self.assertFalse(l.ok)
        self.assertEqual(l.items, ())
        self.assertIn('service manager', l.problems[0])

    def test_output_that_is_not_json_proves_nothing(self):
        l = list_user_units(ACC, units_runner(units='UNIT LOAD ACTIVE'))
        self.assertFalse(l.ok)

    def test_rows_without_the_expected_fields_fail_the_listing(self):
        # Review 26 Oct: rows without unit_file were skipped silently, so a changed format read as "absent".
        files = json.dumps([{'name': 'stopkit-svc-aa11.service', 'status': 'enabled'}])
        self.assertFalse(list_user_units(ACC, units_runner(files=files)).ok)

    def test_a_unit_show_does_not_describe_fails_the_listing(self):
        self.assertFalse(list_user_units(ACC, units_runner(show=SHOW.split('\n\nId=stopkit')[0])).ok)

    def test_enablement_of_an_instance_comes_from_show(self):
        # list-unit-files lists only the template, as "indirect"; show gives the instance's own state.
        units = json.dumps([{'unit': 'probe@stopkit-x1.service', 'load': 'loaded', 'active': 'inactive', 'sub': 'dead'}])
        files = json.dumps([{'unit_file': 'probe@.service', 'state': 'indirect'}])
        show = 'Id=probe@stopkit-x1.service\nUnitFileState=enabled\nTriggeredBy=\n'
        [item] = list_user_units(ACC, units_runner(units, files, show)).find('stopkit-x1')
        self.assertEqual(item.fields['file_state'], 'enabled')

    def test_in_force_rules(self):
        by_name = {
            'a.timer': {'active': 'active', 'file_state': 'enabled'},
            'stopkit-t1.service': {'active': 'inactive', 'file_state': 'static', 'triggered_by': ['a.timer']},
            'b.service': {'active': 'inactive', 'file_state': 'disabled'},
            'c.service': {'active': 'inactive', 'file_state': 'generated'},
            'd.service': {'active': 'inactive', 'file_state': 'static', 'triggered_by': ['gone.path']},
        }
        self.assertIs(unit_in_force('stopkit-t1.service', by_name), True)     # started by a timer in force
        self.assertIs(unit_in_force('b.service', by_name), False)
        self.assertIsNone(unit_in_force('c.service', by_name))                # may run again: cannot tell
        self.assertIsNone(unit_in_force('d.service', by_name))                # its trigger is not listed


class TimersTest(unittest.TestCase):
    def test_regression_gap1_user_timer_is_listed(self):
        r = FakeRunner({BASE + ('list-timers', '--all', '-o', 'json'): result(TIMERS)})
        l = list_timers(ACC, r)
        self.assertTrue(l.ok)
        [item] = l.find('stopkit-tm-dd44')
        self.assertEqual((item.key, item.fields['activates']), ('stopkit-tm-dd44.timer', 'stopkit-tm-dd44.service'))

    def test_system_timers(self):
        r = FakeRunner({('systemctl', '--no-pager', 'list-timers', '--all', '-o', 'json'): result(TIMERS)})
        l = list_timers(None, r)
        self.assertEqual((l.ok, l.scope, len(l.items)), (True, 'system', 1))

    def test_empty_timer_list_is_a_successful_empty_listing(self):
        r = FakeRunner({BASE + ('list-timers', '--all', '-o', 'json'): result('[]')})
        l = list_timers(ACC, r)
        self.assertEqual((l.ok, l.items), (True, ()))

    def test_failed_command_proves_nothing(self):
        l = list_timers(ACC, FakeRunner({}))
        self.assertFalse(l.ok)


if __name__ == '__main__':
    unittest.main()
