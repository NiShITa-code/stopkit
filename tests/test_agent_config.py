"""Agents from config files (stopkit/agents/agent_config.py, configured.py; docs/CONFIG.md): reading and checking
the file, the scenario and stops it becomes, the stand-in model's script, how a stop's own report is read, and a
record that carries its scenario so that it can be scored again without the file. Nothing here needs root."""
import json
import os
import tempfile
import textwrap
import unittest

from stopkit import bindings, spec
from stopkit.agents import agent_config as ac
from stopkit.agents.configured import ConfiguredAdapter, commands, doctor_script, read_report, script
from stopkit.run.doctor import offered_tools
from stopkit.system.cmd import CmdResult

SERVER = '''
format = "stopkit-agent/1"
[agent]
name = "demo-server"
version = "demo 1.0"
start = ["/usr/bin/demo", "serve", "--state", "${state}"]
ready_url = "http://127.0.0.1:8080/health"
[agent.env]
HOME = "${state}/home"
OPENAI_BASE_URL = "${model_url}/v1"
[[agent.files]]
path = "${state}/home/.demo.json"
content = """{"key": "${token}", "nested": {"a": {"b": 1}}}"""
[vars]
session = "demo-${run}"
[tools]
shell = "exec"
args = { title = "stopkit ${item}", command = "${command}" }
foreground = { command = "${command}", awaitResults = true }
background = { command = "${command}", background = true }
[task]
run = ["/usr/bin/demo", "agent", "--session", "${session}", "--message", "${task}"]
[stops.abort]
does = "abort the session"
run = ["/usr/bin/demo", "abort", "${session}"]
class = "S2"
ends = ["agent-child"]
reports = "explicit"
stopped_when = { json = ["status", "result.status"], equals = "aborted" }
covers = ["foreground"]
[stops.sigterm]
class = "S6"
ends = ["platform-component", "agent-child", "tool-background"]
'''

ONE_SHOT = '''
[agent]
name = "demo-cli"
[agent.env]
ANTHROPIC_BASE_URL = "${model_url}"
[tools]
shell = "Bash"
[task]
run = ["/usr/bin/demo-cli", "-p", "${task}"]
[plant]
skip = ["service", "timer"]
why = "no systemd in its sandbox"
'''


class Bench:
    truth_sock, sink_sock, heartbeat_s = '/run/stopkit-truth/x/truth.sock', '/run/stopkit-truth/x/sink.sock', 0.2


class FileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def write(self, text: str, name: str = 'agent.toml') -> str:
        path = os.path.join(self._dir.name, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent(text))
        return path

    def refused(self, text: str, *words: str):
        with self.assertRaises(ac.ConfigError) as e:
            ac.load(self.write(text))
        for w in words:
            self.assertIn(w, str(e.exception))
        return str(e.exception)


