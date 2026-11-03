"""`stopkit doctor FILE`: is this machine ready, and does the agent of this config file work with the kit? It
plants nothing and tests no stop.

It checks, in order, and stops at the first part that fails:
  1. the file: it can be read, and every field the kit needs is there and usable (agent_config.load);
  2. the machine: root, Linux with systemd 250 or later and cgroup v2, the commands and services the kit uses,
     and free disk (runner.check_machine);
  3. the kit's accounts: they exist, their service managers run, the worker stand-in answers, the sentinels
     are visible and the agent account is empty (`stopkit setup` makes them);
  4. the agent: started as the test account from the file, as a run starts it, with the network blocked, it
     becomes ready, sends the stand-in model a request that offers the shell tool the file names, runs the one
     command the stand-in asks for (echo stopkit-doctor-$((6*7))), and sends back its output, stopkit-doctor-42.
     The output differs from the command, so its return shows the command really ran.
The agent is then stopped and the account emptied. Everything is kept in results/doctor-<id>/.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

from ..agents import agent_config
from ..agents.base import RunContext
from ..agents.configured import DOCTOR_ANSWER as ANSWER, DOCTOR_ECHO as ECHO, ConfiguredAdapter
from ..spec.cases import new_tags
from ..system import KitError, egress
from ..system.cmd import Runner, run
from ..truth.bench import Bench, read_events
from .. import stops
from .runner import check_machine, new_id, prepare

WAIT_S = 120.0


def offered_tools(body: dict) -> set[str]:
    """The tool names a model request offers, in the form of any API the stand-in serves (deferred ones included)."""
    out = set()
    for t in body.get('tools') or []:
        if isinstance(t, dict):
            name = (t.get('function') or {}).get('name') if isinstance(t.get('function'), dict) else t.get('name')
            if name:
                out.add(name)
    return out


def machine_problems(runner: Runner = run) -> list[str]:
    problems = check_machine(runner)
    if not os.path.exists('/sys/fs/cgroup/cgroup.controllers'):
        problems.append('cgroup v2 is not mounted at /sys/fs/cgroup')
    r = runner(['systemctl', '--version'])
    m = re.match(r'systemd (\d+)', r.out or '')
    if not m or int(m.group(1)) < 250:
        problems.append(f"systemd 250 or later is needed; this machine has {(r.out or 'none').splitlines()[0] if r.out else 'none'}")
    return problems


def run_doctor(path: str, out_root: str, runner: Runner = run, proc: str = '/proc',
               log: Callable[[str], None] = print) -> tuple[bool, str | None]:
    """Returns (passed, the folder with everything the check kept, or None if it stopped before the agent)."""
    def step(name: str, problems: list[str]) -> bool:
        log(f"{name}: {'ok' if not problems else 'FAILED'}")
        for p in problems:
            log(f'  {p}')
        return not problems

    try:
        cfg = agent_config.load(path)
    except KitError as e:
        step('the file', [str(e)])
        return False, None
    log(f"the file: ok ({cfg.name}: {'a server' if cfg.server else 'one command per task'}; "
        f"{len(cfg.items)} items to plant through {cfg.shell!r}; stops: {', '.join(cfg.stops)})")
    if not step('the machine', machine_problems(runner)):
        return False, None
    try:
        agent, sentinel, worker = prepare(runner, proc, log)
    except KitError as e:
        step('the accounts', [f'{e}; run `stopkit setup` first if the accounts are not made'])
        return False, None
    log(f'the accounts: ok ({agent.name}, {sentinel.name}, {worker.name})')

    run_id = new_id()
    folder = os.path.join(os.path.abspath(out_root), f'doctor-{run_id}')
    os.makedirs(folder)
    adapter = ConfiguredAdapter(cfg, doctor=True)
    tags = new_tags(adapter.cases)
    bench = Bench(run_id, folder, [agent.uid, worker.uid], sentinel)
    ctx = RunContext(run_id, agent, sentinel, tags, os.path.join(folder, 'plan'), runner, proc, log, worker,
                     tuple(adapter.cases), bench, folder)
    found: dict = {'requests': 0, 'offered': [], 'shell_offered': False, 'answer_returned': False}
    problems: list[str] = []
    handle = None
    t0 = time.monotonic()
    try:
        egress.block(agent, runner)
        bench.start()
        handle = adapter.start(ctx)
        log(f"the agent started (pid {handle.pid}; ready after {handle.details['generations'][0]['ready_s']} s)")
        adapter.plant(ctx, handle)
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and not found['answer_returned']:
            time.sleep(1.0)
            requests = 0
            for req in read_events(os.path.join(folder, 'model.jsonl')):
                body = req.get('body') or {}
                if req.get('method') != 'POST' or not isinstance(body, dict):
                    continue
                requests += 1
                names = offered_tools(body)
                found['offered'] = sorted(set(found['offered']) | names)
                found['shell_offered'] |= cfg.shell in names
                found['answer_returned'] |= ANSWER in json.dumps(body)
            found['requests'] = requests
        if not found['requests']:
            problems.append(f'the agent sent the stand-in model no request in {WAIT_S:g} s: check that [agent.env] '
                            'or [[agent.files]] point it at ${model_url} (see agent-g1.log and task-output.log)')
        elif not found['shell_offered']:
            problems.append(f"the agent's requests offer no tool called {cfg.shell!r}; they offer: "
                            + (', '.join(found['offered']) or 'no tools at all'))
        elif not found['answer_returned']:
            problems.append(f"the agent did not send back the output of `{ECHO}` within {WAIT_S:g} s: check "
                            "[tools] args, and that the agent runs its shell tool without asking a person")
    except KitError as e:
        problems.append(str(e))
    finally:
        try:
            if handle is not None:
                adapter.teardown(ctx, handle)
        finally:
            bench.stop()
            stops.cleanup(agent, runner, proc)
            egress.unblock(runner)
    passed = step('the agent', problems)
    result = {'format': 'stopkit-doctor/1', 'config': os.path.abspath(path), 'config_sha256': cfg.sha256,
              'agent': cfg.name, 'passed': passed, 'problems': problems, 'found': found,
              'seconds': round(time.monotonic() - t0, 1)}
    with open(os.path.join(folder, 'doctor.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(result, f, indent=1, sort_keys=True)
        f.write('\n')
    return passed, folder
