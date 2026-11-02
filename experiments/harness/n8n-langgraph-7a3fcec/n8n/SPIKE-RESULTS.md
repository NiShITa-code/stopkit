# Experiment B feasibility spike: results (10 Oct 2026; not a scored trial)

Run: `results/spike-20261010-053121` (report.json, spike.log and the per-process logs). Script: spike.py inside spike_ns.sh, a fresh network namespace with only loopback.

**Set-up that worked:**
- n8n 2.41.7, queue mode: one main and one worker, both running as `nobody`, with internal task runners.
- Postgres 16.15 on 5433 and Valkey 7.2.13 on 6380.
- Each process's task broker on its own port (main 5681, worker 5682); worker health on 5680. The default 5679 clashes when main and worker share a namespace.
- Readiness taken from `/healthz/readiness`, not `/healthz`: `/healthz` answers before migrations finish.
- Owner, login and API key were created with the throwaway values in n8n-test.env (git-ignored).
- The workflow was created and published through the public API. The webhook answered about 1.1 s after publish; registration is asynchronous.

## The four checks
| Check | Result |
|---|---|
| C1 memory | Pass. About 1.15 GB in total while an execution ran (main 405 MB, worker 295 MB, task runner 150 MB, Postgres about 200 MB); about 3 GB was free |
| C2 a running Code node can be seen and stopped | Pass. 10 pings at 1 s before the stop. `POST /api/v1/executions/{id}/stop` answered 200 `canceled` in 0.09 to 0.12 s. Then 0 pings after the stop in the last run, and 1 ping arriving at +0.052 s (before the reply) in the run before |
| C3 the recorder stays independent | Pass. The untouched control's largest gap was 1.0 s while Valkey and main were stopped |
| C4 queue (Valkey) | Pass. With the worker down, a webhook execution waited in Valkey (`bull:jobs:2`, `bull:jobs:wait`). Stopping it removed the job (keys gone), and the execution read `canceled` |

