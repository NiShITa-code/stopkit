"""A stand-in for the model behind an agent: a scripted, local model API, so that a real agent can be tested with
no model and no account. Standard library only. It serves the Anthropic Messages API (POST /v1/messages, streamed
or not), for Claude Code, adapted from the client probe's stand-in (mcp-cancel-spike/client-probe/stub_model.py,
25 Oct 2026); the OpenAI Chat Completions API (POST /v1/chat/completions, streamed), for OpenClaw's custom
provider, framed as in the OpenClaw spike's stand-in (stopcheck-openclaw/openclaw/stub_completions.py, 14 Oct 2026);
and the OpenAI Responses API (POST /v1/responses, streamed), for the Codex CLI's custom provider, framed as in the
client probe's stand-in for Codex (experiments/claude-code-codex/stub_model.py, 25 Oct 2026).

    python3 -I -B model_standin.py CONTROL LOG

It listens on 127.0.0.1 at a port the system picks and prints "stopkit-model-ready <port>". CONTROL is a JSON
file with the run's script: {"steps": [{"name": ..., "tool": "Bash", "input": {...}}, ...]}. Each request is
answered with the next step whose tool use the conversation does not hold yet, in order; when every step has
been asked for, with a short closing text. A request that offers no tools (a title, a summary) gets "ok".
Claude Code 2.1.310 can defer tools; a step whose tool is offered only by name is asked for through ToolSearch
first, as a real model would. The Codex CLI's shell tool returns after at most 30 s with a longer command still
running; the stand-in then polls that command, as a model waiting for it would (responses_plan).

Every request is written to LOG without its credential headers: what the agent sent the model, including what
it said about a stop (an interrupted tool call, a task it stopped), is evidence of the agent's own report.
"""
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKIP_HEADERS = {'authorization', 'x-api-key', 'cookie'}
LOCK = threading.Lock()
# Codex's tool that writes to, or with no characters polls, a command its exec_command tool still runs, and the
# line of exec_command's output that says the command still runs (Codex 0.164.0).
POLL_TOOL = 'write_stdin'
RUNNING = re.compile(r'Process running with session ID (\d+)')


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'steps': []}


def used_tools(body):
    """The tool uses the conversation already holds, in order: [(name, input), ...]."""
    out = []
    for m in body.get('messages') or []:
        if isinstance(m.get('content'), list):
            for b in m['content']:
                if isinstance(b, dict) and b.get('type') == 'tool_use':
                    out.append((b.get('name'), b.get('input') or {}))
    return out


def plan(body, script):
    tools = body.get('tools') or []
    if not tools:
        return {'kind': 'text', 'text': 'ok'}
    loaded = {t.get('name') for t in tools if isinstance(t, dict) and not t.get('defer_loading')}
    used = used_tools(body)
    done = [n for n, _ in used if n != 'ToolSearch']
    steps = script.get('steps') or []
    for i, step in enumerate(steps):
        if i < len(done):
            continue
        name = step['tool']
        if name not in loaded and re.search(r'\b%s\b' % re.escape(name), json.dumps(body)) \
                and 'ToolSearch' in loaded and ('ToolSearch', {'query': f'select:{name}', 'max_results': 5}) not in used:
            return {'kind': 'tool', 'name': 'ToolSearch', 'input': {'query': f'select:{name}', 'max_results': 5}, 'step': step['name']}
        return {'kind': 'tool', 'name': name, 'input': step['input'], 'step': step['name']}
    return {'kind': 'text', 'text': script.get('closing', 'Done.')}


