"""Every binding's specification, and the pure parts of the bindings that run a real agent: the scripts the model
stand-in plays, in the Anthropic and the Chat Completions formats, and OpenClaw's configuration."""
import json
import os
import sys
import unittest

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