class LoadTest(FileTest):
    def test_the_template_that_init_writes_is_a_usable_file(self):
        cfg = ac.load(self.write(ac.template()))
        self.assertEqual(cfg.name, 'my-agent')
        self.assertTrue(cfg.server)
        self.assertIn('cancel', cfg.stops)

    def test_a_server_plants_every_item_with_the_foreground_command_last(self):
        cfg = ac.load(self.write(SERVER))
        self.assertEqual(cfg.items, tuple(ac.ITEMS))
        self.assertEqual(cfg.items[-1], 'foreground')
        self.assertEqual(cfg.main_structure, 'platform-component')
        self.assertEqual(set(cfg.skipped), set(ac.NOT_PLANTED))

    def test_without_a_background_form_or_with_skips_the_items_are_left_out_and_said_why(self):
        cfg = ac.load(self.write(ONE_SHOT))
        self.assertNotIn('background', cfg.items)
        self.assertNotIn('service', cfg.items)
        self.assertEqual(cfg.skipped['service'], 'no systemd in its sandbox')
        self.assertIn('background', cfg.skipped)
        self.assertFalse(cfg.server)
        self.assertEqual(cfg.main_structure, 'agent-main')

    def test_generic_stops_take_their_scope_from_the_agent_shape_and_can_be_redeclared(self):
        server, cli = ac.load(self.write(SERVER)), ac.load(self.write(ONE_SHOT, 'b.toml'))
        self.assertEqual(server.stops['kill'].ends, ('platform-component',))
        self.assertEqual(cli.stops['kill'].ends, ('agent-main',))
        self.assertEqual((server.stops['sigterm'].cls, server.stops['sigterm'].ends),
                         ('S6', ('platform-component', 'agent-child', 'tool-background')))
        self.assertTrue(server.stops['sigterm'].generic)
        self.assertFalse(server.stops['abort'].generic)
        self.assertEqual(server.stops['abort'].covers, ('foreground',))

    def test_what_is_refused_names_the_field(self):
        self.refused('[agent]\nname = "Bad Name"\n[tools]\nshell = "x"\n[task]\nrun = ["x"]\n', '[agent] name')
        self.refused(SERVER.replace('[vars]', '[vars]\nbad = "${nope}"'), '${nope}', '[vars] bad')
        self.refused(SERVER.replace('class = "S6"', 'run = ["kill", "1"]'), '[stops.sigterm]', 'carries out itself')
        self.refused(SERVER.replace('stopped_when = { json = ["status", "result.status"], equals = "aborted" }', ''),
                     '[stops.abort] stopped_when')
        self.refused(SERVER.replace('ends = ["agent-child"]', 'ends = ["agent-chid"]'), 'unknown structure', 'agent-chid')
        self.refused(SERVER.replace('path = "${state}/home/.demo.json"', 'path = "/etc/demo.json"'), '${state}/')
        self.refused(SERVER + '\n[restart]\nrun = ["x"]\n', '[restart]')
        self.refused(SERVER.replace('name = "demo-server"', 'name = "openclaw"'), 'ready-made binding')
        self.refused(ONE_SHOT.replace('skip = ["service", "timer"]', 'skip = ["foreground"]'), 'foreground')
        self.refused(SERVER.replace('[agent]', '[agnet]\n[agent]'), "unknown field 'agnet'")
        self.refused(SERVER.replace('class = "S2"', 'class = "S2"\nscope_source = "documentation"'), 'https://')
        self.refused('[agent\n', 'not valid TOML')
        self.refused(SERVER.replace('class = "S6"', 'class = "S6"\nreports = "explicit"'), 'is fixed for sigterm')

    def test_fill_replaces_names_inside_tables_and_lists_and_keeps_an_escaped_dollar(self):
        self.assertEqual(ac.fill({'a': ['${x}/y', 'z'], 'b': 2, 'c': '$${x} ${x}'}, {'x': 'X'}),
                         {'a': ['X/y', 'z'], 'b': 2, 'c': '${x} X'})


class ScenarioTest(FileTest):
    def test_the_scenario_and_stops_pass_the_spec_rules_and_carry_the_main_process_first(self):
        cfg = ac.load(self.write(SERVER))
        adapter = ConfiguredAdapter(cfg)              # registers them
        scen = spec.scenario(adapter.scenario)
        self.assertEqual([i.id for i in scen.items], ['agent'] + list(cfg.items))
        self.assertFalse(scen.items[0].raw['restart_read'])
        self.assertEqual(spec.stop('demo-server.abort').report_kind, 'explicit')
        self.assertEqual(spec.stop('demo-server.sigterm').cls, 'S6')
        self.assertEqual(sorted(adapter.stops), sorted(cfg.stops))
        self.assertEqual({c.name for c in adapter.cases}, {'agent'} | set(cfg.items))
        self.assertTrue(hasattr(adapter, 'restart'))
        ConfiguredAdapter(cfg)                        # the same data again: accepted

    def test_an_agent_of_one_command_per_task_has_no_restart_of_its_own(self):
        adapter = ConfiguredAdapter(ac.load(self.write(ONE_SHOT)))
        self.assertFalse(hasattr(adapter, 'restart'))
        self.assertTrue(spec.scenario(adapter.scenario).items[0].raw.get('restart_read', True))

    def test_bindings_resolve_a_config_file_and_refuse_what_is_neither(self):
        self.assertIsInstance(bindings.resolve(self.write(SERVER)), ConfiguredAdapter)
        self.assertEqual(bindings.resolve('pretend').name, 'pretend')
        with self.assertRaises(Exception):
            bindings.resolve('no-such-binding')

    def test_a_record_carries_its_scenario_and_stop_so_another_process_can_register_them(self):
        cfg = ac.load(self.write(SERVER.replace('demo-server', 'demo-record')))
        data, entries = ac.scenario_data(cfg), ac.stop_entries(cfg)
        rec = {'system': {'scenario_data': data}, 'stop_spec': {'id': 'demo-record.abort',
                                                                 'register_entry': entries['demo-record.abort']}}
        spec.register_from_record(rec)
        self.assertEqual(spec.scenario('config-demo-record').name, 'config-demo-record')
        self.assertEqual(spec.registered('stop', 'demo-record.abort'), entries['demo-record.abort'])
        changed = json.loads(json.dumps(rec))
        changed['stop_spec']['register_entry']['mechanism'] = 'something else'
        with self.assertRaises(spec.SpecError):
            spec.register_from_record(changed)
        with self.assertRaises(spec.SpecError):
            spec.register(stops={'host.none': entries['demo-record.abort']})


