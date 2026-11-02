"""The kit's MCP test server, built with the official Python SDK (`mcp` 2.3.0, as in the MCP cancel study).

Each tool starts a job, a child process that sleeps under the name of a tag, and waits for it: a tool call
in progress whose work is a separate process. The synchronous tool waits as `subprocess.run` does, the
asynchronous one as `asyncio.create_subprocess_exec` does.

With the bench (`report` given), the call reports itself as work that is not a process of its own: the
request beats from a thread of its own while the call is in progress and reports its end from its exit hook
(stopkit_report.py), and the child is the kit's reporter (beat.py), a process of its own that the truth
watches and whose effects reach the sink. Without it the child is a silent sleeping process, as in kit 0.3.0.

Run as: python -I -B mcp_server.py AGENT_TAG    (stdio transport; the tag only names the server's process)
"""
import asyncio
import re
import subprocess
import sys

from mcp.server.mcpserver import MCPServer

TAG = re.compile(r'^[A-Za-z0-9-]{1,100}$')
BIN = '/var/lib/stopkit/bin'
server = MCPServer('stopkit-test-server')


def _job(tag: str, report: dict | None) -> list[str]:
    if not TAG.match(tag):
        raise ValueError('a tag is letters, digits and hyphens')
    if report:
        return ['/usr/bin/python3', '-I', '-B', f'{BIN}/beat.py', 'run', str(report['truth']), str(report['sink']),
                tag, 'child', str(float(report.get('period', 0.2)))]
    return ['/bin/bash', '-c', 'exec -a "$0" sleep infinity', tag]


def _work(report: dict | None):
    """The request's own reporter, or None without the bench."""
    if not report:
        return None
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    from stopkit_report import Work
    w = Work(str(report['truth']), str(report['sink']), str(report['request_tag']), 'request')
    w.hello()
    return w


@server.tool()
def start_job(tag: str, report: dict | None = None) -> str:
    """Start a job (a child process) and wait for it to finish, as a synchronous tool does."""
    work = _work(report)
    stop_beats = work.beat_while(float(report.get('period', 0.2))) if work else None
    try:
        subprocess.run(_job(tag, report), check=False)
        return 'done'
    finally:
        if work:
            stop_beats()
            work.end('the tool returned')


@server.tool()
async def start_job_async(tag: str, report: dict | None = None) -> str:
    """Start a job (a child process) and wait for it to finish, as an asynchronous tool does."""
    work = _work(report)
    stop_beats = work.beat_while(float(report.get('period', 0.2))) if work else None
    try:
        proc = await asyncio.create_subprocess_exec(*_job(tag, report))
        await proc.wait()
        return 'done'
    finally:
        if work:
            stop_beats()
            work.end('the tool returned or was cancelled')


if __name__ == '__main__':
    server.run()
