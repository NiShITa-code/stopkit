# Experiment B screen on several VMs: the campaign coordinator

**Status (13 Oct).** Tested with fake ssh and tar workers (`tests/test_campaign_b.py`); its worker-side helper was run on a temporary repository in WSL. It has not yet been run against a VM. Before a counted campaign, the rules must allow it. The README and `run_expB.py` say each freeze gets one screen batch, and RULES-expB B.10 runs the grid sequentially in one order shuffled with a fixed seed. Both need to allow one registered screen batch per worker, each in its own seeded order.

`n8n/campaign_b.py` runs one registered Experiment B screen across several Google Cloud VMs, with one active trial per VM. Each worker VM runs exactly one local screen batch of `run_expB.py --freeze`, restricted to the conditions the campaign assigned to it. The coordinator runs on the operator's machine (Windows or WSL, Python 3.12, standard library only) and reaches each worker over ssh.

The coordinator never runs a trial and never judges validity. The runner and its frozen scorer do both, as on the laptop. The coordinator:

- registers the campaign: workers, condition lists, seed, freeze and source commit;
- checks each worker before a launch;
- starts the worker's runner in a detached tmux session, so an ssh drop cannot end it;
- copies and verifies the raw evidence;
- keeps the attempt ledger. The ledger holds the campaign-wide count of attempts per condition.

The same file is also the worker-side helper. For each probe, manifest or launch, ssh feeds the file to the VM's `/usr/bin/python3` on stdin, as root (`sudo -n`).

## Rules it enforces

- **One batch per worker.** The batch id is `<campaign id>-<worker>`, for example `cb1014-w3`. A replacement attempt is the runner's own `--resume` of that batch on the same worker. A new batch starts only on a worker with no batch folder and no attempts in the ledger. No condition is ever moved to another worker. A launch is refused if the runner would start any condition that is registered to a different worker.
- **At most two attempts per condition, counted over all workers** from the ledger, not from whichever files survived. Interrupted and unrecorded attempts count. Before a relaunch, the coordinator reads the runner's own dry run (`--dry-run --resume`). It refuses the relaunch if any attempt the runner would start is a third attempt, or if the attempt number disagrees with the ledger. A disagreement happens, for example, when a batch.json was restored from a snapshot and lost attempts.
- **The campaign file cannot change.** `register` records the SHA-256 of `campaign.json`. Every later command refuses if the file differs from that fingerprint, or if the ledger's hash chain is broken.
- **Frozen code only.** Registration requires two things. The freeze's `source_hashes` must equal what the runner committed at the campaign commit computes over that commit's tree. And the coordinator, runner and scorer files in the operator's checkout must equal the committed ones. Every command checks those four files again (`campaign_b.py`, `run_expB.py`, `analyze_b.py` and `clockwall_b.py`). So the freeze must be prepared **after** this coordinator and its tests are committed: its `source_hashes` must include `n8n/campaign_b.py`.
- **A launch is recorded before it is sent, and acknowledged after.** The acknowledgement is the start stamp written by the tmux session's own script. An ssh timeout, or a reply without a start stamp, leaves the worker *unresolved*. An unresolved worker cannot be launched or collected until `reconcile` finds its state.
- **Collected files are never changed.** Each collection is a new numbered folder, verified against the worker's own SHA-256 manifest. The coordinator writes no reports. It collects the runner's own: `run_expB.py` scores every attempt into `<batch folder>/derived/<trial>/`, beside a SHA-256 manifest of the attempt's raw files and reports (`manifest-sha256.json`), and batch.json records that manifest's hash.

*Accepted* in the ledger means the attempt's evidence is admitted. The runner accepted the attempt under the frozen scorer, and its copy and its bindings to this campaign verified. *Accepted* says nothing about whether the shutdown succeeded.

## Before registering

1. Commit the final code, including `n8n/campaign_b.py` and `tests/test_campaign_b.py`. Then prepare the freeze on the `linux-vm` profile and commit it. The commit that contains the freeze is the campaign commit.
2. Put that same commit, clean, at `/root/stopcheck` on every worker. Each worker also needs `n8n/n8n-test.env`, `/root/pilot-venv/bin/python`, tmux, and the validated installation (README, Supported configuration).
3. Record each worker's GCE instance id. It is the numeric id, not the name: `gcloud compute instances describe NAME --zone ZONE --format='value(id)'`, or `curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/id` on the VM.
4. Put each worker's host key in `~/.ssh/stopcheck_known_hosts`. The coordinator uses `BatchMode=yes` and `StrictHostKeyChecking=yes`: it never prompts and never accepts an unknown or changed host key. Its defaults are the key `~/.ssh/stopcheck_ed25519`, the user `stopcheck`, and passwordless `sudo -n` on the VM.

## The spec

The operator writes a small spec. `register` validates it and writes the campaign file from it.

