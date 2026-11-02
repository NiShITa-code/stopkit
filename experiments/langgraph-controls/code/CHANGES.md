# What changed from the code of the 9 October trials

This folder holds the code of the LangGraph trials of 9 October: the 13 files that their runner
(`langgraph/run_expA.py`, list `FROZEN`) bound by SHA-256 before the first trial, taken from commit `cd26851` of the
test repository. At that commit, every file a trial loads has the SHA-256 that each of the 33 trials recorded in
`trials.jsonl` (field `code.sha256`), and the runner, the predictions and the trial code have the SHA-256 that the
trials' `FREEZE.json` records. The frozen code in `experiments/harness/n8n-langgraph-7a3fcec/langgraph/` holds a later revision
of this code, for the later LangGraph batches: it adds the clock guard of rule R6.13 and a sender's time stamp on
every ping, and its runner refuses to rerun the 9 October batch.

Four files are changed, so that the code runs on a plain Ubuntu 24.04 machine as well as under WSL 2. Under WSL each
change behaves as the original line did, except that one log line names `/mnt/c` instead of `C:`. No change touches
what a trial does or how it is scored.

| File | What changed | Why |
|---|---|---|
| `langgraph/run_trial_ns.sh` | `P`, the harness's folder, is the script's own folder instead of a fixed path to one machine's copy of the code; one usage line added | That path exists only on the machine that ran the trials |
| `common/guards.py` | `c_drive_free_gb()` reads the free space of `/mnt/c` where it exists, as before, and otherwise of the disk that holds this code; `STOPCHECK_DISK` names another folder. Each trial records the value under the same name, `c_free_gb` | Without `/mnt/c` it returned nothing, and the harness then ends the batch before its first trial, as if the disk were below its 10 GB floor |
| `langgraph/run_expA.py` | The same free-space rule for the runner's own check, whose log line names the folder it read instead of `C:`; the stop file `/root/expA.STOP` can be moved with `EXPA_STOPFILE`; two docstring lines say so | `os.statvfs("/mnt/c")` fails where there is no Windows drive |
| `langgraph/harness.py` | Where `/mnt/c/Windows/System32/tasklist.exe` does not exist, the Windows process list is not read: `windows_start` and `windows_end` hold `skipped` with the reason, and `windows_changed` is `null` | A Linux machine has no Windows side to list; the scorer does not read these fields |

Besides this file, three files are new: `requirements.txt` and `constraints.txt`, the Python environment that the
trials ran, and `.gitignore`, which keeps the results of your own runs out of git. With the first two,
`pip freeze --all` gives the SHA-256 that the trials' `run_meta.json` records for every trial (`721b3c3bc63a8fd9`,
the first 16 hex digits); `experiments/setup/langgraph-controls.sh` checks it.

Two files of the commit differ from the trials' `FREEZE.json`, because the commit was made after the trials:
`langgraph/RULES-expA.md` adds the dated addendum written after them, and `langgraph/analyze_x.py` is the scorer that
addendum describes, the one that wrote the trials' `scored_x.jsonl` and `summary_x.md` (kept in the frozen code as
`langgraph/archive/analyze_x_rev5.py`). The trials recorded the scorer file as it was while they ran.

## SHA-256 of each file: at commit cd26851, and here

The commit's `langgraph/run_trial_ns.sh` held a home-folder path in its usage comment and its `P=` line; here that path
is replaced by one of the same length (experiments/data/MANIFEST.json, substitution `home-path`), so the commit's digest of that
file is not listed, and the diff below shows the replaced path.

```text
f5d9f4e213673f100b08f9ad22767b797339083de497610dd4a74d46555e23fb  common/guards.py (commit)
3046c2b9df33cc24d2fb13cd9787628773599ba4948f2d1f578a3bf885b9ea97  common/guards.py (here)
a4ea00eb68e60c825340152d75817584820494b0a61af8eba69721de307497cd  common/mocks.py
0889b362b842bd6d413a81ed00d4b5ce3670479059f95a20cb5328adb1242990  langgraph/RULES-expA.md
f28e692ce6a1768d7a33af9924907b0327aaa7886b6853bea3d373a73d2baedd  langgraph/analyze_x.py
6e9ab7e7c75fd33d5134bf542a69d325efc49080efeec5cba2a94de00d0e0671  langgraph/app/agent_graphs.py
65fdde8ededdd5ac21306e00b9f9a7293c0ffe3daf1b5f60d8f047ec58fbb119  langgraph/app/langgraph.json
cad5c599ecb8901af5a59f8d9e341be88cd6f1b4e7ad02ee7b291d44043742fe  langgraph/canary.py
cf2b2b58a9043b75210fe83b2ca9422850241af625ff9eab64361041f0816e97  langgraph/harness.py (commit)
62cb0be261d8f9d0716476b58ed0ea712ff422267fd002d1f33a38ee46d98ab7  langgraph/harness.py (here)
4ba8c68378d4b87fcea38c291f6eac01c6215bbb29bbac5b34749923e77ecb05  langgraph/recorder.py
5c7c4b970042f25f3343f7a341892535da899102b0aa5837ce45ee606c5af962  langgraph/run_expA.py (commit)
eb168b135e43a88753157ef1bac008ec138c5b4851cbdb91cdd6495a0ffc3f75  langgraph/run_expA.py (here)
c6a104cc5cd261b696c6fade6854cfb5695fc0438c4f16e902a0f988eaa5a1d6  langgraph/run_trial_ns.sh (here)
c0adced638d262800796a975cf3840b217018b28c7cb9d6175696c1dabdc12c3  predictions/langgraph-expA-DRAFT.md
2a0b37ba28a502e001d0b507dea81a72dadee611f8a42ca029ab6008a6c59500  predictions/langgraph-expA-cells.json
```

