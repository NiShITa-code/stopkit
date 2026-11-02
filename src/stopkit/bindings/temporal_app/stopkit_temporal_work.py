"""The Temporal binding's workflows and activities, and the worker and the clients that run them (temporalio 1.34.0).

The binding copies this file into the run's folder. The worker runs it as the agent account, in a system unit started
through the kit's reporter; short client processes, also run as the agent account, start the planted workflows and
check that the worker is ready:

    python3 -I -B stopkit_temporal_work.py worker --address A --identity I --task-queue Q
    python3 -I -B stopkit_temporal_work.py start  --address A --identity I        (the workflows, as JSON, on stdin)
    python3 -I -B stopkit_temporal_work.py ready  --address A --identity I --task-queue Q --workflow-id W [--timeout S]

The workflows. Each one's input carries the bench's sockets and the tag of the work it runs, never another's:
  ActivityWork  runs one work activity to its end: work_heartbeat, which heartbeats after each effect (Temporal
                delivers a cancel in its reply to a heartbeat), or work_quiet, which never heartbeats
  TimerWork     waits on a durable timer (workflow.sleep), then runs the activity fire; again and again
  ReadyWork     runs the activity ready once, which shows that the worker polls its task queue and runs activities
The sentinel workflow's type is not defined here: it is started on a task queue that no worker polls.

A work activity reports from inside the worker's process (stopkit_report.py, installed by the kit): its first
message, one effect every period, and its end from its finally block, however it ends. fire runs the kit's reporter
(beat.py fire), a short process of its own, which reports one run of the timer's job.

The workflow sandbox imports this module by its name to check the workflows, and restricts what workflow code may
call, so the module does nothing at import but define names, workflow code does no input or output, and the
activities do every effect. temporalio is imported in a guarded block so that the kit's unit tests can import this
file without it.
"""
import asyncio
import json
import os
import sys
import time
from datetime import timedelta

try:
    from temporalio import activity, workflow
except ImportError:                       # the kit's unit tests, which run without temporalio
    activity = workflow = None

REPORT_DIR = '/var/lib/stopkit/bin'       # where the kit installs stopkit_report.py for the test accounts


def _activity(fn):
    return activity.defn(fn) if activity else fn


def _workflow(cls):
    return workflow.defn(cls) if workflow else cls


def _run(fn):
    return workflow.run(fn) if workflow else fn


async def _work(plan: dict, heartbeat: bool) -> int:
    """One effect every period, for a fixed number of steps, reported from this process; with heartbeat, a
    heartbeat after each effect."""
    from stopkit_report import Work
    w = Work(plan['truth'], plan['sink'], plan['tag'], plan['item'])
    w.hello()
    n, why = 0, 'finished'
    due = time.monotonic()
    try:
        while n < plan['steps']:
            n += 1
            w.beat()
            if heartbeat:
                activity.heartbeat(n)
            due += plan['period_s']
            await asyncio.sleep(max(0.0, due - time.monotonic()))
    except asyncio.CancelledError:
        why = 'cancelled'
        raise
    except BaseException as e:
        why = f'{type(e).__name__}: {e}'
        raise
    finally:
        w.end(f'{why} after {n} effects')
    return n


@_activity
async def work_heartbeat(plan: dict) -> int:
    return await _work(plan, heartbeat=True)


@_activity
async def work_quiet(plan: dict) -> int:
    return await _work(plan, heartbeat=False)


