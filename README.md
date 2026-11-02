<div align="center">

# Stop test kit

### *Checks from outside whether stopping an AI agent stopped its work*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)

</div>

---

## Overview

When an operator stops an agent, the platform that runs it reports a status such as `canceled`, `interrupted` or
`stopped`. That status describes the platform's record of one run. The agent's work can live elsewhere: in processes it
started, in schedules and crontab lines, in jobs handed to another service, and in credentials that still work. The
kit tests a stop from outside the agent. A *binding* starts one kind of agent or platform as the kit's test account,
`stopkit-agent`, and plants known, harmless, tagged work through the system's own paths. The kit applies one stop,
lists from outside the agent what still runs, what acts and what comes back after a restart, and compares every
reading with a sealed record of the work's own life, the *ground truth*.

```mermaid
flowchart LR
    B["Binding<br/>starts the agent as stopkit-agent"] -->|"plants tagged work"| W["Planted work"]
    W -->|"reports its own life"| T["Sealed ground-truth recorder"]
    B -->|"presses one stop"| W
    O["Listings from outside<br/>processes, units, timers, cron, at,<br/>sockets, platform stores"] -->|"read"| W
    S["Sentinel items<br/>in a second account"] -->|"prove each listing"| O
    O --> V["Readings and verdicts"]
    T --> A["Accuracy against the truth"]
    V --> A
    V --> R["Record and report"]
    A --> R
```

At each check $c$ after the stop, every planted path $i$ gets a reading $x_i(c)$: *present* when a listing shows an
item carrying the path's tag in force; *absent* only when every listing where the path is looked for succeeded and is
proven complete by the sentinel items, the known items the kit keeps in a second account; and *unknown* otherwise. A
path has ended only when it is absent at every check $C$ after the stop and at the restart check $r$, when a restart
is performed:

$$\mathrm{ended}(i) \iff \forall c \in C \cup \{r\} :\ x_i(c) = \mathsf{absent} ,$$

so a doubt is never rounded to "ended". A stop is judged against the scope it is meant to end, its set of covered
paths $S$. When the stop reports that it stopped, its report is a *false all-clear* when a covered path was there after
the stop or came back after the restart:

$$\mathrm{false\ all\text{-}clear} \iff \mathrm{reported\ stopped} \,\wedge\, \exists\, i \in S :\ \bigl(\exists\, c \in C : x_i(c) = \mathsf{present}\bigr) \vee x_i(r) = \mathsf{present} .$$

Work outside the stop's scope is reported as a remainder for operators, not as a failure of the stop. How to test your
own agent is in [docs/CONFIG.md](docs/CONFIG.md). The method and its terms are in [docs/FRAMEWORK.md](docs/FRAMEWORK.md),
the engineering in [docs/DESIGN.md](docs/DESIGN.md), the record format in [docs/FORMAT.md](docs/FORMAT.md), and what has
been checked in [docs/VALIDATION.md](docs/VALIDATION.md).

| Binding | What it runs | Its stops | Set-up |
|---|---|---|---|
| `pretend` | The kit's own scripted agent on a Linux host, which plants processes, services, timers, cron and at jobs, and a job handed to a worker | Signals, kills of a process tree, a control group or an account, and the reference stop | `setup/base.sh` |
| `mcp` | The kit's MCP server on the official Python SDK, with a tool call in progress | A protocol cancel, closing its input, MCP's stdio shutdown, SIGINT as a client exits, a control-group stop | `setup/base.sh` |
| `n8n` | n8n 2.41.7 in queue mode: an execution, a sub-workflow's execution, a schedule | Stop by ID, with and without a restart of n8n's main process before it; the worker stopped; unpublish; killing the account | `setup/n8n.sh` |
| `langgraph` | LangGraph's development server (langgraph-api 0.15.1) running a run with a helper and a crontab line | Cancel, delete the thread, Ctrl+C, SIGTERM, kill, a control-group stop | `setup/langgraph.sh` |
| `temporal` | Temporal's development server (CLI 1.9.1) and a worker (temporalio 1.34.0) | Cancel, terminate, the worker killed or stopped, the server stopped | `setup/temporal.sh` |
| `claude-code` | A real Claude Code (2.1.310) in a terminal, against a scripted stand-in for its model | Esc, Ctrl+C until it quits, SIGTERM, kill-tree, a control-group stop | `setup/claude-code.sh` |
| `openclaw` | OpenClaw's Gateway (2026.10.2), against a scripted stand-in for its model | Abort the session, SIGTERM or SIGKILL to the Gateway, a control-group stop | `setup/openclaw.sh` |

