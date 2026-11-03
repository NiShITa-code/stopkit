"""Every binding's specification, and the pure parts of the bindings that run a real agent: the scripts the model
stand-in plays, in the Anthropic, the Chat Completions and the Responses formats, and OpenClaw's configuration."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request

from stopkit import agents, bindings, spec

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, agents.HERE)  # the stand-in model is a program; import it from its folder, in any layout
import model_standin  # noqa: E402
sys.path.pop(0)


class Bench:
    truth_sock, sink_sock, heartbeat_s, http_url = '/run/t/truth.sock', '/run/t/sink.sock', 0.2, None


class Ctx:
    def __init__(self, tags):
        self.tags = tags
        self.bench = Bench()
        self.run_id = '20261029T000000Z-abcdef'


class SpecTest(unittest.TestCase):
    def test_every_binding_has_a_scenario_and_registered_stops(self):
        for name in bindings.NAMES:
            a = bindings.get(name)
            with self.subTest(binding=name):
                sc = spec.scenario(a.scenario)
                self.assertEqual({c.name for c in a.cases} >= {it.case for it in sc.items}, True)
                for stop in a.stops:
                    self.assertTrue(spec.stop(f'{a.prefix}.{stop}').implemented, stop)
                self.assertIn('none', a.stops)
                self.assertIn('reference', a.stops)
                self.assertIn(getattr(a, 'n2_stop', 'kill-account'), a.stops)     # N2 uses it (noninterference.n2)
                if sc.effect_lookback_s:
                    self.assertGreaterEqual(min(sc.checks_s), sc.grace_s + sc.effect_lookback_s)
                    self.assertGreaterEqual(sc.restart_settle_s, sc.effect_lookback_s)

    def test_scopes_tell_the_turn_from_background_work(self):
        esc = spec.stop('claude-code.esc')
        self.assertTrue(esc.in_scope('agent-child'))
        self.assertFalse(esc.in_scope('tool-background'))
        abort = spec.stop('openclaw.abort')
        self.assertTrue(abort.in_scope('agent-child'))
        self.assertFalse(abort.in_scope('tool-background'))
        self.assertTrue(spec.stop('openclaw.gateway-sigterm').in_scope('tool-background'))
        self.assertFalse(spec.stop('langgraph.cancel').in_scope('agent-detached'))


class AnthropicScriptTest(unittest.TestCase):
    SCRIPT = {'steps': [{'name': 'a', 'tool': 'Bash', 'input': {'command': 'one'}},
                        {'name': 'b', 'tool': 'TaskStop', 'input': {'task_id': 'x'}}], 'closing': 'Done.'}

    def body(self, used=(), tools=('Bash',), deferred=()):
        messages = [{'role': 'user', 'content': 'go'}]
        for name in used:
            messages.append({'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': name, 'input': {}}]})
            messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'ok'}]})
        offered = [{'name': n} for n in tools] + [{'name': n, 'defer_loading': True} for n in deferred]
        return {'messages': messages, 'tools': offered}

    def test_steps_in_order_then_the_closing(self):
        p = model_standin.plan(self.body(), self.SCRIPT)
        self.assertEqual((p['name'], p['input']), ('Bash', {'command': 'one'}))
        p = model_standin.plan(self.body(used=('Bash',), tools=('Bash', 'TaskStop')), self.SCRIPT)
        self.assertEqual(p['name'], 'TaskStop')
        p = model_standin.plan(self.body(used=('Bash', 'TaskStop'), tools=('Bash', 'TaskStop')), self.SCRIPT)
        self.assertEqual(p, {'kind': 'text', 'text': 'Done.'})

    def test_a_deferred_tool_is_loaded_first(self):
        body = self.body(used=('Bash',), tools=('Bash', 'ToolSearch'), deferred=('TaskStop',))
        p = model_standin.plan(body, self.SCRIPT)
        self.assertEqual((p['name'], p['input']['query']), ('ToolSearch', 'select:TaskStop'))

    def test_a_side_request_gets_ok(self):
        self.assertEqual(model_standin.plan({'messages': [], 'tools': []}, self.SCRIPT)['text'], 'ok')


class ChatScriptTest(unittest.TestCase):
    def test_tool_calls_are_counted_not_matched_by_id(self):
        script = {'steps': [{'name': 'a', 'tool': 'exec', 'input': {'command': 'one'}},
                            {'name': 'b', 'tool': 'exec', 'input': {'command': 'two'}}]}
        tools = [{'type': 'function', 'function': {'name': 'exec'}}]
        body = {'tools': tools, 'messages': [{'role': 'user', 'content': 'go'}]}
        self.assertEqual(model_standin.chat_plan(body, script)['input'], {'command': 'one'})
        body['messages'] += [{'role': 'assistant', 'tool_calls': [{'id': 'callREWRITTEN', 'function': {'name': 'exec'}}]},
                             {'role': 'tool', 'tool_call_id': 'callREWRITTEN', 'content': 'Command still running'}]
        self.assertEqual(model_standin.chat_plan(body, script)['input'], {'command': 'two'})


class ResponsesScriptTest(unittest.TestCase):
    """The Responses API, as Codex 0.164.0 speaks it: its exec_command leaves a long command running and says so."""
    SCRIPT = {'steps': [{'name': 'background', 'tool': 'exec_command', 'input': {'cmd': 'one'}},
                        {'name': 'foreground', 'tool': 'exec_command', 'input': {'cmd': 'two'}}], 'closing': 'Done.'}
    TOOLS = [{'type': 'function', 'name': 'exec_command'}, {'type': 'function', 'name': 'write_stdin'}]
    RUNNING = [{'type': 'input_text', 'text': 'Wall time: 30.0021 seconds\nProcess running with session ID 7\nOutput:'}]

    def body(self, *calls, tools=TOOLS):
        items = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'go'}]}]
        for name, output in calls:
            items += [{'type': 'function_call', 'name': name, 'arguments': '{}', 'call_id': 'c'},
                      {'type': 'function_call_output', 'call_id': 'c', 'output': output}]
        return {'tools': list(tools), 'input': items}

    def test_steps_in_order_then_the_closing(self):
        p = model_standin.responses_plan(self.body(), self.SCRIPT)
        self.assertEqual((p['name'], p['input']), ('exec_command', {'cmd': 'one'}))
        p = model_standin.responses_plan(self.body(('exec_command', 'Process running with session ID 3')), self.SCRIPT)
        self.assertEqual(p['input'], {'cmd': 'two'})          # a command left running before the last is not polled
        done = self.body(('exec_command', 'x'), ('exec_command', 'Wall time: 1 seconds\nProcess exited with code 0'))
        self.assertEqual(model_standin.responses_plan(done, self.SCRIPT), {'kind': 'text', 'text': 'Done.'})

    def test_the_last_command_is_polled_while_it_runs_and_the_polls_are_not_steps(self):
        body = self.body(('exec_command', 'Process running with session ID 3'), ('exec_command', self.RUNNING))
        p = model_standin.responses_plan(body, self.SCRIPT)
        self.assertEqual((p['name'], p['input']), ('write_stdin', {'session_id': 7, 'chars': '', 'yield_time_ms': 300000}))
        body = self.body(('exec_command', 'x'), ('exec_command', self.RUNNING), ('write_stdin', self.RUNNING))
        self.assertEqual(model_standin.responses_plan(body, self.SCRIPT)['name'], 'write_stdin')
        body = self.body(('exec_command', 'x'), ('exec_command', self.RUNNING), ('write_stdin', 'Process exited with code 143'))
        self.assertEqual(model_standin.responses_plan(body, self.SCRIPT)['text'], 'Done.')

    def test_no_poll_without_the_tool_and_ok_without_tools(self):
        body = self.body(('exec_command', 'x'), ('exec_command', self.RUNNING), tools=self.TOOLS[:1])
        self.assertEqual(model_standin.responses_plan(body, self.SCRIPT)['text'], 'Done.')
        self.assertEqual(model_standin.responses_plan({'input': [], 'tools': []}, self.SCRIPT)['text'], 'ok')

    def test_the_served_stream_and_its_log(self):
        """The stand-in as a program: a streamed /v1/responses reply in the events Codex reads, logged without the key."""
        with tempfile.TemporaryDirectory() as d:
            control, log = os.path.join(d, 'script.json'), os.path.join(d, 'model.jsonl')
            with open(control, 'w') as f:
                json.dump(self.SCRIPT, f)
            p = subprocess.Popen([sys.executable, '-I', '-B', agents.MODEL_STANDIN, control, log], stdout=subprocess.PIPE)
            try:
                port = int(p.stdout.readline().decode().split()[1])
                req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/responses', method='POST',
                                             data=json.dumps(self.body() | {'stream': True}).encode(),
                                             headers={'Content-Type': 'application/json', 'Authorization': 'Bearer k'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    events = [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith('data: ')]
            finally:
                p.kill()
                p.wait()
            with open(log, encoding='utf-8') as f:
                logged = [json.loads(line) for line in f]
        self.assertEqual([e['type'] for e in events], ['response.created', 'response.output_item.done', 'response.completed'])
        item = events[1]['item']
        self.assertEqual((item['type'], item['name'], json.loads(item['arguments'])), ('function_call', 'exec_command', {'cmd': 'one'}))
        self.assertEqual(events[2]['response']['id'], events[0]['response']['id'])
        self.assertEqual([e['reply']['step'] for e in logged], ['background'])
        self.assertNotIn('authorization', {k.lower() for k in logged[0]['headers']})


class AgentScriptsTest(unittest.TestCase):
    def test_claude_code_plants_through_its_bash_tool(self):
        from stopkit.bindings import claude_code
        ctx = Ctx({c.name: c.prefix + 'aa' for c in claude_code.ClaudeCodeAdapter.cases})
        s = claude_code.script(ctx)
        self.assertEqual([x['name'] for x in s['steps']], ['cron', 'service', 'detached', 'background', 'foreground'])
        self.assertTrue(all(x['tool'] == 'Bash' for x in s['steps']))
        self.assertIs(s['steps'][3]['input']['run_in_background'], True)
        for case, step in (('cc-bg', 3), ('cc-fg', 4), ('cc-detached', 2), ('cc-cron', 0), ('cc-service', 1)):
            self.assertIn(ctx.tags[case], json.dumps(s['steps'][step]))
        self.assertNotIn(ctx.tags['cc'], json.dumps(s))          # only Claude Code's own process carries its tag

    def test_openclaw_plants_through_exec_and_its_config_points_at_the_stand_in(self):
        from stopkit.bindings import openclaw
        ctx = Ctx({c.name: c.prefix + 'aa' for c in openclaw.OpenClawAdapter.cases})
        s = openclaw.script(ctx)
        self.assertEqual([x['input'].get('background') for x in s['steps']], [True, None, None, None])
        self.assertIs(s['steps'][-1]['input']['awaitResults'], True)
        cfg = openclaw.config('tok', 4321)
        self.assertEqual(cfg['models']['providers']['standin']['baseUrl'], 'http://127.0.0.1:4321/v1')
        self.assertIs(cfg['tools']['toolSearch'], False)
        self.assertEqual(cfg['gateway']['auth']['token'], 'tok')


if __name__ == '__main__':
    unittest.main()
