"""The demo agent: a LangGraph graph with no model. Its three steps stand in for what agents' tools do:

  start_helper  starts a helper in the background, in a session of its own, as coding agents start
                development servers
  schedule      adds a line to the account's crontab that runs a follow-up job every minute, as agents set
                scheduled jobs for themselves
  work          the main task: one update a second for thirty minutes, in a plain (synchronous) function

Every action is a request to the outside service that demo.py runs, which shows each one as it arrives.

graph_async is the same main task written as an async function, with no helper and no scheduled job. The server offers
it as `agent_async`, but the demo does not launch it. In round 1 of the LangGraph trials, cancelling the run stopped a
tool that waits with `await` but not a plain function running in a thread (experiments/docs/WHAT-EACH-STOP-REACHES.md at commit 19544dd).
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

OUTSIDE = 'http://127.0.0.1:8899/action'
UPDATES = 1800  # one a second for thirty minutes, longer than the demonstration takes from its task to its last stop
HERE = os.path.dirname(os.path.abspath(__file__))
CRON = ("* * * * * /usr/bin/python3 -c 'import json,urllib.request as u;u.urlopen(u.Request("
        "\"http://127.0.0.1:8899/action\",data=json.dumps({\"source\":\"scheduled job\",\"text\":\"ran\"}).encode(),"
        "headers={\"Content-Type\":\"application/json\"}),timeout=2)' # stopkit-demo")


def send(source: str, text: str) -> None:
    """One action, sent with the process that sent it, so the outside service can tell which server did it."""
    body = {'source': source, 'text': text, 'pid': os.getpid()}
    req = urllib.request.Request(OUTSIDE, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError:
        pass


class State(TypedDict, total=False):
    updates: int


def start_helper(state: State) -> State:
    subprocess.Popen([sys.executable, os.path.join(HERE, 'helper.py')], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    send('agent', 'started a helper in the background')
    return {}


def schedule(state: State) -> State:
    old = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout
    subprocess.run(['crontab', '-'], input=old + CRON + '\n', text=True, check=False)
    send('agent', 'scheduled a follow-up job for every minute')
    return {}


def work(state: State) -> State:
    for n in range(1, UPDATES + 1):
        send('agent', f'update {n}')
        time.sleep(1)
    return {'updates': UPDATES}


async def work_async(state: State) -> State:
    for n in range(1, UPDATES + 1):
        # LangGraph's dev server stops an async step that makes a blocking call, so the request goes to a thread
        await asyncio.to_thread(send, 'agent (async)', f'update {n}')
        await asyncio.sleep(1)
    return {'updates': UPDATES}


builder = StateGraph(State)
builder.add_node('start_helper', start_helper)
builder.add_node('schedule', schedule)
builder.add_node('work', work)
builder.add_edge(START, 'start_helper')
builder.add_edge('start_helper', 'schedule')
builder.add_edge('schedule', 'work')
builder.add_edge('work', END)
graph = builder.compile()

async_builder = StateGraph(State)
async_builder.add_node('work_async', work_async)
async_builder.add_edge(START, 'work_async')
async_builder.add_edge('work_async', END)
graph_async = async_builder.compile()
