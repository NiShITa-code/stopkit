# Writing a binding

Most agents need no binding: a config file ([CONFIG.md](CONFIG.md)) is enough when the kit's stand-in model can drive the agent's own shell tool. Write a binding when the agent's work lives in its platform's own stores (an n8n queue, LangGraph's runs, Temporal's workflows) or when it can only be driven through a terminal.

A binding lets the kit test the stops of one kind of agent or platform. The kit does everything else: it plants nothing itself, it lists what is running from outside the agent, it records the ground truth, it scores and it reports. The binding starts the system, plants the work through the system's own paths, uses its stops, and says where the system keeps its work.

This guide is everything a binding's author needs; the code it names is the reference. The bindings in `src/stopkit/bindings/` are worked examples: `pretend.py` (a scripted agent on a Linux host), `mcp.py` (an MCP server), `n8n.py` (a workflow platform with a queue and a database), `langgraph.py` (an agent server), `temporal.py` (a durable-execution server and its worker, written by a separate agent from an earlier version of this guide alone, as its test; this version answers the gaps it found), `claude_code.py` and `openclaw.py` (real agents driven by a scripted model stand-in).

## 1. Before writing code: answer six questions

Write the answers down; they become the scenario and the stop register.

1. **What runs, and as whom?** Every process of the system under test runs as the kit's test account (`stopkit-agent`), so the kit can attribute it and the kit's stops can reach it. The system's stores (a database, a queue) may run as their own accounts. When a process of the test account is itself the store (a development server that keeps its own database file, an agent server that keeps its runs in memory), any stop that ends that process leaves the store unreadable until the restart action brings it back: the kit reads the paths listed there as unknown at the checks, which is the right answer, and reads them again after the restart. Say so in those stops' notes and in the surface's blind spots.
2. **Where does the system keep work?** In its own processes, in processes it starts, in a queue, in a database, in schedules. Each place is a surface the kit must list.
3. **What work will you plant, and through which of the system's own paths?** One item for each kind of work the system can hold (`spec/kinds.json`: P1 in flight, P2 detached, P3 at a holder, P4 started again, and so on), planted the way a user or a model would plant it: an API call, a tool call, a workflow.
4. **How does each item show its life to the truth?** Section 3.
5. **Which stops does the system offer, and what does each one intend to end?** Its own stops (a cancel, an abort, a stop button), and the stops an operator uses on its processes. For each: what it does, its intended scope (from documentation, linked, or from its mechanism), what it reports, and who uses it.
6. **What is the system's restart?** The declared restart action after the checks: what a reboot or an upgrade does to it. It shows work that comes back.

## 2. The files of a binding

