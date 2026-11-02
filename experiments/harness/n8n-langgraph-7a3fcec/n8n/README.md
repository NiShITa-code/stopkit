# StopCheck n8n shutdown trials

Experiment B tests operator procedures on n8n 2.41.7 queue mode in the project's native WSL2 deployment. It plants identifiable test work, observes outside the stopped platform and reports both leftover work and missing evidence. This is a research harness with a supported configuration; independent operator installation and a reusable adapter interface remain follow-on work.

[RULES-expB.md](RULES-expB.md) defines validity and scoring. [DESIGN-expB.md](DESIGN-expB.md) explains the deployment and follow-on experiments. Neither a development checkpoint nor passing offline checks constitutes the prospective research freeze.

## Supported configuration

Use the existing disposable Ubuntu 24.04 WSL deployment with systemd, n8n 2.41.7, Node.js 24.21.0, Postgres 17.11, Redis 7.0.15 and the dedicated `n8ntest` account. The main, worker and private cron daemon have separate units. The observer and stand-in job service run outside n8n's units.

Workload A and unrelated B share a worker with concurrency 10. Internal runner concurrency is explicitly set to 16 so the Code carriers can start; this is part of the tested configuration. Worker-level and global stops therefore reach B. The queue backlog is harness-submitted in every condition; child executions, processes, schedule and outside calls retain their distinct provenance. The stand-in has no cancel API and does not demonstrate real provider cancellation.

Current records use schema 7. Records written before the 13 Oct merge already said schema 6, without host checks and with the earlier cgroup reader, so the scorer requires that evidence only from schema 7: a schema 7 record without both host checks is void. A schema 7 record is also void without usable guard readings at start and end: a boot id and integer PID 1 start ticks, OOM-kill count and network-namespace inode. `common/guards.py` alone reads a missing reading as unchanged. Full trials use a common 1,950 s long-canary cap and 2,070 s task-runner timeout across all conditions, set by B1r's planned stop. B1r stops 180 s after the agent fires, not 75 s: its preparatory main restart took 33.1 s to stop main and more than 64.5 s to readiness in the 13 Oct pilot. The stop time is fixed, and a restart not verified ready by its deadline (15 s before the stop) withholds the stop and voids the trial. Because B1 and B1r stop at different workload ages, their comparison alone cannot attribute a difference to the restart. The full watch remains 180 s and the restart probe 60 s; probe preparation shares a 660 s deadline and every snapshot has a 60 s budget. A snapshot that overruns its budget during the pre-stop read, the watch or a recovery read ends the trial with an error, so the record is void. In the restart probe, an overrun (or any other probe error) is recorded as a censored probe with its error, and the scorer reports the restart outcomes as not tested. A completed probe whose last read crossed a long carrier's earliest possible natural end is censored too; its restart outcomes are unknown.

A quiet Code/AI loop is M2 ended by activity, as RULES-expA R6.3: no ping in the last 5 s with working observation coverage. Whether the worker's computation boundary closed is reported beside M2 as `computation_boundary` (closed or unresolved, with its evidence), never inside it. Closed needs a stopped worker unit whose control group a complete read finds empty, or the designed final iteration with successful terminal records. The unit's processes are read from `/proc/*/cgroup`, because `ip netns exec` remounts `/sys` without the cgroup hierarchy. The five events' `work_ceased` counts an in-worker loop as ceased only when its boundary is closed; otherwise it is unresolved.

"No false all-clear" and M2 "ended" are not proof that everything stopped: they rest on the platform's claims and on quiet canaries over a bounded watch. The per-trial operator verdict `verified_stop` answers that separately. It is `stopped` only when every declared carrier has independent ending evidence:
- in-worker work: the closed computation boundary;
- OS processes: gone from a successful process read;
- schedules, the cron line and the persisted wait: disabled and not firing;
- authority: refused at the issuer;
- outside obligations: resolved in the outside service's log.

It is `not stopped` when any carrier is running, armed or authorised, and `unresolved` otherwise. The summary prints it under the false-all-clear headline.