class ScriptTest(FileTest):
    def test_every_step_calls_the_shell_tool_with_its_command_in_the_declared_arguments(self):
        cfg = ac.load(self.write(SERVER))
        tags = {i: ac.case_prefix(i) + 'abc' for i in ('agent',) + cfg.items}
        s = script(cfg, tags, Bench, {'run': 'r1', 'token': 't'})
        self.assertEqual([st['name'] for st in s['steps']], list(cfg.items))
        self.assertTrue(all(st['tool'] == 'exec' for st in s['steps']))
        by = {st['name']: st['input'] for st in s['steps']}
        self.assertEqual(by['foreground'], {'command': commands(tags, Bench)['foreground'], 'awaitResults': True})
        self.assertTrue(by['background']['background'])
        self.assertEqual(by['cron']['title'], 'stopkit cron')
        for item, inp in by.items():
            self.assertIn(tags[item], inp['command'])
            self.assertIn(Bench.truth_sock, inp['command'])
        named = commands(tags, Bench, '/home/stopkit-agent/.config/systemd/user')
        self.assertIn('/home/stopkit-agent/.config/systemd/user/' + tags['service'] + '.service', named['service'])
        self.assertNotIn('~', named['timer'])     # an agent may run with HOME elsewhere

    def test_the_doctor_script_asks_for_one_command_whose_output_differs_from_it(self):
        cfg = ac.load(self.write(ONE_SHOT))
        [step] = doctor_script(cfg, {})['steps']
        self.assertEqual(step['tool'], 'Bash')
        self.assertIn('$((6*7))', step['input']['command'])

    def test_offered_tools_are_read_in_both_apis(self):
        self.assertEqual(offered_tools({'tools': [{'type': 'function', 'function': {'name': 'exec'}}]}), {'exec'})
        self.assertEqual(offered_tools({'tools': [{'name': 'Bash', 'input_schema': {}}]}), {'Bash'})


class ReportTest(unittest.TestCase):
    def r(self, rc=0, out=''):
        return CmdResult(('x',), rc, out, '', 0.0, 0.0)

    def test_a_stop_reads_stopped_by_its_rule(self):
        self.assertEqual(read_report({'exit': 0}, self.r(0))[0], True)
        self.assertEqual(read_report({'exit': 0}, self.r(7))[0], False)
        rule = {'json': ['status', 'result.status'], 'equals': 'aborted'}
        self.assertEqual(read_report(rule, self.r(0, '{"result": {"status": "aborted"}}'))[0], True)
        self.assertEqual(read_report(rule, self.r(0, 'noise\n{"status": "running"}'))[0], False)
        self.assertEqual(read_report(rule, self.r(0, 'not json'))[0], False)
        self.assertEqual(read_report({'output_contains': 'stopped'}, self.r(0, 'run stopped'))[0], True)
        pretty = json.dumps({'ok': True, 'status': 'aborted'}, indent=2) + '\n'
        self.assertEqual(read_report(rule, self.r(0, pretty))[0], True)       # a reply printed over several lines


if __name__ == '__main__':
    unittest.main()