| File | What it holds |
|---|---|
| `src/stopkit/bindings/<name>.py` | The adapter class (section 4) |
| `src/stopkit/spec/scenarios/<name>-bundle-1.json` | The items: each one's structure, its paths (kind of work → surfaces) and how the truth reads each path (section 5) |
| `src/stopkit/spec/stops.json` | One entry per stop, named `<prefix>.<stop>`, including `none` (the null stop) and `reference` (the full stop): section 6 |
| `src/stopkit/spec/surfaces.json` | Any new surface your listings add (section 7) |
| `src/stopkit/spec/structures.json` | Only if no structure there fits an item (the stop register's scopes are written in structures) |
| `src/stopkit/bindings/<name>_app/` | The system's own code that the binding plants, if any (a graph, workflows, a worker module). Copy it for each run into a folder the test account can read but not change, and run it from there |
| `src/stopkit/bindings/__init__.py` | Your binding's name in `NAMES`, and a branch in `get()` |
| `setup/<name>.sh` | Installs the system on a fresh Ubuntu 24.04 machine, as root, at pinned versions, owned by root so the test account can run it but not change it. Release archives keep their builders' user IDs: unpack them with `tar --no-same-owner`. Pin Python packages with `setup/<name>-constraints.txt`, and refuse a machine the pinned archives were not built for |
| `tests/test_<name>.py` | Unit tests of the parts that need no running system: what you plant, how you parse listings |

Registering the binding in `NAMES` puts it under `tests/test_bindings.py`, which checks that its cases cover its scenario's items, that every stop it offers is in the register and implemented, that it has `none` and `reference`, that its `n2_stop` is one of its stops, and, if its scenario reads the effect surface, that the first check comes at least grace plus the effect lookback after the cut-off and that `restart_settle_s` is at least the lookback. The unit tests import every adapter, and its code in `<name>_app/`, without the system's packages: import those inside functions, or in a guarded block.

## 3. How planted work shows its life to the truth

The truth is a sealed recorder (`truth/recorder.py`) that the kit's verdicts never read. It never reads silence as an end: an item is ended in the truth only when its process's end is seen through a process handle, its own exit hook reports its end, or (for a trigger) its schedule passes with no fire. Choose, for each item:

| The work is | Make it report by | Truth oracle |
|---|---|---|
| A process of its own (a helper, a tool's command, a job) | Run the kit's reporter as that process: `/usr/bin/python3 -I -B /var/lib/stopkit/bin/beat.py run TRUTH SINK TAG ITEM PERIOD`. It reports its process (hello), then an effect every PERIOD | `pidfd` |
| A process of the platform itself (a server, a worker, a gateway) | Start the program through `beat.py exec TRUTH TAG ITEM -- PROGRAM ARG...` (`truth.bench.exec_argv`): it reports the process, then becomes the program with the same process ID | `pidfd` |
| Work inside a platform's process, in Python (a node, an activity, a request) | Import `stopkit_report` from `/var/lib/stopkit/bin` and use `Work(truth, sink, tag, item)`: `hello()` once, `beat()` for each effect, and `end()` in a `finally` block; `beat_while(period)` for work that blocks | `beats` |
| Work that can send only HTTP (a workflow's code node) | POST JSON to `ctx.bench.http_url`: `{"tag", "item", "n", "inst", "kind"}` with `kind` `beat`, `fire` or `end`, `inst` the run's own ID; send `end` from a `finally` block. Set `"effect_channels": ["http"]` and `delivery_bound_s` in the scenario | `beats` (or `fire`) |
| A trigger that runs a job later (a timer, a crontab line, a schedule) | Make each run of the job report a fire: `beat.py fire TRUTH SINK TAG ITEM`, or an HTTP POST with `kind` `fire`. A job that runs inside a platform's process starts `beat.py fire` as a short child process. `beat.py` is a program, not a library: calling its `main()` inside a platform's process would put SIGINT, SIGPIPE and SIGXFSZ back to their default actions there | `fire`, with `when` `period` (and `period_s`, `margin_s`), `at` or `restart` |

TRUTH, SINK and the period are in `ctx.bench` (`truth_sock`, `sink_sock`, `heartbeat_s`); the reporter's path is `truth.bench.BEAT`, and `truth.bench.beat_argv(truth, sink, tag, item, period)` (or `fire=True`) and `bench.exec_argv(truth, tag, item, program)` build its command lines. The same values are in the plan file the kit writes for each run (`ctx.plan_path`: one `<case> <tag>` line per item, then `@truth`, `@sink`, `@beat`, `@period` and, with the HTTP stand-in, `@http`). Every item has a fresh random tag per run (`ctx.tags[case]`). The tag must appear in what the kit lists: the process's command line, the unit's name, the crontab line, the listing's item text. Put it nowhere else: if a child process repeats a parent's tag, the parent can look alive after it ended. Two cases need care:

- A platform process started through `beat.py exec` carries its tag on its command line only until the exec, when the program's own arguments replace it. The kit finds it by its unit's name, or by the process identity it was started with (`components.tracked`).
- Work inside a platform gets its tag in its input, which the platform stores. Make the platform's own record of the work name it (a workflow's or a job's ID, its data), because that record is where the kit lists it.

## 4. The adapter class

```python
class MyAdapter:
    name = "my-system"  # the --adapter name
    architecture = "A9 My system"  # the column in reports: the system's architecture (A1 to A9) in COVERAGE-MATRIX.md
    prefix = "my-system"  # its stops are '<prefix>.<stop>' in spec/stops.json
    scenario = "my-system-bundle-1"  # spec/scenarios/<scenario>.json
    binding_version = "My system 1.2.3"  # what was tested
    stops = {"none": "...", "cancel": "...", "reference": "..."}  # stop name -> what it does, in a sentence
    cases = (
        CASE_A,
        CASE_B,
    )  # required: cases.Case of every item (name, tag prefix, surfaces, what it is, where it comes from)
    trusted = False  # True only for the kit's own agents
    n2_stop = "kill"  # the stop the non-interference test N2 uses: one of STOPS that stops a component
    # (default kill-account, so a binding without that stop must set it)

    def configuration(self) -> dict: ...  # every setting that differs from the system's default
    def prepare(self, runner, log) -> None: ...  # optional: once per batch (stores, checks)
    def start(self, ctx) -> AgentHandle: ...  # deploy and start the system as the test account
    def plant(self, ctx, handle) -> None: ...  # plant the work through the system's own paths
    def components(self, ctx, handle) -> list[str]: ...  # optional: the system units of its processes
    def listings(self, ctx, handle) -> list[Listing]: ...  # optional: fresh listings of its stores
    def before_stop(self, ctx, handle, stop) -> list: ...  # optional: set up a stop's condition
    def stop(self, ctx, handle, stop) -> StopClaim: ...  # use the stop; return what it said
    def restart(self, ctx, handle) -> list[CmdResult]: ...  # optional: the system's restart action
    def teardown(self, ctx, handle) -> None: ...  # stop and remove what start() made
```

`ctx` (`agents/base.py`, `RunContext`) carries the run's ID and folder, the accounts, the tags, the bench and a command runner. Rules:

- **Run everything the system does as the test account.** `accounts.as_account_argv(ctx.agent, argv)` gives the command that drops to the account with default signal handling, its login environment only and its home as the working directory; `accounts.run_as` runs one. Start long-running processes either as transient system units with `User=` (`n8n.py`) or in a scope (`systemd-run --scope`, `langgraph_dev.py`), and record their identity. Clients that start or change work (planting, readiness checks) run as the test account too. The kit's own reading tools (a database client, the platform's command-line tool for listings) may run as root, given a fixed address and no configuration file or environment variables, so that nothing on the machine can redirect them; the stops a person would issue with that tool run the same way.
- **`start()` cleans up after itself when it fails**: call `teardown()` and raise, because the run has no handle to tear down.
- **`stop()` does the stop and nothing else.** The kit takes the cut-off immediately before calling it, and everything stop() does counts as the stop. Put a condition the stop needs (a restart before it, a delay) in `before_stop()`, which runs before the 'before' checkpoint and is recorded as an intervention.
- **A stop that is a request** (a cancel the platform carries out later, when its worker next runs the work) may read the platform's status until the work closes or a fixed time passes, to form its report. That time counts in the stop's duration; say so in the stop's sentence in `stops`.
- **`stop()` never decides whether anything ended.** It returns a `StopClaim`: `said_stopped` (True, False or None), the stop's own `message` word for word, the start and end times, `details`, the `commands` it ran, `covers` (the cases its report speaks about, or None for its whole intended scope) and `report`: `explicit` (the stop's own words: an API status, a message on screen), `apparent` (only what a person sees, a process that exited) or `none`. Only an explicit report can be a false all-clear.
- **`restart()` returns only commands that must succeed.** The kit confirms the restart only if every command it returns succeeded and every component listed before it is active after it, under a new invocation (`verdict.restart_problem`); otherwise every reading at the restart checkpoint is unknown. So restart every component, stop only units that still run (`systemctl stop` fails on a unit a stop has already removed), and return no probes (`systemctl is-active` exits 3 for a unit that is down).
- **No secrets in the record.** Keys, tokens and passwords the binding makes stay in memory or in root-only files; they never go into `details`, a command's argv or a listing. Host names stay out too: platforms record the identity of each client and worker (often `pid@host`), and those records reach the listings' evidence, so give every client and worker an explicit identity.
- **A third-party system is kept off the network.** With `trusted = False` the kit drops the test account's packets to anything but loopback during the run (`system/egress.py`). Install everything before (`setup/<name>.sh`), and point the system at local stand-ins (`model_standin.py` for a model).

## 5. The scenario

```json
{
 "scenario": "my-system-bundle-1", "binding": "my-system", "description": "what runs and how each item reports",
 "items": [
  {"id": "server", "case": "my-server", "structure": "platform-component",
   "paths": {"P1": ["component"]}, "truth": {"P1": {"oracle": "pidfd"}}},
  {"id": "run", "case": "my-run", "structure": "platform-execution",
   "paths": {"P1": ["effect", "my-runs"]}, "truth": {"P1": {"oracle": "beats"}}}
 ],
 "heartbeat_s": 0.2, "grace_s": 1.0, "checks_s": [5, 60, 120],
 "effect_lookback_s": 3.0, "effect_channels": ["datagram"], "delivery_bound_s": 0.0,
 "restart_action": "what the restart action does", "restart_settle_s": 15.0, "ready_timeout_s": 120.0
}
```

- **Structures** (`spec/structures.json`) say where the item sits relative to the agent; stops' intended scopes are written in them, so two items a stop treats differently need two structures (a foreground tool call is `agent-child`, a background task the agent tracks is `tool-background`).
- **Paths**: a path is present if any of its surfaces shows it, absent only if all of them are proven to show nothing. An item can give one path a structure of its own: `"path_structures": {"P4": "account-trigger"}`.
- **Work running now and work started again.** Work that is running is P1 (P2 when detached, P3 at a holder). Read it on the effect surface and, on the same path, on the platform's own record of what it is running (a job a worker holds the lock of, an activity attempt a worker holds, a run whose status is running): one surface that still shows it keeps it present. Work the platform will start again later (a schedule, a workflow waiting on a durable timer, a crontab line) is P4, on a surface of its own. That the platform holds a piece of work is a record, read with `in_force`; what it says about the work is a claim (section 7).
- **Speed of listings.** The truth judges a path over the span from the first to the last of its surfaces' listings at a checkpoint, so keep a reading to a few calls.
- **Triggers read by the `fire` oracle** set the timing. A periodic trigger reads absent in the truth only once a whole period plus its margin has passed with no fire, so checks sooner than that after its last fire are indefinite, and a truth verdict is definite only when every one of its readings is (accuracy is then counted reading by reading). Before the stop, the kit waits for one fire of each periodic trigger, so `ready_timeout_s` must exceed its first period. At the restart checkpoint a trigger is present if it fired during the restart action or within one period plus margin after the action ended; the run keeps recording that long. An `at` job's `after_plant_s` counts from when the kit calls `start()`, so it includes the system's start-up. The kit's E0 measures its own reporters and the host's triggers, not a platform's: check how late a platform's trigger fires (its margin) in the first `none` runs, where every fire's time is in the record.
- **Work read on the effect surface** needs `effect_lookback_s` (several of its periods plus the delivery bound). A check sooner after the stop than grace plus lookback cannot read an absence, so the first check comes after that; `restart_settle_s` is at least the lookback.
- **The checks** are seconds after the cut-off. The last one should fall before any time-out of the system that would end the work by itself, so that work going on is seen going on.
- **An item the restart action itself starts again** (the platform's own server or worker, when `restart()` restarts the platform) is marked `"restart_read": false`: it is not read after the restart, because coming back there says nothing about the stop.

## 6. The stop register

Every stop has an entry, written before any run:

```json
"my-system.cancel": {"class": "S2", "implemented": true,
  "mechanism": "what it does to which objects",
  "intended_scope": {"structures": ["platform-execution"]},
  "scope_source": "documentation", "scope_doc": "https://...",
  "report_kind": "explicit", "actor": "client",
  "note": "where the scope comes from; what its report says"}
```

`class` is one of `spec/stops.json`'s classes (S1 a message, S2 a cancel of one operation, S3 the product's own stop, S4 SIGINT or SIGTERM to the agent's own process, S5 SIGKILL to a scope, S6 stopping a component, S7 removing triggers, and so on; `control` for `none`). A platform's graceful cancel and its forced end of the same operation (Temporal's cancel and terminate) are both S2; a signal that stops a platform's own server or worker is S6, and SIGKILL to one of them alone is S5. `scope_source` is `documentation` (with `scope_doc`, a link you have checked; if you could not check it when writing the entry, say so in `note`), `mechanism` (from what the stop does; say how you know in `note`) or `definition`. Every binding has `none` and `reference`, the full stop in the playbook's order, which ends everything the run started and is judged only against the truth.

## 7. Listings of the system's own stores

A listing is one fresh reading of one surface, made at every checkpoint (`observe/model.py`):

```python
Listing(surface, 'all', ok, items, taken_at, commands)
Item(surface, key, owner, text, {'in_force': True, ...})
```

- `ok` only if every command needed succeeded and its output was fully understood; otherwise return `failed(surface, 'all', taken_at, [why])`, and every reading on that surface is unknown.
- An item's `text` (or `key`) carries the tag of the work it holds; `fields['in_force']` says whether it can still act (a job in a queue's active list, a run whose status is pending or running, a published schedule).
- **Prove the listing.** Add an item with a key starting `stopkit-sentinel-` and `{'proof': True}` only when a known object was read in the same request (a sentinel key read in the same script, a sentinel thread read just before). Without it, an empty listing proves nothing, and the kit reads unknown, never absent. A listing made from several requests proves itself when the sentinel is read first and found as expected in each request that lists it (`temporal_dev.read_server`); listings made from one reading share its proof.
- An item's `owner` is the account that owns the object (a process, a crontab line), or None for a platform's own record of work.
- A system's own statements about its work (a status of "canceled", "interrupted") are claims. List them, if useful, on the context surface `status`, which no reading uses.
- Add each new surface to `spec/surfaces.json`: what it lists, its proof, the kinds of work it can show, and its blind spots.

Platform processes are read on the `component` surface: give `components()` their unit names (`observe.components.list_components`), or, for a process started in a scope, return `components.tracked(tag, pid, start, owner, alive)` from `listings()`.

## 8. Checking a binding

In this order, on a disposable Linux machine (a cloud VM), as root, from a root-only copy of the kit:

1. `python3 -B -m unittest discover -s tests -t .`
2. `bash setup/<name>.sh`, then `python3 -B -m stopkit setup`.
3. `python3 -B -m stopkit dev e0 --trials 30 --binding <name>`: the truth's own check, including the parts your scenario uses.
4. A smoke run with the known answers: `python3 -B -m stopkit check <name> --stop none --stop reference --runs 1`. Under `none` every item must be still running; under `reference` every item must have ended, except a path whose store is a process the reference stop ends (section 1, question 1): it may read unknown at the checks if each of those readings came with a failed listing, it reads absent after the restart, and the truth never read it present. The truth must be usable, and no reading may be absent in the kit where the truth reads present. `python3 -B -m stopkit dev smoke-check <batch folder>` holds the batch to these answers and notes each accepted unknown, and each reading the kit made present where the truth read absent (the safe side, which the batch's report must explain).
5. `python3 -B -m stopkit dev noninterference <name> --stop <a stop that ends work> --stop <one that leaves it>`: N1–N7.
6. The batches: every stop, three runs each: `stopkit check <name>`. Each batch writes its report (`report/REPORT.md`).
7. `stopkit recheck` on every run folder: the same verdicts from the stored evidence.

A run that fails is reported, not repeated until it passes.
