"""Which lines of LangGraph's own log the demo shows, and how (standard library only, so the check runs anywhere).

The demo shows every warning or error, every line about a run, every line about shutting down, and the line that
says the server has started. The rest stays only in the full log: start-up settings, statistics printed every few
seconds, the start-up banner, and the lines of a traceback (the warning before a traceback names its exception).
check_log_filter.py checks a run's printed lines against its full log with this same module.
"""
from __future__ import annotations

import datetime
import re
import time

ANSI = re.compile(r'\x1b\[[0-9;]*m')
LINE = re.compile(r'^(\S+Z) \[(\w+)\s*\] (.*?)\s+\[([\w.]+)\](.*)$')     # time [level] message [logger] key=value...
HEAD = re.compile(r'^(\S+Z) \[(\w+)\s*\] (.*)$')     # the first line of an entry whose message runs over several lines
PROBLEM = re.compile(r'\b\w*(Warning|Error|Exception): .*')
KV = re.compile(r'(\w+)=(\[[^\]]*\]|\S+)')
RUN_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
ABOUT_RUNS = re.compile(r'\bruns?\b', re.IGNORECASE)
ABOUT_STOPPING = re.compile(r'shutting down|shutdown|stopping|exiting', re.IGNORECASE)


def parse(raw: str) -> tuple[str, str, str, str, str] | None:
    """(time stamp, level, message, logger, the rest) of a structured log line; None for any other line. For the
    first line of an entry that runs over several lines, logger and rest are empty."""
    line = ANSI.sub('', raw).strip()
    m = LINE.match(line)
    if m:
        return m.groups()
    m = HEAD.match(line)
    return (m.group(1), m.group(2), m.group(3), '', '') if m else None


def shows(level: str, message: str) -> bool:
    return (level not in ('debug', 'info') or bool(ABOUT_RUNS.search(message))
            or bool(ABOUT_STOPPING.search(message)) or message.startswith('Application started up'))


def shown(raw: str) -> tuple[float, str] | None:
    """A line the demo shows, as (its time, the text printed); None for a line left in the full log."""
    p = parse(raw)
    if not p or not shows(p[1], p[2]):
        return None
    stamp, _level, message, _logger, rest = p
    problem = PROBLEM.search(message)          # "path/to/file.py:12: UserWarning: ..." is shown from its warning
    message = problem.group(0) if problem else message
    kv = dict(KV.findall(rest))
    ids = [x[:8] for x in RUN_ID.findall(kv.get('run_id', '') or kv.get('run_ids', ''))]
    extra = ([f"run {', '.join(ids)}"] if ids else []) + [f'{k.split("_")[0]} {kv[k]}' for k in ('graph_id', 'action')
                                                         if k in kv]
    try:
        t = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except ValueError:
        t = time.time()
    return t, f"LangGraph's log: {message[:90]}" + (f" ({'; '.join(extra)})" if extra else '')
