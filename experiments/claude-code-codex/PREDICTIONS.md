# Client probe: what Claude Code and Codex do when a running MCP tool call is stopped (design and predictions, written 25 Oct 2026, before any run)

## Question

A person stops an agent in Claude Code or the Codex CLI while one of its MCP tool calls is running. They might press Esc, let a timeout fire, stop a background task, or quit the program.
- What reaches the MCP server: a cancel, a signal, or nothing?
- What happens to the work the tool started?
- What is the person told, and what is the model told?

The SDK client test (`../clients/README.md`) showed that the official SDK clients send a cancel whenever their caller aborts a call. Whether these two products abort the call at all, on each stop path, is the open question.

Two public reports make it concrete:
- [anthropics/claude-code#104714](https://github.com/anthropics/claude-code/issues/104714) (23 Oct 2026, Claude Code 2.1.308): `TaskStop` on a backgrounded MCP call reported "Successfully stopped task", while a MongoDB query ran on for about 2 hours. The reporter did not know whether a cancel was sent.
- [openai/codex#56132](https://github.com/openai/codex/pull/56132) (merged 22 Oct 2026): "Dropping a local tool-call future did not notify the MCP server that the request was cancelled, leaving remote work running."

## Clients

Each is installed in its own folder under `~/mcp-clients` in WSL 2 (Ubuntu 24.04.1), with nothing installed globally.

| Client | Versions | Why these |
|---|---|---|
| Claude Code | 2.1.310 and 2.1.308 | 2.1.310 is the latest release. 2.1.308 is the `stable` tag and the version in #104714. |
| Codex CLI | 0.164.0 and 0.165.0-alpha.1 | 0.164.0, the latest stable release, was branched 7 commits before #56132 was merged, so it lacks the fix (GitHub's compare of the tag and the merge commit). 0.165.0-alpha.1 contains it. |

VS Code and Cursor are out of scope: their stop buttons cannot be driven from a script without their own UI automation.

## What each client is documented or coded to do

**Claude Code.** Its MCP documentation (`code.claude.com/docs/en/mcp`, read 25 Oct 2026) says the following.
- **Background tasks:** a call still running after two minutes (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`) "moves to a background task". "If you stop the task, Claude Code cancels the call and tells Claude that no result is coming. Stopping doesn't guarantee the server stops."
- **Time limits:** a per-server `timeout`, or `MCP_TOOL_TIMEOUT`, is "a hard wall-clock limit per tool call".
- **Disconnecting a stdio server:** "On macOS and Linux: Claude Code sends `SIGINT`, then `SIGTERM` if the process is still running 100 milliseconds later, then `SIGKILL` after a further 400 milliseconds."
- **Esc:** the page does not say what Esc does to an ordinary, unbackgrounded call.
- **Code:** the executable embeds the MCP SDK's request code, which sends `notifications/cancelled` when a request's abort signal fires or its timeout expires. It also starts some child processes with `detached: true` on non-Windows systems, which puts them in a new process group.

**Codex CLI.** The configuration reference documents a default per-tool timeout of 60 s (`tool_timeout_sec`) and says nothing about cancellation. Its code at `rust-v0.164.0` shows the rest:
- **Start-up:** each stdio MCP server is started in a new process group (`codex-rs/rmcp-client/src/stdio_server_launcher.rs`, `ProcessMode::NewGroup`).
- **Shutdown:** `terminate()` signals the whole group, then kills it if anything is still running after 2 s (`PROCESS_GROUP_TERM_GRACE_PERIOD`).
- **The fix in #56132** adds a guard (`CancelOnDrop`) that sends `notifications/cancelled` with the reason "tool call response future dropped" whenever a tool call's pending response is dropped.

## Method

- **Server.** `probe_server.py` is the recording server from `../clients/record_server.py` with two changes: the number of steps is set by `REC_STEPS`, and the child's process group is logged.
  - It logs every message it receives, stdin closing, and every SIGINT, SIGTERM and SIGHUP, with times.
  - Its one tool, `slow`, starts a bash child that writes a marker line every 0.5 s, and replies when the child exits.
  - It ignores cancels on purpose and only records them, so what each client sends stays visible.
  - It exits on stdin closing, SIGINT or SIGTERM.
  - The child stays in the server's process group, so a signal to the whole group reaches it, and a signal to the server's process alone does not.
- **Model: a local stand-in.** The client's handling of a stop does not depend on which model asked for the call. So in the main arm each client talks to a local stand-in for its model API, which costs nothing and makes every run identical.
  - **Claude Code:** an Anthropic Messages API stand-in, reached through `ANTHROPIC_BASE_URL`.
  - **Codex:** an OpenAI Responses API stand-in, reached through `model_providers.stub` with `base_url` and `wire_api = "responses"`.
  - **Script:** the first reply asks for the `slow` tool. For the background-stop path, the next reply asks for `TaskStop` on the task the client reports. Any later request gets a short closing text.
  - **Record:** the stand-in saves every request, which shows exactly what the client tells the model after the stop, and measures the token counts that price the paid arm.
- **Driving the clients.** Each client runs in its interactive terminal interface inside a pseudo-terminal, with a fresh configuration folder for every run. Telemetry, update checks and other non-essential traffic are switched off: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for Claude Code; `analytics.enabled = false`, `otel.metrics_exporter = "none"` and `check_for_update_on_startup = false` for Codex. The harness:
  1. types the prompt;
  2. waits until the server logs that the tool's child has started;
  3. applies the stop 1.2 s later;
  4. watches for 10 s.
- **Stop paths.**

| Path | Claude Code | Codex |
|---|---|---|
| **esc** | one Esc key press | one Esc key press |
| **timeout** | per-server `timeout` of 3,000 ms (the stop time is when it fires) | `tool_timeout_sec = 3` |
| **background stop** | `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=2000`, then the stand-in calls `TaskStop` on the task, as in #104714 | (not applicable) |
| **quit** | Ctrl+C twice (changed after the smoke runs to presses every 0.3 s until the client exits; see below) | the same |
| **sigterm** | SIGTERM to the client's process, as when a supervisor or a closing terminal ends it | the same |
| **control** | no stop; the tool finishes | the same |

- **Runs.** Three runs of each path, plus one control run, for each client version: 32 runs of Claude Code and 26 of Codex, in a seeded random order.
- **Measures,** all taken outside the client:
  - from the server's log:
    - whether a cancel for the call arrived, when, and its reason text;
    - whether stdin closed;
    - each signal the server received, and when;
    - how the server ended;
  - from the marker file: child markers more than 0.6 s after the stop, and whether the child was alive at the end;
  - from the stand-in's log: what the client sent the model after the stop;
  - from the terminal: what the person is shown, saved raw and with control codes removed.

## Harness details (fixed 25 Oct 2026, before the first run)

- **Shell.** Each client runs as the foreground job of a bash shell inside the pseudo-terminal, so the shell, not the client, holds the terminal session, as in a real terminal. Without the shell, the client's exit would hang up the terminal and signal its process group, which would be an artefact of the harness.
- **Executables.** The clients' native executables are run directly (`bin/claude.exe` in the Claude Code package; the `codex` binary in `@openai/codex-linux-x64`), not the npm launcher script, which for Codex only forwards SIGINT, SIGTERM and SIGHUP to the binary. Each batch's manifest records the paths and SHA-256 checksums.
- **Start.** The prompt is typed once the server has logged the client's `tools/list` request and the screen has been quiet for 1 s.
- **Tool length.** The tool's child writes 30 markers, 0.5 s apart (15 s), so a child that survives a stop is still writing when the watch ends.
- **Watch.** The watch lasts 10 s from the stop: from the key press or signal for Esc, quit and sigterm; from when the timeout fires (call time + 3 s) for the timeout; from when the stand-in asks for `TaskStop` for the background stop. The control is watched until the result is sent, plus 3 s.
- **Quit.** Ctrl+C twice, 0.3 s apart. If the client is still running 3.3 s after the first press, one more press each second, up to three; the record keeps every press.
- **Follow-up.** After the watch, if the client is still running, the harness types one more message ("What happened to the slow tool call?"). For Esc, a client sends nothing to the model until the person types again, so this is what makes the "model is told" measure visible.
- **Smoke runs.** Before the batch, one run per path on Claude Code 2.1.310 and Codex 0.164.0 makes sure the harness works (setup screens, keys, the stand-in's script). These runs are kept and reported but not counted, and any harness change made after them is listed in the write-up.

**Changed after the smoke runs (25 Oct, before the batch).** The smoke runs covered every path on Claude Code 2.1.310 and Codex 0.164.0, plus one control on each of the other two versions. The predictions above are unchanged.
- **Deferred tools (Claude Code).** Claude Code 2.1.310 does not put MCP tools or `TaskStop` in the model's tool list. It names them in a reminder and offers `ToolSearch`, which loads them. The stand-in now first calls `ToolSearch` with `select:<name>`, as a real model has to. The client's defaults are kept.
- **Quit (Claude Code).** In Claude Code, the first Ctrl+C interrupts the running call and the second shows "Press Ctrl-C again to exit". The planned extra presses, one per second, each re-armed that message and never exited, so the window is shorter than 1 s. Quit is now Ctrl+C every 0.3 s until the client exits, at most 6 presses; the record keeps every press.
- **Quit (Codex).** Codex answers Ctrl+C during a running task with a menu: "1. Cancel task" (stop the current task and stay in Codex), "2. Run in background" (exit Codex and leave the task running) and "3. Exit" ("Stop the current task and exit Codex"). Quit chooses 3, the option for a person who wants to stop and leave.
- **Tool approval (Codex).** Codex's default MCP approval mode asks before running a tool that has no read-only annotation, and with `approval_policy = "never"` it refused the call ("MCP tool call requires approval, but approval policy is never"). The probe server is pre-approved with `default_tools_approval_mode = "approve"`, the counterpart of Claude Code's `--allowedTools`.
- **The Codex daemon.** In both Codex versions, the interactive interface runs MCP servers inside a shared app-server daemon (feature `daemon_auto_start`, stable and on by default), which it starts in a new session. On first launch it installs the daemon by copying its whole package, about 430 MB, into `CODEX_HOME`. Each run gets that package pre-installed as hard links to the installed client's files, as a returning user would have it, so no copy is made. Two smoke runs stalled while the copy was being made. The daemon's own updater, which is on by default and checks for new releases online, is switched off in its `settings.json`. Its 60 s shutdown grace is left at the default. The `plugins` feature is switched off because it made the daemon `git fetch` `github.com/openai/plugins` at start-up.
- **Folders.** Run folders are under `~/client-probe-live` instead of `/tmp`, because Codex refused to create helper links under `/tmp` and showed a warning.
- **Start-up.** The harness waits up to 90 s for the client to list the server's tools. A run whose client never gets there is excluded and repeated once. It is excluded before any stop, so the repeat cannot bias the outcome. Both records are kept.
- **Exit code.** The shell now records the client's own exit code. Before the fix it recorded the exit code of `echo`.
- **Clean-up.** The kill pass is repeated until no process from the run is left. A killed marker loop can leave one last `sleep` running for up to 0.5 s. This changes nothing that is measured.
- **Exploratory, outside the predictions.** After the watch, if the client has exited but processes it started are still running, the harness keeps watching until the tool would have finished (15 s after it started, plus 6 s). It records the server's events and any requests to the stand-in. In a Codex smoke run killed with SIGTERM, the daemon sent the model the tool's result after the interface had gone.

**First batch attempt, stopped (25 Oct).** The first batch (`results/20261025T135755Z`) stopped at run 28 of 58. At that point the harness was polling the server's log on the Windows-mounted drive while the server wrote it, and the read failed with `ENODATA`. This is a harness fault, not a client result. Its 27 finished runs are kept and reported as a partial replication, but not counted. Before the batch was run again in full:
- logs are written on WSL's own file system and copied into the results after each run's clean-up;
- reads retry;
- a run that hits a harness error is excluded and repeated once, like one that never starts;
- the checksum file is written even if a batch stops.

**Second batch, stopped and resumed (25 Oct).** The second batch (`results/20261025T142258Z`) was stopped by hand after run 42, with 41 runs measured. C: had fallen to 1.7 GB free. Codex 0.165.0-alpha.1 names its daemon package by a content digest (`local-<digest>-<target>`), so it ignored the hard-linked pre-install, which works for 0.164.0, and copied its 430 MB package into every run's home. With your OK, the run homes were deleted; every run's logs and the clients' records had already been copied into the results. The batch was then resumed in the same order with these changes:
- each run's home is deleted once its records are copied;
- the batch stops if C: has less than 1.2 GB free, which leaves room for one 430 MB package copy;
- if a typed prompt has not reached the model within 4 s, Enter is pressed once more (in run 36 Claude Code left the prompt unsent);
- a run in which the tool never starts is also excluded and repeated once, since that too happens before any stop;
- the client's exit time is taken when the exit is detected; in Codex's quit runs it had been taken before the menu keys, up to 0.3 s early.

None of these touches a measure of what happens at or after the stop.

## Predictions

| Path | Claude Code 2.1.310 and 2.1.308 | Codex 0.164.0 | Codex 0.165.0-alpha.1 |
|---|---|---|---|
| **esc** | Cancel sent, medium confidence: I expect Esc to abort the request's signal, which makes the embedded SDK send the cancel. The model is told the call was interrupted. | **No cancel** (the stated reason for #56132). The turn ends as interrupted. | Cancel sent, reason "tool call response future dropped" |
| **timeout** | Cancel sent when the timeout fires; the model gets a timeout error | No cancel; the model gets a timeout error | Cancel sent (the timeout drops the pending response) |
| **background stop** | Cancel sent (the documentation says "Claude Code cancels the call"); `TaskStop` reports success | — | — |
| **quit** | No cancel. SIGINT to the server's process, then SIGTERM and SIGKILL only if it is still running. The server exits on SIGINT. **The child survives**, low confidence: the documentation says the server's process is signalled, but if Claude Code signals the whole group, the child dies. | No cancel. SIGTERM to the server's whole process group, so **the child is killed**. | As 0.164.0, possibly with a cancel first if the pending responses are dropped before shutdown (low confidence) |
| **sigterm** | As quit (low confidence: it depends on Claude Code's own SIGTERM handling) | As quit (low confidence) | As quit (low confidence) |
| **control** | The tool finishes, the result is sent, no cancel | Same | Same |

In every path, the child's work after the stop follows from what reached the server, since this server ignores cancels by design. It is reported for completeness.

**Not predicted:**
- the exact text each client shows the person;
- any difference between Claude Code 2.1.310 and 2.1.308.

## What would change the conclusions

- **If Claude Code sends no cancel on `TaskStop`,** the client half of #104714 is confirmed: the stop is reported as done while the server is never told. If it does send one, the 2-hour query in #104714 needs another explanation. Our real-server test found that this MongoDB server stops an aggregation within 0.3 s of a cancel (`../real/README.md`), so the question would move to the server or the deployment.
- **If Codex 0.165.0-alpha.1 sends no cancel on Esc,** the fix does not reach the interactive path.
- **If quitting Codex kills the child** while quitting Claude Code does not, the difference is the process-group design, which is worth reporting to both projects.

## The paid confirmation arm

The paid arm checks that the stand-in did not change the clients' behaviour. It repeats the **esc** path, 3 runs per client at the latest versions, with a real model in place of the stand-in. The prompt asks the model to call the `slow` tool, and the rest is unchanged.

**Models** (prices checked 25 Oct 2026 on the providers' own pricing pages):
- **Claude Code: Claude Haiku 5.5** (`claude-haiku-5-5`), for prompts up to 100,000 tokens: $0.10 per million input tokens, $0.125 per million for 5-minute cache writes, $0.01 per million for cache hits and $0.50 per million output tokens (`platform.claude.com/docs/en/about-claude/pricing`).
- **Codex: `gpt-5-mini`**: $0.25 per million input tokens, $0.025 per million cached input and $2.00 per million output tokens (`developers.openai.com/api/docs/pricing`).

**Cost.** The cost is computed from the token counts the stand-in measures in the free arm: each client's request sizes and the number of requests per run, priced as above with no cache discount, so it is an upper bound. The figure is written into `COST.md` before anything paid runs.

*Correction (25 Oct, after the free batch, before any paid run):* for Claude Haiku 5.5, a cache write ($0.125 or $0.20) costs more than base input ($0.10). So "no cache discount" is not an upper bound. COST.md prices the high and ceiling cases at the dearest input rate.

**Credentials.** The paid arm needs an Anthropic API key or a Claude subscription login, and an OpenAI API key or a ChatGPT login. You set these yourself; I do not see or handle them. With a subscription login the marginal cost is nil, but the runs count against the plan's usage limits.
