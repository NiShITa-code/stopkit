"""The LangGraph binding's agent: a LangGraph graph with no model (the demo agent, reporting to the kit's bench).
Its steps stand in for what agents' tools do:

  start_helper  starts a helper in the background, in a session of its own, as coding agents start
                development servers
  schedule      adds a line to the account's crontab that runs a job every minute, as agents set scheduled
                jobs for themselves
  work          the main task: one effect every half second for fifteen minutes, in a plain (synchronous)
                function, which LangGraph's development server runs in a thread of its own

The run's input carries the bench's plan: the sockets of the truth recorder and the effect sink, the kit's
reporter, and a tag for each piece of work. The helper and the scheduled job report through the reporter
(beat.py), as processes of their own; the main task reports from inside the server's process
(stopkit_report.py), with its end from its exit hook.
"""
import subprocess
import sys
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

sys.path.insert(0, '/var/lib/stopkit/bin')
from stopkit_report import Work  # noqa: E402

PY = '/usr/bin/python3'
UPDATES = 1800          # one every half second: fifteen minutes, longer than any run's watch


class State(TypedDict, total=False):
    plan: dict
    updates: int


def start_helper(state: State) -> State:
    p = state['plan']
    subprocess.Popen([PY, '-I', '-B', p['beat'], 'run', p['truth'], p['sink'], p['helper'], 'helper', str(p['period'])],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {}


def schedule(state: State) -> State:
    p = state['plan']
    line = f"* * * * * {PY} -I -B {p['beat']} fire {p['truth']} {p['sink']} {p['cron']} cron"
    old = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
    subprocess.run(['crontab', '-'], input=old + line + '\n', text=True, check=False)
    return {}


def work(state: State) -> State:
    p = state['plan']
    w = Work(p['truth'], p['sink'], p['run'], 'run')
    w.hello()
    n = 0
    try:
        for n in range(1, UPDATES + 1):
            w.beat()
            time.sleep(p['work_period'])
    finally:
        w.end(f'the node stopped after {n} updates')
    return {'updates': n}


builder = StateGraph(State)
builder.add_node('start_helper', start_helper)
builder.add_node('schedule', schedule)
builder.add_node('work', work)
builder.add_edge(START, 'start_helper')
builder.add_edge('start_helper', 'schedule')
builder.add_edge('schedule', 'work')
builder.add_edge('work', END)
graph = builder.compile()