@_activity
async def fire(plan: dict) -> int:
    """One run of the timer's job: the kit's reporter issues its effect, reports the fire, and exits."""
    proc = await asyncio.create_subprocess_exec(*plan['fire_argv'], stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait()


@_activity
async def ready(plan: dict) -> str:
    return 'ready'


@_workflow
class ActivityWork:
    """Runs one work activity to its end, with the SDK's default retry policy and cancellation type. It does not
    catch a cancel, so a cancel closes it CANCELED."""

    @_run
    async def run(self, plan: dict) -> int:
        timeout = timedelta(seconds=plan['start_to_close_s'])
        if plan['heartbeat']:
            return await workflow.execute_activity(work_heartbeat, plan, start_to_close_timeout=timeout,
                                                   heartbeat_timeout=timedelta(seconds=plan['heartbeat_timeout_s']))
        return await workflow.execute_activity(work_quiet, plan, start_to_close_timeout=timeout)


@_workflow
class TimerWork:
    """Waits on a durable timer, then runs fire; as many times as the plan says."""

    @_run
    async def run(self, plan: dict) -> int:
        for _ in range(plan['fires']):
            await workflow.sleep(plan['every_s'])
            await workflow.execute_activity(fire, plan, start_to_close_timeout=timedelta(seconds=plan['fire_timeout_s']))
        return plan['fires']


@_workflow
class ReadyWork:
    """One activity that does nothing: run, it shows that a worker polls the task queue."""

    @_run
    async def run(self, plan: dict) -> str:
        return await workflow.execute_activity(ready, plan, start_to_close_timeout=timedelta(seconds=30))


WORKFLOWS = (ActivityWork, TimerWork, ReadyWork)
ACTIVITIES = (work_heartbeat, work_quiet, fire, ready)


async def _serve(args) -> int:
    """Run the worker until SIGTERM or SIGINT, then shut it down with Worker.shutdown(); every worker option is the
    SDK's default, the graceful shutdown timeout (zero) included."""
    import logging
    import signal
    from temporalio.client import Client
    from temporalio.worker import Worker
    client = await Client.connect(args.address, identity=args.identity)
    worker = Worker(client, task_queue=args.task_queue, workflows=list(WORKFLOWS), activities=list(ACTIVITIES),
                    identity=args.identity)
    asked = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, asked.set)
    running = asyncio.ensure_future(worker.run())
    waiting = asyncio.ensure_future(asked.wait())
    await asyncio.wait({running, waiting}, return_when=asyncio.FIRST_COMPLETED)
    if not waiting.done():
        waiting.cancel()
        await running                    # the worker ended by itself: its error, if any, is the exit status
        logging.warning('the worker ended without a signal')
        return 1
    logging.warning('signal received: Worker.shutdown()')
    await worker.shutdown()
    await running
    logging.warning('the worker has shut down')
    return 0


async def _start(args) -> int:
    """Start each workflow given on stdin, in order, as an operator's client does. Prints what was started."""
    from temporalio.client import Client
    wanted = json.load(sys.stdin)
    client = await Client.connect(args.address, identity=args.identity)
    started = []
    for s in wanted:
        try:
            h = await client.start_workflow(s['workflow'], s['input'], id=s['id'], task_queue=s['task_queue'])
            started.append({'id': s['id'], 'run_id': h.result_run_id})
        except Exception as e:           # reported, and the exit status says so
            started.append({'id': s['id'], 'error': f'{type(e).__name__}: {e}'})
    print(json.dumps({'started': started}))
    return 0 if all('run_id' in s for s in started) else 1


async def _ready(args) -> int:
    """Run ReadyWork on the worker's task queue and wait for its result."""
    from temporalio.client import Client
    client = await Client.connect(args.address, identity=args.identity)
    t0 = time.monotonic()
    h = await client.start_workflow('ReadyWork', {}, id=args.workflow_id, task_queue=args.task_queue)
    result = await asyncio.wait_for(h.result(), args.timeout)
    print(json.dumps({'id': args.workflow_id, 'run_id': h.result_run_id, 'result': result,
                      'seconds': round(time.monotonic() - t0, 3)}))
    return 0


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="The Temporal binding's worker and clients.")
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('worker', 'start', 'ready'):
        p = sub.add_parser(name)
        p.add_argument('--address', required=True)
        p.add_argument('--identity', required=True, help='the identity the server records (never a host name)')
        if name != 'start':
            p.add_argument('--task-queue', required=True)
        if name == 'ready':
            p.add_argument('--workflow-id', required=True)
            p.add_argument('--timeout', type=float, default=60.0)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if activity is None:
        print("temporalio is not importable: run this file with the binding's environment (setup/temporal.sh)",
              file=sys.stderr)
        return 2
    if args.command == 'worker':
        import logging
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        sys.path.append(REPORT_DIR)
        return asyncio.run(_serve(args))
    return asyncio.run(_start(args) if args.command == 'start' else _ready(args))


if __name__ == '__main__':
    # Python started with -I leaves this folder off the path. The sandbox imports the workflows by their module's
    # name, which must then be this file's own name, not __main__.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stopkit_temporal_work
    sys.exit(stopkit_temporal_work.main())