## Things the spike found that the design must state
1. **No sender stamps inside a Code node.** The task-runner sandbox has no `process` object (ReferenceError). Code-node and other n8n-node pings therefore have only the recorder's arrival time, the commit record. For n8n carriers M1 is scored by commit, which is the headline; the initiation version is not available.
2. **Postgres 16 is below n8n's supported range.** n8n 2.41.7 logs "outside the supported range and receives compatibility support only. Upgrade to Postgres 17 or newer". Either it is named as a deviation in every result, or Postgres 17 is installed, which is another download.
3. **n8n warns that running outside a container is deprecated.** The test runs n8n natively, because there is no Docker locally. Results concern a native install; the official Docker image could be tested on the cloud server.
4. **Internal task-runner mode is deprecated** (n8n's own warning). It is kept, as decided, and claims are limited to it.
5. **Observation (one run, not a finding): the queued execution was invisible in the execution list.** `GET /api/v1/executions?workflowId=...` returned only the earlier run, and `status=new` returned nothing, while job 2 was waiting in Valkey and `GET /api/v1/executions/2` existed. An operator listing executions might not see queued work. It is to be checked in the scored trials (status scoring) before anything is claimed.
6. The Python runner failed to start (no virtual environment). This is not used; JavaScript only.

---

# Feasibility rerun on the agreed set-up (10 Oct 2026; not a scored trial)

Run: `results/feasibility2-20261010-080435` (report.json, run.log, snapshot-090.json, snapshot-180.json, pings.jsonl, per-process logs). Script: spike2.py inside spike2_ns.sh. Set-up as DESIGN-expB section 10:
- Postgres 17.11 (PGDG) and Redis 7.0.15 (Ubuntu), each started by the harness inside the trial's network namespace, with its own data folder;
- n8n 2.41.7 in queue mode (main, one worker, internal task runners), running as `n8ntest` in a private mount namespace: the Windows drive and WSL's shared mounts are hidden, and /tmp is private.

Scope: a native install in WSL2 (Ubuntu 24.04), not a container deployment.

| Check | Result |
|---|---|
| S services, before and after | Pass. System Postgres (16 and 17), Redis and Valkey services disabled and inactive; both packaged clusters down; outside the namespace only the DNS resolvers listen (port 53) |
| K0 isolation of `n8ntest` | Pass inside the namespace: the Windows drive is not visible (`/mnt` is empty); `n8ntest` cannot write the planted script or `/root`, can write its own folder and can run `/usr/bin/python3`. **Outside the namespace** (where the cron daemon runs its jobs) the Windows drive stays mounted for every user: an earlier check found `/mnt/c/work/agent-` writable by `n8ntest` (the run's own probe tested `/mnt/c/Users`, which is not writable, so it read "False"; that probe was the wrong path) |
| K1 start-up and readiness | Pass. Main ready in 34.3 s, worker in 10.0 s |
| K2 back ends | Pass. n8n's database is the harness's Postgres 17.11 (data folder `/var/lib/stopcheck-n8n/feas2/pg`, 4 n8n connections); Redis 7.0.15 with n8n's clients connected |
| K3 Code-node cancellation | Pass. 12 pings before the stop; `POST /executions/2/stop` answered 200 in 0.098 s; 0 pings after the reply; status `canceled` from the first read (0.1 s) to the last (10.4 s) |
| K4 Execute Command children (as `n8ntest`) | Pass. Background and detached canaries both pinged (8 each in the first 8 s), both in the hidden-drive namespace |
| K5 cron line written by `n8ntest` | Pass. The line was installed; it fired 3 times (first ping 9.3 s after planting) and each ping came through the recorder's Unix socket. The locked password and `nologin` shell do not stop cron |
| K6 queued-job discovery | Pass. With the worker down, two executions (4 and 5) were queued: both jobs in Redis, **neither in the default list nor in the `status=new` or `status=waiting` lists** (second observation of this). Stopping 4: HTTP 200, `canceled`, its job removed, no pings after the worker returned. Left alone, 5 ran when the worker returned (26 pings) |
| K7 restart recovery | Pass. SIGKILL of the main's and worker's process groups left no `n8ntest` process. Main ready again in 21.4 s, worker in 11.1 s. The killed execution read `crashed` on return and at 120 s, never pinged again, and had no job left in Redis. A new execution after the restart ran |
| K8 snapshots | Pass. Taken at +90.01 s and +180.03 s after the stop (monotonic clock), with every section filled |
| K9 waiting execution | Pass. Before the stop it read `waiting` (API) with `waitTill` set (Postgres), so it was offloaded; it resumed and finished `success` |
| Clean-up | No leftover processes; `n8ntest`'s crontab removed |

**Things this run found that the design must state:**
1. **WSL's monotonic clock runs 8.3 % slow on this machine (found here, also present in Experiment A).** The kernel's tick length is 9167 µs instead of 10000 µs (read with `adjtimex`, read-only), so `CLOCK_MONOTONIC` advances 0.917 s per real second. `CLOCK_MONOTONIC_RAW` matches Windows' clock exactly (60 s streams to Windows: raw +0.00 %). Measured inside the kernel (common/clockguard.py), monotonic runs -83,333 ppm against raw, exactly what the tick and frequency settings predict. The -7.8 % 'idle' figure from the first Windows stream was timing noise in that stream. The wall clock is slow by the same rate, and Ubuntu's time service steps it forward by about 3 s every 32 s. Every Experiment A round 1 trial shows the same drift between its wall and monotonic stamps (median 7.8 %, range 5.7-9.7 %; 4-13 steps per trial). See "Clock" below.
2. **`stoppedAt` of a crashed execution is not the kill time.** Execution 6's `stoppedAt` is 31 ms before its first ping arrived, so it is an earlier event's time. n8n's stored times are not used as stop or kill times.
3. **Execute Command children leave the worker's process group.** The "background" child had its own process group and session (the shell's), the detached one another. A group kill of the worker reaches neither, so in n8n `bg` and `det` differ less than in Experiment A. They are scored separately as planned, and the difference is stated.
4. **Queued executions do not appear in the public API's lists** (two runs now). The four views are compared in the scored trials before anything is claimed.
5. **Memory:** about 1.38 GB for n8n's processes while an execution ran (main 782 MB, worker 482 MB, runner 120 MB), more than in the first spike.

**Clock (to be settled before any scored trial):**
- All durations in both experiments are measured on `CLOCK_MONOTONIC`. On this machine one monotonic second lasts 1.091 real seconds, and the systems under test run their own timers on the same slowed clock (sleeps, `setTimeout`, lease and stall checks). Wall-clock schedules (cron, `waitTill`) follow the stepped wall clock.
- Effects: (a) times measured in monotonic seconds are about 8 % short of real seconds for work that is not timer-driven (for example shutdown work); (b) conversions from wall to monotonic time through the nearest ping can be off by up to one step (about 3 s) where a step falls between that ping and the moment converted; (c) the waiting execution resumed 3.3 s after its `waitTill`, as expected when its timer runs 8 % slow.
- Round 1 stays as recorded. Its records can be rescored after the fact for sensitivity (which deadline rows change if durations are scaled by 1.091, and which wall-to-monotonic conversions straddle a step) without changing any frozen file.

**Clock: settled on 10 Oct.** Guard built (common/clockguard.py, its tests, RULES-expA.md R6.13); the fix (common/set_tick.py --apply) is run by the fellow. Experiment B uses the same guard.

---

# Setup checks for RULES-expB draft 2 (11 Oct 2026; unscored, excluded from every result)

- **Runs:** `results/feasibility3-20261011-044216` (first) and `results/feasibility3-20261011-044742` (second, after one fix).
- **Script:** feasibility3.py inside feasibility3_ns.sh. It uses a named network namespace, because systemd's `NetworkNamespacePath=` needs a path.

| Check | Result (second run; the first agreed except where noted) |
|---|---|
| F1 isolation of the units | Pass. Every process of `n8n-main-f3` and `n8n-worker-f3-1` runs as `n8ntest`, in the trial's network namespace and a private mount namespace, with `/mnt/c` not visible. The properties read back as pinned: KillMode control-group, TimeoutStopSec 40 s, Restart no, User n8ntest, NetworkNamespacePath, TemporaryFileSystem /mnt, PrivateTmp, the spool BindPaths |
| F1 cgroup membership | The background, `setsid`-detached and SIGTERM-ignoring canaries started by Execute Command are all in the worker unit's cgroup. The detached one has its own process group and session |
| F1 `systemctl stop` of the worker unit | SIGTERM reached the whole cgroup at +0.06 s, and the background and detached canaries' last pings came before the stop. The worker process exited at +2.5 s (+1.9 s in the first run), which started the unit's "final SIGTERM" stage. SIGKILL followed 40 s after that, at +42.3 s (+41 s in the first run). That ended the SIGTERM-ignoring canary (last ping +42.1 s). `systemctl stop` returned at +42.3 s. The private cron unit stayed active, and its job kept pinging |
| F1 `systemctl kill -s SIGKILL` of the worker unit | It returned in 0.015 s, and every canary in the unit ended at once. The cron unit's processes were untouched |
| F2 private cron daemon | Pass. The cron-launched canary reported uid 1000 (`n8ntest`), the trial's network namespace, a mount namespace that is not the host's, `/mnt/c` not visible, and the cgroup `stopcheck-cron-f3.service`. The private daemon sees an empty `/etc/crontab` and `/etc/cron.d`. The crontab line is in the private spool only, never in the host's. The cron journal shows the PAM session for `n8ntest` and the command |
| F3 AI Agent | Pass in the second run. n8n's AI Agent (3.1) with the chat-model node (1.3, Responses API off) pointed at stub_model.py, and the HTTP Request node in its tool form (`n8n-nodes-base.httpRequestTool` 4.2). Two runs were identical: 15 tool calls, 15 recorder pings carrying the execution id, 16 model requests (all `/v1/chat/completions`, none streamed), and status `success`. The trial namespace has no route out, and `api.openai.com` does not resolve |
| Clean-up | No `n8ntest` process, no unit, and no host crontab line left |

**Found in the first run (a setup error, fixed):** the older tool node `@n8n/n8n-nodes-langchain.toolHttpRequest` fails in 2.41.7's agent ("has a supplyData method but no execute method"). All 15 tool calls failed, the agent carried on because the stand-in model ignores tool errors, and **the execution still ended `success`**. A setup observation, not a result: a `success` status does not show that an agent's tool calls worked.

**Carried into the rules (draft 3):**
- the systemd stop timing;
- the private cron daemon's settings (empty `/etc/crontab` and `/etc/cron.d` bound read-only, its own pidfile bound onto `/run/crond.pid`);
- the AI Agent node types;
- the named network namespace;
- process cgroups read from `/proc`, since `ip netns exec` remounts `/sys`.