```json
{
  "campaign_id": "cb1014",
  "source_commit": "<the 40-character campaign commit>",
  "workers": [
    {"name": "w1", "ip": "198.51.100.1", "instance_id": "1234567890123456789", "zone": "europe-west1-b"},
    {"name": "w2", "ip": "198.51.100.12", "instance_id": "2345678901234567890", "zone": "europe-west1-b"}
  ]
}
```

These keys are optional:

| Key | Default and meaning |
| --- | --- |
| `seed` | 20261012, the runner's default. |
| `freeze` | `n8n/FREEZE-expB.json`. |
| `assignment` | `{"w1": ["B7", "B0", ...], ...}`: every frozen condition exactly once. Without it, the coordinator assigns the longest expected conditions first, each to the least-loaded worker. |
| `expected_minutes` | Per condition. Defaults: 25 for the conditions with recovery follow-up reads (B1r, B7, B8, B9a, B9b, B9c, B10), 9 for the others. |
| `ssh` | `user`, `key` and `known_hosts`. |
| `remote` | Worker paths. Defaults: `/root/stopcheck`, `/root/pilot-venv/bin/python`, the launcher lock `/run/stopcheck-n8n.lock` and the log folder `/root/stopcheck-campaign`. |
| `notes` | Free text. |

With six workers and all nineteen conditions, the default assignment gives one worker two long conditions, because there are seven long conditions. The largest expected load is then 52 minutes. Startup checks and invalid attempts add to that.

Each worker's run order is the runner's own shuffle of its assigned list with the seed. Registration records that order. Every preflight checks it against the worker's own dry run.

## Commands

Run the coordinator from a checkout at the campaign commit, for example a separate worktree:

```bash
py -3.12 -B n8n/campaign_b.py register spec.json --dry-run      # print the campaign file; write nothing
py -3.12 -B n8n/campaign_b.py register spec.json                # writes ..\cloud-results\<campaign id>\
py -3.12 -B n8n/campaign_b.py preflight ..\cloud-results\cb1014
py -3.12 -B n8n/campaign_b.py launch ..\cloud-results\cb1014 [--worker w1 ...]
py -3.12 -B n8n/campaign_b.py status ..\cloud-results\cb1014 [--no-ssh]
py -3.12 -B n8n/campaign_b.py collect ..\cloud-results\cb1014 [--worker w1]
py -3.12 -B n8n/campaign_b.py reconcile ..\cloud-results\cb1014 --worker w1
```

The default results root is `C:\work\agent-shutdown-test\cloud-results`, the `cloud-results` folder beside the checkout. `--worker` repeats; without it, a command covers every worker. `--address w1=IP` reaches a worker at a new address for one command. The ledger records the address, and the instance id is still checked.

- **preflight** probes each worker read-only and records the result in the ledger. It refuses when any of these holds:
  - ssh fails;
  - the metadata server's instance id is not the registered one;
  - HEAD is not the campaign commit;
  - the working tree is not clean;
  - the freeze file's SHA-256 differs, or the freeze is not committed;
  - the kernel reads as WSL;
  - the launcher lock is held, or the batch lock is held;
  - a StopCheck program (`run_expA.py`, `run_expB.py`, `run_expC.py`, `harness_b.py`, `harness_b_ns.sh`) is running, or a campaign tmux session (`sc-...`) is open;
  - tmux is missing;
  - a `stopcheck-b-*` network namespace is left over;
  - the test user has processes or a crontab;
  - less than 10 GB is free on `n8n/results` or `/var/lib`;
  - the system Postgres, Redis or Valkey service is enabled or active;
  - `n8n-test.env` or the runner interpreter is missing;
  - another screen batch on the VM uses this freeze;
  - the next trial's work directory or host sentinel already exists;
  - the runner's dry run fails or gives another order.

  Several of these are checks the harness would otherwise fail at a trial's start, which uses up an attempt. Lock state is read from `/proc/locks`. The probe never takes the launcher lock, so a probe cannot make a starting trial's launcher refuse.
- **launch** repeats every preflight check, then the ledger checks. It refuses if the worker is running, unresolved, or stopped but not yet collected. It also refuses if batch.json changed since the last verified collect, or if the global limit would be exceeded. Otherwise it records `launch_request` and starts this command in tmux session `sc-<batch id>`:

  ```bash
  env STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json \
      --conditions <assigned> --seed <seed> --batch-id <batch id>
  ```

  For a relaunch, the arguments are `--resume n8n/results/XB-screen-<batch id>/batch.json` instead. The VM keeps the runner's output and exit status in `/root/stopcheck-campaign/<campaign>/<worker>-launch-<n>.{sh,start,log,exit}`, outside the repository and outside `n8n/results`.