The prospective across-trial guard also rejects wall/monotonic discontinuities, using the paired recorder stamps. Keep the test host and VM running throughout a trial. A passing monotonic/raw rate check alone does not validate wall-clock schedule or timer evidence. The scorer recomputes this check from the stamps in `pings.jsonl` with the harness's own rule (`clockwall_b.py`) and reports both verdicts. For a schema 7 record a failed or impossible recomputation voids the trial; for an older record it is a labelled post hoc diagnostic only.

At its start and at its end every trial checks, read-only, the disk floor and that the system's own `postgresql`, `redis-server` and `valkey-server` services are disabled and inactive. A failure at the start refuses the trial before any setup; a failure at the end voids it.

The host is declared with `STOPCHECK_HOST_PROFILE` and recorded in every record (`host_profile`, and the record's `scope`). `wsl`, the default, is the laptop deployment and is unchanged: the disk floor is `common/guards.py` `disk_ok` (10 GB free on the Windows C: drive) and the isolation check looks for `/mnt/c/Windows`. `linux-vm` is a plain Linux VM: the floor is 10 GB free on each of the filesystems holding `n8n/results` and `/var/lib`, and the isolation check uses a sentinel the harness places at `/mnt/stopcheck-sentinel-<trial>` on the host, which the host must see and the units and canaries must not. On a host without `/mnt/c` an undeclared (wsl) trial is refused by the disk floor. The two profiles are separate configurations; do not pool their trials. The freeze records the declared profile, and its validation pilots, its screen and every trial a batch accepts must declare the same, so a laptop pilot can never validate a cloud freeze. On `linux-vm` the runner reads each worker's GCE identity before every trial from the metadata server, outside the trial's network namespace, which has no route to it. The identity is the instance id and name, zone, machine type, CPU platform, image, and the preemptible and onHostMaintenance scheduling. The runner records it in the trial's `batch.json` entry, and the harness copies it into the record as `host_identity`. Workers need not be identical. A missing identity is recorded as missing and never refuses a trial. `sudo` drops the variable unless it is given on the command line:

```bash
sudo STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --pilot --conditions B0
```

Paths can be supplied through `STOPCHECK_PY`, `STOPCHECK_SYSPY`, `STOPCHECK_NODE`, `STOPCHECK_N8N` and `STOPCHECK_PGBIN`. The launcher runs the harness with `STOPCHECK_PY` (or `STOPCHECK_PYTHON`, which must then be the same interpreter; the harness refuses a different one). The current defaults match this project's deployment; these variables do not install missing dependencies or provision a new machine. Trials require root inside WSL for namespaces, transient systemd units and the private spool.

Run trials sequentially. The dedicated-user cleanup and shared infrastructure make overlapping campaigns unsafe. The launcher holds a lock and refuses an existing namespace instead of deleting another run's namespace.

## Offline checks

From the `stopcheck` directory in Ubuntu, run the focused suites without starting services:

```bash
python3 -B -m unittest tests.test_analyze_b tests.test_harness_b tests.test_run_expB tests.test_launch_b tests.test_clockwall_b
python3 -B n8n/mutate_b.py
python3 -B n8n/run_expB.py --dry-run --pilot
```

The mutation check targets the documented corrected behaviors of the scorer, the runner, the harness, the planting, the reproduction script and the shared wall/monotonic rule. On Windows the mutants that need Linux are reported as not run; `python3 -B n8n/mutate_b.py --harness-only` runs just those under Linux. It is not evidence that every possible bug is covered. Synthetic fixtures validate calculations; the full-length pilot must validate live readiness, planting, isolation and cleanup.

## Unscored validation

Use the runner from the `stopcheck` directory inside the existing WSL deployment:

```bash
/root/pilot-venv/bin/python -B n8n/run_expB.py --pilot
```

The default full-length pilot is B0, PC, B1 and B1r. Select a narrower pilot while diagnosing setup:

```bash
/root/pilot-venv/bin/python -B n8n/run_expB.py --pilot --conditions B0
```

FAST plumbing trials shrink timing and remain smoke runs:

```bash
STOPCHECK_FAST=1 bash n8n/_preflight.sh B0 PC
```

FAST timing refuses B1r and B9c: their restarts need n8n's own shutdown and start-up time, which FAST does not shorten. The runner's FAST pilot (`--pilot --fast`) is therefore B0, PC and B1, and naming B1r or B9c with `--fast` is refused before any trial; the harness refuses them as well.

With `STOPCHECK_FAST=0` the preflight runs full-length pilots instead. It never runs a screen and ignores any freeze or batch setting.

FAST may not contain a pre-stop minute schedule firing. Such a carrier is untested; a nonzero exit is meaningful and must not be hidden by a log pipeline. The harness uses exit 2 for error/void and exit 3 for incomplete carrier testing. Preserve those statuses and inspect raw evidence before rerunning. The runner stops a batch at the first trial that exits nonzero or that the scorer calls invalid. Ctrl-C, SIGTERM or a hangup (a closed terminal or SSH session) asks the running trial to clean up and waits for it, then records the interruption in `batch.json`. Under `nohup`, hangups stay ignored.

Do not count pilot or smoke runs in the later screen. Inspect acceptance/job IDs, actual intervention timing, pre-stop evidence, queue ownership, B activity, readiness and cleanup. A failed check needs a repair and a new validation trial.

## Prospective screen and freeze

After the pilot, write the final operator predictions at `predictions/n8n-expB-cells.json`, relative to the `stopcheck` directory. Predictions never feed scoring. The JSON requires:

- `schema_version: 1`, a nonempty `prior_exposure` disclosure and a `configuration` object with the expected effective settings. It must give a non-null value for each B.0 key: `n8n`, `node`, `postgres`, `redis`, `package_lock_sha256`, `EXECUTIONS_MODE`, `N8N_RUNNERS_MODE`, `N8N_RUNNERS_MAX_CONCURRENCY`, `N8N_RUNNERS_TASK_TIMEOUT`, `worker_concurrency`, `NODES_EXCLUDE`, `N8N_GRACEFUL_SHUTDOWN_TIMEOUT`, `N8N_LOG_LEVEL`, `queue_lock_s`, `lock_renew_s`, `stalled_interval_s` and `queue_recovery_interval`.
- A `conditions` object keyed by every selected condition; each entry has nonempty `prediction`, `basis` and `refutation` strings.
- Each condition's `carriers` object contains all 16 declared carriers exactly once. Each cell has `predicted_state`, a provenance `label` from P/C/O/S/M, and nonempty `basis` and `refutation` strings.

A key repeated anywhere in the file is refused. Supported predicted states are `running`, `armed`, `authorised`, `ended`, `unknown`, `not_planted` and `unpredicted`. Use `unpredicted` to declare an exploratory cell rather than inventing an outcome. Disclose feasibility, smoke and pilot exposure, and keep a refuting observation for every substantive prediction.

Use `--freeze-only` to validate predictions and the four full-length validation pilots of B.10 (B0, PC, B1 and B1r) and write a source manifest that also records the declared host profile. Each pilot must:
- be valid by the scorer;
- carry the exact source fingerprints the freeze binds;
- declare the freeze's host profile;
- have a restart probe that made its reads with every restarted unit ready and every probe control passing (B.9).

B1r's must also show its preparatory main restart verified ready, with a changed process identity, by its deadline and before its stop. Replace the four example pilot-directory names below with their actual result directories, and run the freeze on the host profile the pilots ran on. Inspect the manifest and deliberately commit the final rules, predictions, code, tests and freeze record before running a screen. Freeze creation does not commit or start trials; it must not invent predictions.

```bash
sudo STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze-only --validation \
    n8n/results/B0-FULL-PILOT n8n/results/PC-FULL-PILOT n8n/results/B1-FULL-PILOT n8n/results/B1r-FULL-PILOT
sudo STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json
```

The scored runner rejects dirty source, file/configuration mismatches, incomplete predictions, invalid validation records, a host declaring another profile than the freeze, and failing required checks. The manifest and predictions must be committed and unchanged; a git-ignored location such as `n8n/results/` is refused. Runtime checks remain necessary even when the file manifest agrees. A direct harness call defaults to pilot or smoke. A screen-labelled trial also needs the runner's `batch.json` listing that trial as pending, so it cannot bypass the research freeze by receiving a scored label.

Each freeze gets one screen batch per machine, in `n8n/results/XB-screen-<batch-id>/batch.json`; a campaign over several machines registers one batch per worker with `n8n/campaign_b.py` (see `CAMPAIGN-B.md`), each with its assigned conditions and its own seeded order. A batch id uses letters, digits and hyphens, short enough for every trial id to fit the harness's 64-character limit. After a stopped batch, inspect the raw record, then continue in the recorded order with the same code and freeze:

```bash
/root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json --resume n8n/results/XB-screen-<batch-id>/batch.json
```

Conditions already accepted are skipped; only the scorer's validity verdict accepts a trial, and a trial that ended before the runner stored that verdict is judged when the batch resumes. The failed condition is rerun once under a new trial id, recorded with `attempt: 2` and `rerun_of`. If the replacement also fails, `batch.json` lists the condition as incomplete (B.10, as R6.10). Every started attempt counts, including an interrupted one or one whose exit the runner never recorded. Before each trial the runner refuses to start, and records no attempt, while another StopCheck trial still holds the launcher lock (`STOPCHECK_LOCK_FILE`, default `/run/stopcheck-n8n.lock`), for example one left running by a runner that was killed. `--resume` also works for pilot batches with `--pilot`.

One trial per condition is a descriptive exploratory screen. It identifies cases for declared repetition and matched remedy comparisons; it does not establish failure rates or cross-platform generality. Bare-process, regular-mode and long-tail recovery comparisons are not part of the current runner's supported screen.

## Results and interpretation

New trials write raw records and observer/service logs under `n8n/results/`. Each record retains workload/timing limits, source identity, run kind, actual stop evidence and carrier validity. Preserve raw inputs when rescoring. After every attempt the runner scores the raw folder into `<batch folder>/derived/<trial>/` and writes a `manifest-sha256.json` there. This covers accepted, invalid and interrupted attempts, and attempts without a record. The manifest gives the SHA-256 and size of every raw file and of the two derived reports, and `batch.json` references it with its own hash. Raw files are only read, and an attempt that ended before its export is exported on `--resume`. `analyze_b.py` writes derived `scored_b.jsonl` and `summary_b.md` to the selected trial directory:

```bash
python3 -B n8n/analyze_b.py n8n/results/TRIAL-DIRECTORY
```

Use `reproduce_b.py` to rebuild and check Experiment B outputs from raw records. A report reproduces only with the scorer recorded in the trial, and for a screen with the frozen scorer. The replay is source-matched only when every file of the record's source manifest, and for a screen the freeze's, matches this checkout, `analyze_b.py` and its `clockwall_b.py` included. A rescoring with any other sources is post hoc. Write it to a separate directory with `posthoc` in its name and check that directory:

```bash
python3 -B n8n/analyze_b.py --out n8n/results/TRIAL-DIRECTORY-posthoc n8n/results/TRIAL-DIRECTORY
python3 -B n8n/reproduce_b.py n8n/results/TRIAL-DIRECTORY --reports n8n/results/TRIAL-DIRECTORY-posthoc
```

Experiment A's reproduction script and historical inputs remain separate.

Planted node ids are kept within the 36 characters of n8n's trigger-status column. The longer webhook ids used before 13 Oct made n8n's publication bookkeeping fail and retry in every pilot (`QueryFailedError ... character varying(36)` in `main.log`); see SOURCE-FACTS-2.41.7.md.

Read acceptance, platform state, cessation, effects and evidence sufficiency separately. HTTP 404 means record absence. Missing reads stay unknown. A natural endpoint is not shutdown success, continued activity is not resumption, and a job's eventual completion does not erase an effect it committed earlier. Reports only cover planted work and observed boundaries.

The next experiment is real-service cancellation calibration with Temporal, under separately frozen rules. Additional controls and portability tests follow the n8n screen; they are not implied by its results.

Treat the button screen as diagnostic calibration. The main operator-tool comparison needs a competent supported runbook, per-target positive controls, structured versus detached creation paths, and ordinary-work reproductions. The [current run plan](../../research-redesign/RUN-PLAN-2026-10-13.md) lists those cases and records invalid validation attempts. Four current-source valid validation pilots, B0, PC, B1 and B1r, on the freeze's host profile, are still required before a screen.
