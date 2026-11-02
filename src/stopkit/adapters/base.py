"""What an adapter is: the small piece that starts one kind of agent and uses its stop (DESIGN.md section 8).

The kit does everything else: it plans the leftovers, takes the listings, scores them and writes the record.
An adapter never decides whether anything ended. It records what the stop reported, and says what kind of
report that is: the stop's own explicit words, only what a person would see (the agent's process exited), or
nothing at all. A signal or a protocol cancel says nothing; only an explicit claim can be a false all-clear.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ..accounts import Account
from ..cmd import CmdResult, Runner


@dataclass
class RunContext:
    run_id: str
    agent: Account
    sentinel: Account
    tags: dict[str, str]          # case name -> tag
    plan_path: str                # the plan file: one "<case> <tag>" line per case, readable by the agent
    runner: Runner
    proc: str
    log: Callable[[str], None]
    worker: Account | None = None   # the worker stand-in's account, whose jobs are processes to list
    cases: tuple = ()               # the cases this adapter's agent plants (cases.Case)
    bench: Any = None               # the run's bench (bench.Bench): its sockets, and the HTTP stand-in's address
    run_dir: str | None = None      # the run's folder, where a binding may keep its own evidence


@dataclass
class AgentHandle:
    pid: int                      # the agent's main process
    start: int                    # its start time (clock ticks since boot): with pid, its identity
    pgid: int
    scope: str | None             # the agent's own control group, as a systemd unit, if it has one
    popen: subprocess.Popen | None = None
    output: list[str] = field(default_factory=list)   # what the agent printed while starting
    details: dict[str, Any] = field(default_factory=dict)
    client: Any = None                                # the adapter's own connection to the agent, if any


EXPLICIT, APPARENT, NO_REPORT = 'explicit', 'apparent', 'none'


@dataclass(frozen=True)
class StopClaim:
    """What the stop reported. said_stopped: True (stopped), False (not stopped) or None (nothing either way).
    report says what that rests on:
      explicit  the stop's own words: a message, an API status, or an exit status that means stopped
      apparent  only what a person would see: the agent's process exited (or not); the stop said nothing
      none      nothing at all"""
    said_stopped: bool | None
    message: str
    started: float
    finished: float
    details: Mapping[str, Any] = field(default_factory=dict)   # JSON-ready
    commands: tuple[CmdResult, ...] = ()                       # every command the stop ran, kept whole
    covers: tuple[str, ...] | None = None    # the cases its report is about (None: all)
    report: str = APPARENT


class Adapter(Protocol):
    """The contract every binding meets (GUIDE.md says how to write one). Required: the attributes and the
    four methods below. Optional attributes, read with these defaults when absent:
      binding_version = None           what was tested, as the system names its version
      n2_stop = 'kill-account'         the stop the non-interference test N2 uses
      legacy_cases = None              the cases `stopkit test` uses without the bench (the MCP binding only)
    Optional methods, used when present:
      configuration()                  every setting of the system under test that differs from its default
      prepare(runner, log)             once per batch, before its first run: start stores, check the set-up
      components(ctx, handle)          the systemd units that run the platform's own processes; listed at
                                       every checkpoint (the component surface), and the restart confirmed
                                       against them
      listings(ctx, handle)            fresh listings of the platform's own stores of work, at every checkpoint
      before_stop(ctx, handle, stop)   an action that sets up the stop's condition, after the work is ready and
                                       before the 'before' checkpoint (n8n's main restarted); returns commands
      restart(ctx, handle)             the binding's declared restart action, in place of restarting the
                                       account's service manager; returns its commands
    """
    name: str
    architecture: str                                          # for example 'A3 n8n (queue mode)'
    prefix: str                                                # its stops' prefix in the stop register
    scenario: str                                              # the scenario file it realises
    stops: Mapping[str, str]                                   # stop name -> what it does
    cases: tuple                                               # cases.Case of every item its scenario holds
    trusted: bool                                              # True only for the kit's own agents

    def start(self, ctx: RunContext) -> AgentHandle: ...
    def plant(self, ctx: RunContext, handle: AgentHandle) -> None: ...
    def stop(self, ctx: RunContext, handle: AgentHandle, stop: str) -> StopClaim: ...
    def teardown(self, ctx: RunContext, handle: AgentHandle) -> None: ...
