# Claude Code and Codex: what reaches a tool's server when the agent is stopped, 25 October 2026

Claude Code and the Codex CLI are AI agents that run in a terminal. Many of the tools they use are provided by separate
programs, servers that speak the Model Context Protocol (MCP): the agent's client starts each server and sends it a
request for every tool call. When a person stops the agent while a tool call is running, the client can send the server
MCP's cancel message (`notifications/cancelled`), signal the server's process, or send nothing, and the work the tool
started may stop or carry on. This experiment, the client probe, asks what each client sends on each of its stops, what
then happens to the work, and what the person and the model are told.

It drove Claude Code 2.1.308 and 2.1.310 and the Codex CLI 0.164.0 and 0.165.0-alpha.1 on one machine with Ubuntu
24.04.1: 58 runs, three of each stop and one control run per client version, in a seeded random order.
Claude Code 2.1.308 is the version in a public report that its `TaskStop` tool said "Successfully stopped task" while a
database query ran on ([anthropics/claude-code#104714](https://github.com/anthropics/claude-code/issues/104714)); Codex
0.165.0-alpha.1 contains a change that sends a cancel when a pending tool call is dropped
([openai/codex#56132](https://github.com/openai/codex/pull/56132)), and 0.164.0 does not. Each run starts the client's
own interactive terminal interface in a pseudo-terminal, with a fresh home folder and one MCP server, `probe_server.py`,
whose one tool, `slow`, starts a child process that writes a marker line to a file every 0.5 s for 15 s and replies
when the child ends. The server logs, with times, every message it receives, every SIGINT, SIGTERM and SIGHUP, and the
closing of its input, and it ignores cancels on purpose, so that the record shows what each client sends. The model is
`stub_model.py`, a local stand-in for the Anthropic and OpenAI APIs that asks for the `slow` tool (Claude Code first
loads it with its `ToolSearch` tool, since it lists MCP tools to the model only by name) and saves every request the
client sends, so no account, API key or paid model is involved. 1.2 s after the tool's child starts, the harness,
`run_probe.py`, applies one stop and watches for 10 s: one Esc key; a tool timeout of 3 s; Claude Code's stop of a
background task (the call moves to the background after 2 s, and the stand-in then calls `TaskStop` on it); quitting
with Ctrl+C (Claude Code exits at the third press; Codex opens a menu, in which the harness chooses "Exit"); or SIGTERM
to the client's process. If the client is still open after the watch, the harness types one more message, because a
client tells the model nothing about a stopped call until the person types again.

## Results

What reached the server, with times in seconds after the stop. Each range covers the three runs of each version in its
column, and every run of a cell behaved the same way:

| Stop | Claude Code 2.1.308 and 2.1.310 | Codex 0.164.0 | Codex 0.165.0-alpha.1 |
|---|---|---|---|
| Esc | A cancel after 0.09–0.28 s, reason "AbortError: user-cancel" | Nothing | A cancel after 0.006–0.063 s, reason "tool call response future dropped" |
| Timeout | A cancel 0.04–0.78 s after the timeout fired, reason "SdkError: Request timed out" | Nothing | A cancel within 0.001 s of the timeout, with the same reason as on Esc |
| Background-task stop | A cancel 0.05–0.23 s after the model called `TaskStop`, reason "AbortError: The operation was aborted." | (no such stop) | (no such stop) |
| Quit | A cancel at the first Ctrl+C, after 0.03–0.13 s; at exit, SIGINT to the server's process only, at 0.63–0.71 s, and the server exits | Nothing | A cancel at 0.51 s, when Exit is chosen, and nothing else |
| SIGTERM to the client | No cancel; SIGINT to the server's process only, at 0.02–0.14 s, and the server exits | Nothing | Nothing |
| None (control) | The tool finished and its result was sent | The same | The same |

The work the tool started ran on after every stop. In all 54 runs with a stop, the tool's child kept writing markers
through the watch, 16 to 19 of them more than 0.6 s after the stop. After a cancel this follows from the test server,
which ignores cancels; a server that acts on the cancel could stop its work. Where the client exited, on quit and
SIGTERM, it follows from the clients, which left the child running. Claude Code starts the server as its own child
process, in the client's own process group, and at exit sends SIGINT to the server's process alone, so the server exits
and its child runs on to the end. Codex, in both versions, runs MCP servers inside a shared app-server daemon, a
background process that the interface starts in a session of its own, with the server as the daemon's child. No quit
or SIGTERM of Codex signaled the server, and in all 12 of those runs the tool finished and sent its result to the
daemon. After SIGTERM, the daemon also passed that result on to the model in all 6 runs, the next step of the agent's
turn, taken after the person's interface had gone.

What the clients reported overstates the stop. After Esc, Claude Code told the model that the call, which had been
running for more than a second, "was rejected"; `TaskStop` told the model "Successfully stopped task" while the work
ran on; and after Exit, Codex showed the person "The current turn was stopped" while the tool ran on in the daemon.

The predictions were written before any run, in [`PREDICTIONS.md`](PREDICTIONS.md). Of the 48 that the re-check scores,
37 held in every run of their cell and 11 failed in every run. Eight of the failures are one mistake: from Codex's MCP
client code the prediction was that quitting or killing Codex would signal the server's whole process group and so end
the child, but the server belongs to the daemon, not to the interface. The other three are the prediction of no cancel
on quit: Claude Code's first Ctrl+C sends one before the third press exits, in both versions, and Codex
0.165.0-alpha.1 sends one when Exit is chosen. The re-check,
[`records/20261025T142258Z/check.md`](records/20261025T142258Z/check.md), gives every cell's measures, what the model
was told and what the terminal showed. All 522 files of the batch match its checksums, the client executables were
unchanged at the end, and the re-derivation from the raw files differs from the harness's own records in no field.
Three runs failed before their stop and were each repeated once, as the rules in `PREDICTIONS.md` allow: one Claude
Code run left its typed prompt unsent, and two Codex runs never became ready.

## Contents

| Path | What it is |
|---|---|
| `run_probe.py` | The harness: starts each client in a pseudo-terminal, answers its first-run screens, types the prompt, applies the stop, measures, and writes the records (modes `smoke`, `batch` and `resume`) |
| `probe_server.py` | The test MCP server, using only Python's standard library (the records cite this file by its SHA-256, so it is kept as it ran) |
| `stub_model.py` | The stand-in for the model APIs: the Anthropic Messages API for Claude Code and the OpenAI Responses API for Codex, on 127.0.0.1 |
| `check_probe.py` | The re-check: re-derives every measure from the raw files, compares it with the harness's records, scores the predictions and writes `check.md` and `check.json` in the batch folder |
| [`PREDICTIONS.md`](PREDICTIONS.md) | The design and the predictions, written on 25 October before any run, with each change to the harness and the batch's history added under dated headings |
| `clients/` | The npm package lists and lock files from which the set-up installs the four clients |
| `records/20261025T142258Z/` | The 25 October batch: everything the harness wrote, the re-check's `check.md` and `check.json`, and `cost.json` (the size of the model requests in the newest versions' Esc runs) |
| `records/batch-console-2*.log` | The batch's console output, in two parts: the batch was resumed after 41 measured runs, in the same order |
| [`run-2026-10-29/`](run-2026-10-29/) | The run of 29 October on a fresh cloud machine, made with the steps below |

The code files are the versions that ran the resumed part of the batch. The exact copies that ran each part are in the
batch folder, under `experiments/harness/` and `harness-resume-20261025T151940Z/`, and their SHA-256 values are in its
`manifest.json` and `resume.json`.

Inside a batch folder, `runs.jsonl` holds one record per run: the process ids, the stop's actions and their times, the
screen text, and the measures. The raw files, one per run, are in `serverlogs/` (every message the server received,
each signal, the closing of its input, its exit, and the start of the tool's child, with times), `marks/` (each marker
line the tool's child wrote, with its time), `stub/` (every request the client sent to the stand-in, with the reply it
got), `tty/` (everything the client wrote to its terminal) and `client-state/` (the client's own transcript and logs).
`manifest.json` records the SHA-256 of each client's executable and of the harness files, the constants and the run
order; `clients-after.json` confirms that the executables were unchanged at the end; and `SHA256SUMS.json` holds the
SHA-256 of every file the harness wrote. The records keep the absolute paths of the machine they were made on, except a home-folder path, replaced by
one of the same length (experiments/data/MANIFEST.json, substitution `home-path`).

## Checking the records

From this folder, with Python 3.10 or newer and nothing else to install:

```bash
python3 check_probe.py records/20261025T142258Z
```

It checks every file against `SHA256SUMS.json`, re-derives each run's measures from the raw files using only the action
times the harness wrote, compares them with the harness's records, scores each prediction run by run, and rewrites
`check.md` and `check.json` in the batch folder. The shipped copies of those two files were written on Windows, so
`git diff --ignore-cr-at-eol` shows that the rewritten ones are the same. From the top of the repository,
`python3 -B experiments/scripts/build_results.py --only client_probe_recheck` runs the same re-check on a copy of the 25 October batch and
of the batch of 29 October and compares each `check.json` with the saved one; `python3 -B experiments/verify.py`
reruns it with every other check of this repository.

## Running the probe

On a disposable Ubuntu 24.04 machine on x86-64, as root (see [../README.md](../README.md)), from the top of this
repository (a cloud virtual machine, or WSL 2):

```bash
bash experiments/setup/claude-code-codex.sh
```

This installs Node.js 24.22.1 (downloaded from nodejs.org and checked against a fixed SHA-256) and, each in its own
folder under `/root/mcp-clients`, where the harness looks for them, Claude Code 2.1.308 and 2.1.310 and the Codex CLI
0.164.0 and 0.165.0-alpha.1, from the lock files in `clients/`; then it checks each client's executable against the
SHA-256 recorded on 25 October. It also installs tmux and makes an empty folder `/mnt/c`, because the harness reads the
free disk space there (under WSL that is the Windows drive). The harness, the server and the stand-in need only
Ubuntu's own Python and its standard library, and no account, sign-in or key is needed, since the clients send their
model requests to the stand-in. On a cloud machine the set-up took under a minute. Then, still as root, from
`experiments/claude-code-codex`, one run of each client (under half a minute each):

```bash
python3 run_probe.py smoke claude-2.1.310 esc
python3 run_probe.py smoke codex-0.165.0-alpha.1 esc
```

`smoke` takes one client (`claude-2.1.308`, `claude-2.1.310`, `codex-0.164.0` or `codex-0.165.0-alpha.1`) and one or
more stops separated by commas (`control`, `esc`, `timeout`, `bg-stop` for Claude Code only, `quit`, `sigterm`), and
prints what happened in each run. The whole batch of 58 runs, in the same order as on 25 October, took 22 minutes on a
cloud machine; start it inside tmux (open a session with `tmux new -s probe` and type the commands there), so that it
outlives your connection:

```bash
python3 run_probe.py batch
python3 check_probe.py results/<batch>
```

Each command writes a new folder, `results/<time>/` (`results/smoke-<time>/` for `smoke`), with the same files as
`records/20261025T142258Z/`, and `check_probe.py` adds its `check.md`, whose tables you can compare with the recorded
ones. Codex 0.165.0-alpha.1 copies about 450 MB into each run's home folder, which the harness deletes when the run
ends, and the harness stops the batch when the free disk space falls below `MIN_FREE_GB` in `run_probe.py`, leaving
a `STOPPED-low-disk-<time>.txt` in the batch's folder. The runs' scratch homes go under `~/client-probe-live/<batch>/`
and are deleted after each run; the batch's `stub-control.json` stays there.

## The run of 29 October

[`run-2026-10-29/`](run-2026-10-29/) holds a run made with exactly these steps on a fresh cloud
machine (Ubuntu 24.04.5) on 29 October: the console output of the set-up, of the two single runs and of the whole
batch, and the three result folders with their re-checks. The set-up found all four executables identical to those of
25 October, the single runs took 22 and 23 s and each behaved as its cell had on 25 October, and no run of the batch
was excluded. In every one of the 22 cells the batch behaved as on 25 October: the same cancels with the same reasons,
the same signals and server exits, the work running on after every stop (18 or 19 markers), the same texts to the
model and on the screen, the same process groups and sessions, Codex's daemon again passing the tool's result to the
model after SIGTERM in all 6 runs, and the same score for each of the 48 predictions. Only the times and the exact
marker counts differ, and most times are shorter on the cloud machine: Claude Code's cancel on Esc, for example, came
after 0.067–0.084 s. The exit of Codex's interface on quit reads 0.60 s in every quit run of the batch of 29 October
and 0.30 s in five of the six of 25 October, because those six came before the batch was resumed, when the harness
took that time before sending the menu keys (`PREDICTIONS.md`, the paragraph "Second batch, stopped and resumed").
