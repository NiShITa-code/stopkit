# Results format

The specification version is `1.0.0-rc1` (`src/stopkit/spec/version.json`). Every table and figure the toolkit produces is generated from the formats defined here. A number in a report traces back to a row, a row to a run record, and a run record to its raw evidence.

## 0. The specification files

The definitions every record, row and report uses live in `src/stopkit/spec/`, versioned together (`version.json`). `src/stopkit/spec/__init__.py` loads and checks them; a run refuses to start on a broken specification.

| File | Defines |
|---|---|
| `kinds.json` | The kinds of work P1–P7 and the four outcome families |
| `structures.json` | Item structures: where a piece of work sits relative to the agent. Stops and scenarios may name only these |
| `stops.json` | The stop register: each stop's class, mechanism, intended scope (as structures), where that scope comes from (documentation, with a link; mechanism; or definition), report kind and actor |
| `surfaces.json` | Each surface an observer lists: what it lists, how a listing is proven complete, the kinds of work it can show, and its known blind spots |
| `verdicts.json` | Readings, verdicts and their order, the words per family, the qualifier for an unchecked restart, "acted after the stop", the in-scope result, report truthfulness, the ground truth's readings per oracle, and the accuracy categories. Its worked examples are run as tests against the code |
| `scenarios/*.json` | The planted items: each item's structure, its paths (kind of work → surfaces) and how the ground truth reads each path |

## 1. The run record (`record.json`, format `stopkit-record/2`)