To check the digests of the files here, from the top of the repository:

```bash
cd experiments/langgraph-controls/code && sha256sum common/guards.py langgraph/harness.py langgraph/run_expA.py \
    langgraph/run_trial_ns.sh
```

## The diff

```diff
--- a/common/guards.py (commit cd26851)
+++ b/common/guards.py (this folder)
@@ -51,9 +51,15 @@
     return None
 
 
-def c_drive_free_gb(path="/mnt/c"):
+# WSL: the Windows C: drive. On a machine without /mnt/c (a Linux VM): the disk that holds this code,
+# or the folder named by STOPCHECK_DISK. The record keeps the field name c_free_gb either way.
+DISK_PATH = os.environ.get("STOPCHECK_DISK", "/mnt/c" if os.path.isdir("/mnt/c")
+                           else os.path.dirname(os.path.abspath(__file__)))
+
+
+def c_drive_free_gb(path=None):
     try:
-        st = os.statvfs(path)
+        st = os.statvfs(path or DISK_PATH)
         return round(st.f_bavail * st.f_frsize / 1e9, 2)
     except OSError:
         return None
--- a/langgraph/harness.py (commit cd26851)
+++ b/langgraph/harness.py (this folder)
@@ -276,6 +276,8 @@
 def windows_procs():
     """Windows process image-name counts (tasklist.exe through interop). A process the agent
     started on the Windows side would not show in the Linux process table [F23]."""
+    if not os.path.exists(TASKLIST):  # no Windows host (a Linux VM): the listing is skipped, not faked
+        return {"t": now(), "skipped": f"no Windows host: {TASKLIST} not found"}
     try:
         r = subprocess.run([TASKLIST, "/FO", "CSV", "/NH"], capture_output=True, timeout=30)
         counts = {}
--- a/langgraph/run_expA.py (commit cd26851)
+++ b/langgraph/run_expA.py (this folder)
@@ -15,6 +15,8 @@
 - Before the first trial a freeze record is written: sha256 of the rules, the predictions and every code
   file, with the time, plus copies of the prediction files. Nothing in them may change after it.
 - Safety: stops before a trial if C: has under 10 GB free, or if /root/expA.STOP exists.
+  On a machine without /mnt/c (a Linux VM) the free space is that of the disk holding this folder;
+  STOPCHECK_DISK and EXPA_STOPFILE set another folder to check and another stop file.
 """
 import argparse
 import datetime
@@ -30,7 +32,8 @@
 HERE = os.path.dirname(os.path.abspath(__file__))
 ROOT = os.path.dirname(HERE)
 OUT = os.path.join(HERE, "results", "expA")
-STOPFILE = "/root/expA.STOP"
+STOPFILE = os.environ.get("EXPA_STOPFILE", "/root/expA.STOP")
+DISK = os.environ.get("STOPCHECK_DISK", "/mnt/c" if os.path.isdir("/mnt/c") else HERE)  # WSL: Windows C:
 GRAPHS = {"parent_x": "XA-X", "parent_c1": "XA-C1", "parent_c2s": "XA-C2S", "parent_c2a": "XA-C2A"}
 STOPS_X = ["L1", "L2", "L3", "DA", "S1", "S2", "L4", "PG", "CC", "KEY", "KEYC", "NET"]
 STOPS_OTHER = ["L1", "L2", "S1", "S2", "L4", "PG", "CC"]
@@ -89,7 +92,7 @@
 
 
 def c_free_gb():
-    st = os.statvfs("/mnt/c")
+    st = os.statvfs(DISK)
     return st.f_bavail * st.f_frsize / 1e9
 
 
@@ -127,7 +130,7 @@
                 return
             free = c_free_gb()
             if free < 10:
-                log(f"C: has {free:.1f} GB free (< 10 GB floor); ending the batch")
+                log(f"{DISK} has {free:.1f} GB free (< 10 GB floor); ending the batch")
                 return
             tid = f"{cond}-{stop}-t{r}"
             if a.dry_run:
--- a/langgraph/run_trial_ns.sh (commit cd26851)
+++ b/langgraph/run_trial_ns.sh (this folder)
@@ -6,8 +6,9 @@
 #   wsl.exe -d Ubuntu --exec bash -c 'bash /mnt/c/work/agent-shutdown-test/stopcheck/langgraph/run_trial_ns.sh [harness args]'
 # Needs root (the WSL default user here is root). Keep a wsl.exe session open during
 # batches: WSL stops an idle distro after 15 s and the VM after 60 s.
+# On a Linux machine, as root: bash run_trial_ns.sh [harness args]
 set -euo pipefail
-P=/mnt/c/work/agent-shutdown-test/stopcheck/langgraph
+P=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)  # this script's own folder (was the laptop's path)
 PY=${PY:-/root/pilot-venv/bin/python}
 SCRIPT=${SCRIPT:-harness.py}
 exec unshare --net -- bash -c 'ip link set lo up && exec "$0" -u "$1/$2" "${@:3}"' "$PY" "$P" "$SCRIPT" "$@"
```