def chat_plan(body, script):
    """The same script for the OpenAI Chat Completions API (OpenClaw's custom provider). Tool calls already made
    are counted from the assistant messages; their IDs are not matched, because OpenClaw rewrites them (it removed
    the underscores in the spike of 14 Oct 2026)."""
    tools = {(t.get('function') or {}).get('name') for t in body.get('tools') or [] if isinstance(t, dict)}
    if not tools:
        return {'kind': 'text', 'text': 'ok'}
    done = sum(len(m.get('tool_calls') or []) for m in body.get('messages') or []
               if isinstance(m, dict) and m.get('role') == 'assistant')
    steps = script.get('steps') or []
    if done < len(steps):
        step = steps[done]
        return {'kind': 'tool', 'name': step['tool'], 'input': step['input'], 'step': step['name']}
    return {'kind': 'text', 'text': script.get('closing', 'Done.')}


def chat_stream(handler, p):
    """One streamed Chat Completions body, framed as OpenClaw's own mock frames it: `data: <json>` lines and a
    final `data: [DONE]`."""
    base = {'id': 'chatcmpl-stopkit', 'object': 'chat.completion.chunk', 'model': 'scripted'}
    if p['kind'] == 'tool':
        chunks = [dict(base, choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]),
                  dict(base, choices=[{'index': 0, 'delta': {'tool_calls': [
                      {'index': 0, 'id': 'call' + uuid.uuid4().hex[:20], 'type': 'function',
                       'function': {'name': p['name'], 'arguments': json.dumps(p['input'], separators=(',', ':'))}}]},
                      'finish_reason': None}]),
                  dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}])]
    else:
        chunks = [dict(base, choices=[{'index': 0, 'delta': {'role': 'assistant', 'content': p['text']}, 'finish_reason': None}]),
                  dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
    data = (''.join(f'data: {json.dumps(c, separators=(",", ":"))}\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Content-Length', str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
    handler.wfile.flush()


def text_of(value) -> str:
    """The text of a tool's output in the Responses API: a string, or a list of content parts."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(text_of(v) for v in value)
    if isinstance(value, dict):
        return text_of(value.get('text', ''))
    return ''


def responses_plan(body, script):
    """The same script for the OpenAI Responses API (the Codex CLI's custom provider). Tool calls already made are
    counted from the function_call items of the input. Codex's exec_command returns after at most 30 s, leaving a
    longer command running, and its write_stdin with no characters polls that command; so once every step has been
    asked for, while the last tool output says its command still runs, the stand-in polls it, as a model waiting
    for the command would, and the foreground command, the last step, holds the turn for as long as it runs. The
    polls are not steps."""
    tools = {t.get('name') for t in body.get('tools') or [] if isinstance(t, dict)}
    if not tools:
        return {'kind': 'text', 'text': 'ok'}
    items = [i for i in body.get('input') or [] if isinstance(i, dict)]
    done = sum(1 for i in items if i.get('type') == 'function_call' and i.get('name') != POLL_TOOL)
    steps = script.get('steps') or []
    if done < len(steps):
        step = steps[done]
        return {'kind': 'tool', 'name': step['tool'], 'input': step['input'], 'step': step['name']}
    outputs = [i for i in items if i.get('type') == 'function_call_output']
    running = RUNNING.search(text_of(outputs[-1].get('output'))) if outputs else None
    if running and POLL_TOOL in tools:
        return {'kind': 'tool', 'name': POLL_TOOL, 'step': 'poll',
                'input': {'session_id': int(running.group(1)), 'chars': '', 'yield_time_ms': 300000}}
    return {'kind': 'text', 'text': script.get('closing', 'Done.')}


def responses_stream(handler, p):
    """One streamed Responses body, framed as the client probe's stand-in framed it for Codex 0.164.0: the response
    created, its one output item done, and the response completed."""
    rid = 'resp_' + uuid.uuid4().hex[:24]
    if p['kind'] == 'tool':
        item = {'type': 'function_call', 'call_id': 'call_' + uuid.uuid4().hex[:20], 'name': p['name'],
                'arguments': json.dumps(p['input'], separators=(',', ':'))}
    else:
        item = {'type': 'message', 'role': 'assistant', 'id': 'msg_' + uuid.uuid4().hex[:20],
                'content': [{'type': 'output_text', 'text': p['text']}]}
    events = [{'type': 'response.created', 'response': {'id': rid}},
              {'type': 'response.output_item.done', 'item': item},
              {'type': 'response.completed', 'response': {'id': rid, 'usage': {
                  'input_tokens': 10, 'input_tokens_details': None, 'output_tokens': 12, 'output_tokens_details': None,
                  'total_tokens': 22}}}]
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    for e in events:
        handler.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
        handler.wfile.flush()


def stream(handler, model, p):
    def ev(event, data):
        handler.wfile.write(f'event: {event}\ndata: {json.dumps(data)}\n\n'.encode())
        handler.wfile.flush()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    ev('message_start', {'type': 'message_start', 'message': {
        'id': 'msg_' + uuid.uuid4().hex[:24], 'type': 'message', 'role': 'assistant', 'model': model, 'content': [],
        'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 1}}})
    if p['kind'] == 'tool':
        ev('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {
            'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:22], 'name': p['name'], 'input': {}}})
        ev('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                   'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(p['input'])}})
        reason = 'tool_use'
    else:
        ev('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})
        ev('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': p['text']}})
        reason = 'end_turn'
    ev('content_block_stop', {'type': 'content_block_stop', 'index': 0})
    ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None},
                         'usage': {'output_tokens': 12}})
    ev('message_stop', {'type': 'message_stop'})


def whole(model, p):
    if p['kind'] == 'tool':
        content = [{'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:22], 'name': p['name'], 'input': p['input']}]
        reason = 'tool_use'
    else:
        content, reason = [{'type': 'text', 'text': p['text']}], 'end_turn'
    return {'id': 'msg_' + uuid.uuid4().hex[:24], 'type': 'message', 'role': 'assistant', 'model': model,
            'content': content, 'stop_reason': reason, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 12}}


def main(argv):
    if len(argv) != 3:
        print(__doc__.split('\n\n')[2], file=sys.stderr)
        return 2
    control, log_path = argv[1], argv[2]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def _record(self, raw, body, reply):
            with LOCK, open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'t': time.time(), 'mono': time.monotonic(), 'method': self.command, 'path': self.path,
                                    'headers': {k: v for k, v in self.headers.items() if k.lower() not in SKIP_HEADERS},
                                    'bytes': len(raw), 'body': body, 'reply': reply}) + '\n')

        def _json(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_HEAD(self):
            self._record(b'', {}, None)
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            self._record(b'', {}, None)
            if self.path.rstrip('/').endswith('/models'):
                self._json(200, {'object': 'list', 'data': [{'id': 'stub-model', 'object': 'model', 'type': 'model',
                                                             'display_name': 'stub'}], 'has_more': False})
            else:
                self._json(404, {'error': {'type': 'not_found', 'message': 'stand-in: unknown path'}})

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b''
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            path = self.path.split('?')[0]
            if path.endswith('/messages/count_tokens'):
                self._record(raw, body, {'kind': 'count_tokens'})
                self._json(200, {'input_tokens': max(1, len(raw) // 4)})
            elif path.endswith('/messages'):
                p = plan(body, load(control))
                self._record(raw, body, p)
                model = body.get('model', 'stub-model')
                if body.get('stream'):
                    stream(self, model, p)
                    self.close_connection = True
                else:
                    self._json(200, whole(model, p))
            elif path.endswith('/chat/completions'):
                p = chat_plan(body, load(control))
                self._record(raw, body, p)
                chat_stream(self, p)
            elif path.endswith('/responses'):
                p = responses_plan(body, load(control))
                self._record(raw, body, p)
                responses_stream(self, p)
                self.close_connection = True
            else:
                self._record(raw, body, {'kind': 'unknown'})
                self._json(404, {'error': {'type': 'not_found', 'message': 'stand-in: unknown path'}})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    print(f'stopkit-model-ready {server.server_address[1]}', flush=True)
    server.serve_forever(poll_interval=0.2)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