One record per run, written next to `blobs/` (every command's full output, gzip-compressed and named by SHA-256) and `SHA256SUMS`. A record has these sections.

| Section | Contents |
|---|---|
| Identity | `format`, `kit_version`, `spec_version`, `git_commit` (the framework's commit), `run_id`, `batch_id`, `kind` (`test` or `selftest`), `evidence_origin` (`framework` for a record the kit writes; any other value marks imported evidence) |
| `system` | `architecture` (for example `A1 Linux host`), `binding`, `binding_version`, `scenario`, `platform` (operating system, kernel, systemd, Python, WSL or not, boot ID; no host name or address), `configuration` (every non-default setting); for an agent from a config file, `scenario_data`: the scenario the file became, whole, so the run can be scored again where the file is not |
| `stop_spec` | The stop's entry in the stop register (`spec/stops.json`): `id`, `class`, `mechanism`, intended scope, `scope_source`, `report_kind`, `actor`; for a stop from a config file, `register_entry`: its entry as the file made it, which `stopkit recheck` and `stopkit report` register before scoring |
| `launch` | What the agent inherited when it started: ignored, blocked and caught signals (from `/proc/<pid>/status`), the environment's hash, resource limits, control group |
| `timing` | All on the monotonic clock: `stop_sent_mono` (the cut-off), `stop_returned_mono`, `stop_seconds`, `grace_s`, and each checkpoint's window relative to the cut-off |
| `claim` | What the stop reported, verbatim: `message`, `said_stopped`, the report kind (`explicit`, `apparent`, `none`), the scope it covers |
| `checkpoints` | Each fresh snapshot (before the stop, after it at each offset, after the restart action): every listing with its commands, exit codes and evidence hashes, the sentinels seen, and each item's reading with its reason |
| `items` | One entry per item × kind of work, with:<ul><li>the item's structure, and whether it is inside the stop's intended scope;</li><li>the surfaces read;</li><li>its readings, verdict and reason;</li><li>whether its restart was confirmed;</li><li>`acted_after`: effects issued after the cut-off plus the grace period (count, time of the last one), with those inside the grace period counted separately;</li><li>`ended_by_completion`;</li><li>`evidence`: references to the listings that support the verdict</li></ul> |
| `effects` | The observed effect events (time relative to the cut-off, item, channel) as the effect observer recorded them, with a reference to the raw log |
| `truth` | Bench runs only, from the sealed recorder, which the verdicts never read:<ul><li>`status` (`ok`, `failed`);</li><li>per item and kind: the truth reading at each checkpoint (present, absent or indefinite, by the oracle rules in `verdicts.json`), the truth `verdict` computed from them by the same verdict rules, `definite`, `exit_s`, `last_alive_s`, `fires_after`;</li><li>a reference to the raw truth log</li></ul> |
| `coverage` | The coverage manifest (`src/stopkit/score/coverage.py`):<ul><li>per surface the scenario reads: the observer, what it lists, how its listing is proven, whether it was proven at each checkpoint (and why not), and its known blind spots;</li><li>per kind of work: tested or not (and why not), the items and surfaces that read it, whether every one of those listings was proven at every checkpoint, and the implemented observers that could show it</li></ul> |
| `errors`, `notes`, `cleanup`, `clock` | Problems, clock drift, and what clean-up removed or left |

Records of format `stopkit-record/1` are written by `test`, which runs without the ground truth. They are scored, and rechecked, by `recheck`; their rows carry `spec_version` `null`, and the reports below are made from `stopkit-record/2` only.

## 2. Rows (`rows.jsonl`, format `stopkit-rows/1`)

`src/stopkit/report/results.py` turns each record into one row per run × item × kind of work. In `rows.jsonl` each line also carries `rows_format` (`stopkit-rows/1`); `format` is the format of the record the row came from. Each row carries:

| Group | Columns |
|---|---|
| Run | `run_id`, `batch`, `record_path`, `format`, `kit_version`, `spec_version`, `git_commit`, `evidence_origin` |
| System | `architecture`, `binding`, `binding_version`, `platform`, `configuration` |
| Stop | `stop_id`, `stop_class`, `stop_mechanism`, `stop_actor`, `scope_source`, `report_kind`, `report_message`, `said_stopped` |
| Timing | `stop_sent_s`, `stop_returned_s`, `stop_seconds`, `grace_s` |
| Item | `item`, `kind`, `family`, `structure`, `in_scope` |
| Outcome | `verdict`, `verdict_word`, `verdict_why`, `restart_confirmed` |
| Effects | `acted_after`, `acted_within_grace`, `last_effect_s`, `ended_by_completion` |
| Truth (bench) | `truth_status`, `truth_verdict`, `truth_definite`, `truth_exit_s`, `truth_last_alive_s`, `truth_fires_after` |
| Links | `evidence` |

`verdict_word` is the verdict as it reads for that kind of work (`verdicts.json`, words). Work in family 1 is `still active` or `ended`; a trigger (P4) is `armed`, `removed`, `armed, then gone` or `came back`. When no restart action was performed, an ended verdict carries its qualifier, for example `ended (re-activation not checked)`: nothing could be seen coming back.

An `evidence_origin` other than `framework` marks evidence imported from outside the toolkit; such rows must never be counted with toolkit results.

## 3. The reports (generated by `stopkit report`, and after every `check` batch)

`src/stopkit/report/reports.py` writes `REPORT.md` and `REPORT.html` (the same content; the HTML carries the timelines inline and follows the viewer's light or dark setting), `timelines/<run>.svg` and `<run>-zoom.svg`, `rows.jsonl` and `accuracy.json`. Each run folder also gets its own `report.md` (the run details) when it is written. Every number is counted from rows and records; a run's in-scope result is computed from its rows with the current rules, never copied from the record.

| Report | Built from |
|---|---|
| Shutdown reach matrix: stops × kinds of work × architectures. Each cell counts the paths by verdict word, split into in scope and out of scope, with how many acted after the stop, and the number of runs. A column gives each stop's in-scope results: `ended`, `ended late` (everything in scope ended, but not at once: some of it acted after the cut-off plus grace, or was still there at an early check), `continued`, `unknown` or `nothing` | `verdict_word`, `in_scope`, `acted_after`, grouped by `stop_id`, `kind`, `architecture` |
| Run details: per run, the stop's verbatim report, its truthfulness, the in-scope result, the remainder, how long the stop took, the truth's status, and links to the run's report, record and timeline | One run's rows and record |
| Accuracy against ground truth: readings (each path at each checkpoint: agree, false absent, false present, kit unknown, indefinite), verdicts (where the truth verdict is definite: agree, false ended, false active), effects (sink against truth), and every disagreement listed | `src/stopkit/score/accuracy.py` over the records' `items` and `truth` |
| Timelines: per run, on one axis from the cut-off: each process's life and each trigger fire as the truth recorded them, the last effect the sink received after the stop, the kit's reading at each checkpoint, and the cut-off, the stop's return and the restart action; a second panel shows the first seconds | `timing`, `checkpoints`, `items` and `truth` in the record |
| Coverage (section 5 of the report): what was tested, what was not and why, how many listings were proven per surface, each surface's blind spots, and the stops and architectures not run | The coverage manifests, the stop register and `src/stopkit/spec/surfaces.json` |