Every binding also has `none`, the null stop, and `reference`, the full stop in the playbook's order. The stand-in model
is a local, scripted model API, so no binding needs a model account. Most other agents need no binding at all: a config
file says how to start the agent, point it at the stand-in model, give it its task and press its own stops, and the kit
plants its catalogue of work through the agent's own shell tool ([docs/CONFIG.md](docs/CONFIG.md)).
[examples/openclaw](examples/openclaw/README.md) is OpenClaw written as such a file. A system whose work lives in its own
stores needs a binding written in Python ([docs/GUIDE.md](docs/GUIDE.md)).

## Getting started

### 01. Installing the kit

Use a disposable Linux machine (Ubuntu 24.04 was used) with systemd 250 or later, cgroup v2 and Python 3.11 or later,
such as a cloud virtual machine: the agents under test run code as the test account. The kit creates the accounts
`stopkit-agent`, `stopkit-sentinel` and `stopkit-worker` and writes to `/var/lib/stopkit`. As root, install the kit into
a folder only root can write, then create its accounts:

```bash
bash setup/base.sh /opt/stopkit
cd /opt/stopkit && python3 -B -m stopkit setup
```

The platform bindings need outbound internet only while their set-up script installs them; during a run, the test
account's packets to anything but loopback are dropped.

### 02. Checking the kit on known answers

```bash
python3 -B -m stopkit selftest
```

The self-test runs the null stop and the reference stop on the host's planted work three times each and requires the
known answers: under the null stop everything still runs, under the reference stop everything has ended, and the kit
agrees with the ground truth wherever the truth is definite. Nothing else should be trusted until it passes.

### 03. Testing your own agent

Install the agent on the machine, then describe it in one file: how to start it, how to point it at the kit's stand-in
model, how to give it its task, the name of its shell tool, and its own stops ([docs/CONFIG.md](docs/CONFIG.md)).

```bash
python3 -B -m stopkit init my-agent.toml
python3 -B -m stopkit doctor my-agent.toml
python3 -B -m stopkit check my-agent.toml
```

`init` writes a template with every field explained. `doctor` reads the file, checks the machine, starts the agent and
confirms that its shell tool runs a command the stand-in model asks for, without planting anything. `check` tests every
stop three times: the kit's own stops (signals, kills of a process tree, a control group or the account, and the
reference stop) and the agent's. Each batch writes a folder under `results/`: one folder per run, with `record.json`,
the evidence (`blobs/`), the ground truth's log (`truth.jsonl`), the effects (`effects.jsonl`), `report.md` and
`SHA256SUMS`, and the batch's report in `report/REPORT.md` and `REPORT.html`: the reach matrix, each run's details,
accuracy against the ground truth, timelines, and what was not covered.

### 04. Testing a ready-made binding

```bash
python3 -B -m stopkit check pretend --stop sigterm --stop kill-scope --runs 3
```

For a platform, run its set-up script first, then the same command with its name and its stops.

### 05. Scoring a run again

On any machine with Python, without root, from the repository:

```bash
PYTHONPATH=src python3 -B -m stopkit recheck validation/selftest-20261027T091012Z-70406a/run-01-none
```

### 06. Checking an agent you stop yourself

Watch mode plants nothing. It lists what an account of your choice holds around a stop you make your own way:

```bash
python3 -B -m stopkit watch before --account AGENT_ACCOUNT
```

