"""Keeping a third-party agent off the network while it is tested (PLAN-OF-RECORD: no third-party system is
touched; EXECUTION-PLAN.md section 1: runners have no route out).

During a run of a binding whose agent is not the kit's own, every packet the agent account sends to anything
but the loopback interface is dropped, by an nftables rule matched on the sending socket's owner. The bench,
the stand-ins and the platform's own stores (a database, a queue) are reached over loopback and keep working;
packages are installed before any run, as root, which the rule does not touch. The rule's counter shows how
many packets were dropped, and goes into the run's record.
"""
from __future__ import annotations

from .accounts import Account
from .cmd import CmdResult, Runner, run

TABLE = 'stopkit_egress'


def block(account: Account, runner: Runner = run) -> list[CmdResult]:
    """Drop the account's packets to anything but loopback (IPv4 and IPv6). Replaces an earlier table."""
    rules = (f'table inet {TABLE} {{\n'
             '  chain out {\n'
             '    type filter hook output priority 0; policy accept;\n'
             f'    meta skuid {account.uid} oifname != "lo" counter drop\n'
             '  }\n'
             '}\n')
    out = [runner(['nft', 'delete', 'table', 'inet', TABLE])]          # fails harmlessly when there is none
    out.append(runner(['nft', '-f', '-'], input=rules))
    return out


def status(runner: Runner = run) -> CmdResult:
    """The table with its counter: how many packets the agent account tried to send out, and was refused."""
    return runner(['nft', 'list', 'table', 'inet', TABLE])


def unblock(runner: Runner = run) -> CmdResult:
    return runner(['nft', 'delete', 'table', 'inet', TABLE])