- **status** is read-only. For each worker it shows the ledger state and what the worker shows now: activity, each condition's attempts, and what `--resume` would run. It then lists every condition's attempts from the ledger.
- **collect** refuses a worker that is running, unresolved or a different instance. Otherwise it copies the batch folder (with the runner's reports and export manifests), the worker's trial folders and its launch files with `sudo tar -cz` over ssh. It extracts them with Python's tarfile data filter, which refuses absolute paths, escapes and device files. Every file is checked against the SHA-256 manifest the worker computed just before the copy: the same set of relative paths, the same hashes, and nothing that is not a regular file. A failed check renames the folder `collect-NNN-FAILED`, records `collect_failed` and admits nothing. After a verified copy, each attempt is also checked against the runner's export, which was written when the attempt ended. The export manifest must have the SHA-256 recorded in batch.json, every raw file and report it lists must match, the raw folder must hold nothing it does not list, and the reports must come from the frozen scorer. An attempt the runner has not exported yet (it exports one on the next resume) is noted, not refused. Only then are the collect entry and the attempt records appended to the ledger. A worker that has not changed since its last collection is not copied again.
- **reconcile** is for use after a lost reply or a disconnect. It records the worker as *running* when its runner process is alive, and as *stopped* when nothing is active: no runner, no launcher lock, no campaign tmux session. In every other case it records nothing and says why. For example, a trial may still hold the lock after its runner was killed, or a session may be open without a runner. A stopped worker still needs `collect` before any relaunch.

The usual cycle per worker is `preflight`, `launch`, (`status`), `collect` and inspection of the raw records. When the runner stopped at an invalid trial, `launch` again resumes the batch, and the runner reruns that condition once.

## The campaign folder

```
cloud-results/<campaign id>/
  campaign.json  registration.json   the registered campaign and its fingerprint
  freeze/FREEZE-expB.json            the frozen manifest, byte for byte from the campaign commit
  ledger.jsonl  ledger.head          the append-only ledger and its last line
  <worker>/collect-NNN/              one verified collection: results/ (batch and trial folders as on the VM; the
                                     runner's reports are in results/XB-screen-<batch id>/derived/<trial>/),
                                     launch/ (runner logs), manifests/ (one SHA-256 list per folder), SHA256SUMS
  <worker>/collect-NNN-FAILED/       a collection that failed verification, kept for inspection
```

`sha256sum -c SHA256SUMS`, run inside a collection folder, rechecks every copied file.

Each ledger line is one JSON object. It carries its sequence number, the campaign fingerprint and the SHA-256 of the line before it. `ledger.head` names the last line, so an edited, reordered or removed line is detected. Nothing in the coordinator rewrites a line. When an attempt's recorded state changes, for example from `pending` to `unrecorded` after a resume, a new line is appended.

An attempt line holds these fields:

| Field | Content |
| --- | --- |
| `job` | `<campaign>-<condition>` |
| `worker`, `instance_id` | The worker, and the instance id read at collection. |
| `batch_id`, `trial`, `attempt`, `global_attempt`, `rerun_of` | The local batch, the trial id, the runner's attempt number, the campaign-wide attempt number, and the attempt this one replaces. |
| `started_utc`, `ended_utc`, `exit_code`, `status` | From batch.json. |
| `native_validity` | The runner's `scored_valid` and the scorer's verdict. |
| `runner_accepted`, `accepted`, `reasons`, `notes` | The runner's acceptance; the coordinator's admission; why the attempt is not admitted; non-blocking observations. |
| `raw_folder`, `raw_manifest_sha256`, `record_sha256` | The collected trial folder, the SHA-256 of its manifest file in `manifests/`, and of its record.json. |
| `export_manifest_sha256`, `derived_report`, `derived_by` | The runner's export manifest hash, the collected copy of its report folder, and the hashes of the scorer files that wrote the reports. |

An attempt the runner accepted is still not admitted if any binding check fails:

- its raw folder is missing, duplicated or changed after an earlier collection;
- its raw folder or reports differ from the runner's export, or the export manifest differs from the hash in batch.json, or the reports come from another scorer;
- record.json names another trial, condition, run kind, host profile, freeze or code;
- record.json carries no verified batch entry of this freeze;
- the runner recorded another instance id for the attempt;
- batch.json is not the registered batch: another id, run kind, freeze, seed, order, host profile or code.

## Limits

- No reassignment. If a worker VM is lost for good, its unfinished conditions stay unfinished. The coordinator has no way to move them, so the global limit can never be reset by changing workers.
- `collect` copies only a stopped worker. Evidence of a long batch is not backed up while the batch runs. A GCE disk survives a VM restart.
- Lock state is read from `/proc/locks` and matched by device and inode. That has been checked on WSL's kernel and tmpfs, not yet on a GCE VM.
- One trial per condition remains a descriptive, exploratory screen. Each condition also runs on a single worker, so worker identity and condition cannot be separated within this screen. Every attempt records its instance, so comparisons across conditions can name that limit.