Then stop the agent however you normally would, and run `watch after FOLDER` as often as you like, optionally
`watch restart FOLDER --yes` (this restarts that account's services), and `watch report FOLDER`.
`python3 -B -m stopkit status` shows what the kit sees now, and `python3 -B -m stopkit cleanup --all` removes the
accounts and everything else the kit made in them.

### 07. Watching the live demonstration

[examples/langgraph](examples/langgraph/README.md) runs a real LangGraph development server with a scripted workload in
one terminal, presses LangGraph's cancel, Ctrl+C and kill in turn, and shows LangGraph's own log and answers beside the
activity an outside service receives. From the installed copy, as root:

```bash
bash examples/langgraph/demo.sh
```

### 08. Checking the kit itself

| Command | What it checks |
|---|---|
| `.venv/bin/python -m pytest -q tests` | The unit tests, after `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, the package's layers among them; add `STOPKIT_LIVE=1`, as root, to check every listing against the real system |
| `python3 -B -m stopkit dev e0 --trials 30 --fires [--binding B]` | The ground truth on its own: exits recorded within 50 ms, nothing lost, blocked work never read as ended, a cut channel read as indefinite, triggers firing on schedule |
| `python3 -B -m stopkit dev noninterference TARGET --stop S ...` | N1–N7: the kit's observers and instruments do not change what they measure |
| `python3 -B -m stopkit dev smoke-check FOLDER` | A binding's smoke batch against the known answers ([docs/GUIDE.md](docs/GUIDE.md), section 8) |

## The experiments behind it

The kit grew out of shutdown experiments on n8n in queue mode, LangGraph's development server, Temporal, OpenClaw,
Claude Code and Codex. Their guides, frozen code, cleaned records and the checks that rerun every analysis on those
records are in [experiments/](experiments/README.md); `python3 -B experiments/verify.py` reruns all of the checks.

## Repository structure

```
.
├── README.md                             # this page
├── LICENSE                               # Apache License 2.0
├── pyproject.toml                        # the stopkit package and its command; dev extras: pytest, ruff
├── .gitattributes                        # every file is stored byte for byte
├── .gitignore                            # the source archive, the virtual environment and the runs' output stay untracked
├── .gitleaks.toml                        # the secret scan: gitleaks' rules, with the records' identifiers allowed by shape
├── .github/workflows/ci.yml              # secret and identity scans, then lint, the tests, the checks and the commands
├── docs/                                 # the kit's documents
│   ├── CONFIG.md                         # testing your own agent with a config file
│   ├── DESIGN.md                         # the engineering: commands, scope, key ideas, architecture
│   ├── FORMAT.md                         # records, evidence and reports
│   ├── FRAMEWORK.md                      # the method: outcome families, scope, ground truth
│   ├── GUIDE.md                          # writing a binding for another system
│   └── VALIDATION.md                     # the kit's recorded validation runs
├── src/stopkit/                          # the kit
│   ├── __init__.py                       # the version string
│   ├── __main__.py                       # python3 -m stopkit
│   ├── cli.py                            # the commands
│   ├── agents/
│   │   ├── __init__.py                   # layer 5, what an agent is to the kit; the stand-ins' programs
│   │   ├── agent_config.py               # an agent's config file: read, checked, turned into its scenario and stops
│   │   ├── base.py                       # what every binding provides
│   │   ├── configured.py                 # runs an agent from its config file
│   │   ├── delegation.py                 # setting up the worker stand-in
│   │   ├── mcp_server.py                 # the MCP test server
│   │   ├── model_standin.py              # the scripted stand-in model (Anthropic and OpenAI APIs)
│   │   └── worker_standin.py             # the worker stand-in
│   ├── bindings/
│   │   ├── __init__.py                   # layer 6, the binding names; a config file or a name
│   │   ├── claude_code.py                # Claude Code, against the stand-in model
│   │   ├── langgraph.py                  # LangGraph's development server
│   │   ├── mcp.py                        # the kit's MCP test server, driven through a tool call
│   │   ├── n8n.py                        # n8n in queue mode
│   │   ├── openclaw.py                   # OpenClaw's Gateway, against the stand-in model
│   │   ├── pretend.py                    # the kit's scripted test agent
│   │   ├── pretend_agent.sh              # the scripted agent's own process
│   │   ├── temporal.py                   # Temporal's development server and a worker
│   │   ├── langgraph_app/
│   │   │   ├── graph.py                  # the LangGraph binding's agent
│   │   │   └── langgraph.json            # the LangGraph server's configuration
│   │   └── temporal_app/
│   │       └── stopkit_temporal_work.py  # the Temporal binding's workflows, activities and worker
│   ├── observe/
│   │   ├── __init__.py                   # layer 3, the listings, each read fresh from the system
│   │   ├── components.py                 # a platform's own processes, as systemd units
│   │   ├── cron.py                       # crontab lines and at jobs
│   │   ├── effect_sink.py                # where planted work sends its effects
│   │   ├── effects.py                    # which effects each piece of work issued, and when
│   │   ├── managers.py                   # each account's service manager
│   │   ├── model.py                      # items and listings
│   │   ├── processes.py                  # processes and their control groups
│   │   ├── sentinel.py                   # known items that prove each listing worked
│   │   ├── snapshot.py                   # one complete reading of every surface
│   │   ├── sockets.py                    # listening sockets and their processes
│   │   ├── timers.py                     # user and system timers
│   │   ├── units.py                      # an account's user units and unit files
│   │   └── worker.py                     # the jobs handed to the worker stand-in
│   ├── report/
│   │   ├── __init__.py                   # layer 8, records and reports, which never touch the live system
│   │   ├── codehash.py                   # one SHA-256 over the kit's own code, recorded in every run
│   │   ├── record.py                     # run records
│   │   ├── reports.py                    # the batch reports, generated from run records
│   │   ├── results.py                    # rows: one per run, item and kind of work
│   │   └── sheets.py                     # score sheets of the earlier record format
│   ├── run/
│   │   ├── __init__.py                   # layer 9, running
│   │   ├── doctor.py                     # stopkit doctor: the machine and one agent's config file
│   │   ├── e0.py                         # the ground truth checked on its own
│   │   ├── noninterference.py            # the non-interference tests N1–N7
│   │   ├── protocol.py                   # one run of one stop with the bench, batches, the self-test and recheck
│   │   ├── runner.py                     # preparing the accounts; the runs of the earlier record format
│   │   └── watch.py                      # watch mode, around a stop made by hand
│   ├── score/
│   │   ├── __init__.py                   # layer 7, scoring, which never touches the live system
│   │   ├── accuracy.py                   # the kit's readings and verdicts against the ground truth
│   │   ├── coverage.py                   # what each run could see
│   │   ├── ground_truth.py               # ground-truth verdicts from the recorder's log
│   │   └── verdict.py                    # readings and verdicts
│   ├── spec/
│   │   ├── __init__.py                   # loads and checks the files beside it; registers a config file's scenario and stops
│   │   ├── cases.py                      # the host's planted leftovers: what each is, where the kit looks, its tag
│   │   ├── kinds.json                    # the kinds of work an agent can leave
│   │   ├── stops.json                    # the stops and what each is meant to end
│   │   ├── structures.json               # where each piece of work sits
│   │   ├── surfaces.json                 # what each listing reads
│   │   ├── verdicts.json                 # the verdicts, readings and their rules
│   │   ├── version.json                  # the specification's version
│   │   └── scenarios/
│   │       ├── claude-code-bundle-1.json # one scenario: the work a binding plants
│   │       ├── host-bundle-1.json        # one scenario: the work a binding plants
│   │       ├── langgraph-bundle-1.json   # one scenario: the work a binding plants
│   │       ├── mcp-bundle-1.json         # one scenario: the work a binding plants
│   │       ├── n8n-bundle-1.json         # one scenario: the work a binding plants
│   │       ├── openclaw-bundle-1.json    # one scenario: the work a binding plants
│   │       └── temporal-bundle-1.json    # one scenario: the work a binding plants
│   ├── stops/
│   │   └── __init__.py                   # layer 5, the stops the kit carries out itself, and the clean-up
│   ├── system/
│   │   ├── __init__.py                   # layer 2, the machine; KitError
│   │   ├── accounts.py                   # the kit's disposable accounts, and running commands as them
│   │   ├── cmd.py                        # running commands; every one leaves the same evidence
│   │   ├── egress.py                     # keeping a third-party agent off the network during its runs
│   │   ├── evidence.py                   # the evidence store behind every result
│   │   ├── guards.py                     # the checks of the machine before anything changes
│   │   ├── names.py                      # the names and places the kit uses
│   │   └── procfs.py                     # reading the process table
│   └── truth/
│       ├── __init__.py                   # layer 4, the ground truth
│       ├── beat.py                       # the reporter a planted process runs
│       ├── bench.py                      # the bench instruments of one run: the ground-truth recorder and the effect sink
│       ├── http_standin.py               # an outside service that planted work reaches over HTTP
│       ├── recorder.py                   # the sealed ground-truth recorder
│       └── work_report.py                # reporting for work inside a platform's process
├── tests/                                # the kit's unit tests (live ones behind STOPKIT_LIVE=1) and the repository's checks
│   ├── __init__.py                       # the test package
│   ├── helpers.py                        # the fakes the tests share
│   ├── test_accounts.py                  # system/accounts.py
│   ├── test_agent_config.py              # agents/agent_config.py
│   ├── test_bindings.py                  # every binding's specification and its scripted parts
│   ├── test_cases_guards.py              # cases.py and guards.py
│   ├── test_check.py                     # run/watch.py, watch mode
│   ├── test_cmd_evidence.py              # cmd.py and evidence.py
│   ├── test_coverage.py                  # score/coverage.py
│   ├── test_cron.py                      # observe/cron.py
│   ├── test_guards.py                    # system/guards.py
│   ├── test_launch.py                    # how planted processes are launched
│   ├── test_layers.py                    # the package's layers: what each module may import
│   ├── test_live.py                      # every lister against the real system (root, STOPKIT_LIVE=1)
│   ├── test_mcp.py                       # bindings/mcp.py
│   ├── test_n8n.py                       # bindings/n8n.py
│   ├── test_noninterference.py           # run/noninterference.py
│   ├── test_packaging.py                 # what the repository may and may not hold
│   ├── test_platform_core.py             # the parts every platform binding shares
│   ├── test_procfs.py                    # system/procfs.py
│   ├── test_record.py                    # report/record.py
│   ├── test_reports.py                   # report/reports.py
│   ├── test_results.py                   # report/results.py
│   ├── test_snapshot.py                  # observe/snapshot.py
│   ├── test_sockets.py                   # observe/sockets.py
│   ├── test_specs.py                     # spec/__init__.py against the files in spec/
│   ├── test_stops.py                     # the stops the kit carries out itself
│   ├── test_temporal.py                  # bindings/temporal.py
│   ├── test_truth.py                     # score/ground_truth.py
│   ├── test_truth_live.py                # the recorder, sink and reporter as real processes
│   ├── test_units_timers.py              # observe/units.py and observe/timers.py
│   ├── test_verdict.py                   # score/verdict.py
│   ├── test_verdict_rules.py             # verdict.py against spec/verdicts.json
│   └── test_worker.py                    # observe/worker.py
├── setup/                                # machine set-up for the kit and its platform bindings
│   ├── base.sh                           # the kit's machine set-up; installs it into /opt/stopkit
│   ├── claude-code.sh                    # Claude Code, for the Claude Code binding
│   ├── langgraph-constraints.txt         # the LangGraph binding's pinned versions
│   ├── langgraph-requirements.txt        # the LangGraph binding's packages
│   ├── langgraph.sh                      # LangGraph's development server, for the LangGraph binding
│   ├── mcp-2.3.0.txt                     # the MCP binding's pinned packages
│   ├── n8n.sh                            # n8n 2.41.7 in queue mode, for the n8n binding
│   ├── openclaw.sh                       # OpenClaw, for the OpenClaw binding
│   ├── temporal-constraints.txt          # the Temporal binding's pinned packages
│   └── temporal.sh                       # the Temporal CLI and SDK, for the Temporal binding
├── examples/                             # the live LangGraph demonstration; OpenClaw as a config file
│   ├── langgraph/
│   │   ├── README.md                     # how to run the demonstration and what each step shows
│   │   ├── check_log_filter.py           # checks the filter against a saved server log
│   │   ├── constraints.txt               # the pinned versions of requirements.txt
│   │   ├── demo.py                       # the guided demonstration
│   │   ├── demo.sh                       # starts the guided demonstration
│   │   ├── graph.py                      # the scripted workload
│   │   ├── helper.py                     # the detached helper the workload starts
│   │   ├── langgraph.json                # the server's configuration
│   │   ├── log_filter.py                 # which of LangGraph's log lines the demonstration shows
│   │   └── requirements.txt              # the packages the demonstration installs
│   └── openclaw/
│       ├── README.md                     # how to check it, and how it relates to the binding
│       └── agent.toml                    # OpenClaw's Gateway, written as a config file
├── scripts/                              # repository tools
│   └── identity_scan.py                  # refuses a personal name or home folder, in a file, a path or an archive member
├── validation/                           # the kit's recorded validation runs
│   ├── e0-20261027-092228-f20558
│   ├── langgraph-example-20261027.log
│   ├── langgraph-example-20261101-server.log
│   ├── langgraph-example-20261101.log
│   └── selftest-20261027T091012Z-70406a
└── experiments/                          # the earlier studies
    ├── README.md                         # the studies: questions, findings, records, how to recheck them
    ├── RUNBOOK.md                        # regenerating the evidence; each check and what makes it fail
    ├── verify.py                         # reruns every check and compares with results/results.json
    ├── n8n-queue-mode/                   # n8n in queue mode: 19 conditions on 13 October, and the runs of 29 October
    │   └── README.md                     # its guide
    ├── n8n-restart/                      # the stop after a main restart: scripts, 11 reproductions
    │   └── README.md                     # its guide
    ├── langgraph-controls/               # LangGraph's development server: 33 trials on 9 October; a portable copy of their code
    │   ├── README.md                     # its guide
    │   └── code/
    │       └── CHANGES.md                # what changed from the code of the 9 October trials
    ├── temporal-activities/              # Temporal: 16 conditions on 14 October, and the runs of 29 October
    │   └── README.md                     # its guide
    ├── openclaw-commands/                # OpenClaw: three runs on 14 October and one on 29 October
    │   └── README.md                     # its guide
    ├── claude-code-codex/                # Claude Code and Codex: 58 client runs on 25 October, and a batch on 29 October
    │   └── README.md                     # its guide
    ├── docs/                             # the studies' documents, generated pages among them
    │   ├── METHOD.md                     # how an experiment works, with one result recounted from the raw files
    │   ├── WHAT-EACH-STOP-REACHES.md     # per platform: what each stop ended and what it left
    │   ├── n8n-loop-evidence/
    │   │   ├── REPORT.md                 # the n8n loop readings, written by scripts/loop_evidence.py
    │   │   └── rows.jsonl                # the same rows, one per line
    │   └── walkthroughs/
    │       ├── LANGGRAPH.md              # one trial's record lines, merged on one clock
    │       ├── N8N.md                    # one trial's record lines, merged on one clock
    │       ├── OPENCLAW.md               # one trial's record lines, merged on one clock
    │       ├── README.md                 # the walkthrough pages
    │       └── TEMPORAL.md               # one trial's record lines, merged on one clock
    ├── harness/                          # the frozen code the trials ran, one folder per commit
    │   ├── README.md                     # the frozen code: which commit each folder holds, and the names in it
    │   ├── langgraph-cd26851/
    │   ├── n8n-langgraph-7a3fcec/
    │   ├── n8n-scorer-3832d7c/
    │   ├── openclaw-b8fd1e4/
    │   └── temporal-627df0b/
    ├── scripts/                          # the checks, the record build and the page scripts
    │   ├── build_data.py                 # makes the records and the manifest from the source archive
    │   ├── build_results.py              # reruns every check on the records and writes results/results.json
    │   ├── check_pages.py                # checks a walkthrough page against the files it quotes
    │   ├── loop_evidence.py              # the n8n loop rows and their page, from the raw records
    │   ├── walkthrough_langgraph.py      # prints LANGGRAPH.md of docs/walkthroughs
    │   ├── walkthrough_n8n.py            # prints N8N.md of docs/walkthroughs
    │   ├── walkthrough_openclaw.py       # prints OPENCLAW.md of docs/walkthroughs
    │   └── walkthrough_temporal.py       # prints TEMPORAL.md of docs/walkthroughs
    ├── tests/                            # the studies' tests
    │   ├── conftest.py                   # no bytecode written beside the frozen code
    │   ├── test_checks.py                # one changed record per check: the check fails and nothing is written
    │   ├── test_loop_evidence.py         # silence and incomplete readings never count as an end
    │   ├── test_readme.py                # experiments/README.md's figures and quoted output, against a fresh run
    │   └── test_records.py               # the records against the manifest and its substitutions
    ├── setup/                            # machine set-up for each study
    │   ├── claude-code-codex.sh          # one study's set-up
    │   ├── clock_tick.py                 # puts the clock tick back to nominal
    │   ├── common.sh                     # steps every script shares
    │   ├── constraints-temporal.txt      # the Temporal trials' Python packages
    │   ├── langgraph-controls.sh         # one study's set-up
    │   ├── n8n-queue-mode.sh             # one study's set-up
    │   ├── openclaw-commands.sh          # one study's set-up
    │   ├── temporal-activities.sh        # one study's set-up
    │   └── n8n-2.41.7/                   # n8n's pinned packages
    ├── data/
    │   └── MANIFEST.json                 # each record: where it came from, its SHA-256 as shipped, what changed in it
    └── results/
        └── results.json                  # the outcome of every check, written by scripts/build_results.py
```

## Implementation

The kit uses only Python's standard library. Its MCP binding runs the kit's test server on the official Python SDK for
MCP (`mcp` 2.3.0), and each platform binding drives a pinned installation through the platform's own public
interfaces: its API, its command-line tool, signals and the service manager. Every command of the kit runs as root on
the machine under test, and every command that acts for the agent runs as the test account. The observers read the
system without changing it: per-account listings run as the account itself, so a listing never starts a service
manager that a stop has ended. The ground-truth recorder is a separate process that the kit's verdicts never read, so
the two can be compared.

## Citation

A citation will be added here.

## License

Apache License 2.0; see [LICENSE](LICENSE).
