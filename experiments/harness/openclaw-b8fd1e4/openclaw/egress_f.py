#!/usr/bin/env python3
"""Experiment F spike: the separate ROOT setup step that closes egress and proves it (SPIKE-F.md section 2.3).

This is the only part of the spike that runs as root. It never runs OpenClaw. The non-root runner (spike_f.py)
never runs iptables itself; it reads the manifest this step writes. Egress is recorded as an OBSERVATION, never a
gate (round-2 review / the fellow's decision): the runner checks the manifest is verified, that its uid is the
runner's own euid, and runs an end-of-window outbound probe that must be refused AND leave a uid-prefixed kernel LOG
line stamped at or after the probe's start. Without all three it records "egress state unverified" rather than
claiming closure; it does not refuse.

Uid-owner drops cover connections the runner's uid opens to IP addresses only. On Ubuntu 24.04 a name lookup goes to
the systemd-resolved stub on 127.0.0.53, which the loopback ACCEPT allows; the upstream query then leaves under the
resolver's uid and is logged with the general prefix. The runner therefore also reports general-prefix drops in the
window, and DNS (DPT=53) among them, without attributing them to OpenClaw.

    sudo python3 -I openclaw/egress_f.py close --user ocspike --out-dir /var/lib/stopcheck-openclaw/egress

What `close` does, in order, writing every command and its exit status into <out-dir>/manifest.json:
  1. Records the filter ruleset before (`iptables-save`, `ip6tables-save`).
  2. Starts a root kernel-log watcher, `journalctl -k -f -o short-iso-precise`, appending to <out-dir>/kernel.log
     (0644, so the runner can read it). The runner reads its run window from this file, so its egress observation
     uses a root-collected kernel log, not a log the non-root user cannot open. The watcher runs in its own session;
     if it dies, the runner's end-of-window probe leaves no new line and the state reads "unverified".
  3. For IPv4 and IPv6 alike: flushes OUTPUT (so an earlier ACCEPT, for example a ufw jump, cannot win), appends
     ACCEPT for loopback and for ESTABLISHED/RELATED (the operator's SSH replies), on IPv6 also the neighbour
     discovery ICMPv6 types, then an owner-matched LOG rule for the runner's uid (prefix "stopcheck-egress-uid ",
     with --log-uid) ahead of a general LOG rule for every other owner (`-m owner ! --uid-owner <uid>`, prefix
     "stopcheck-egress-other "), and sets the OUTPUT policy to DROP last. LOG does not end a packet's traversal, so
     without that exclusion each of the runner's drops would be logged under both prefixes. The uid prefix records
     the runner's own drops; the general prefix records other users' drops (system daemons, the DNS resolver), which
     the runner reports without attributing them to the Gateway.
  4. Records the ruleset after and checks it: policy DROP and exactly the rules above, in that order.
  5. Probes as the runner's user (`runuser -u <user>`): a TCP connect to 1.1.1.1:443 and to [2606:4700:4700::1111]:443
     must fail. The IPv4 probe must also leave a uid-prefixed LOG line in the watcher's file, stamped at or after the
     probe's start (kernel.log is appended to, so an older line never counts): that line is the positive control
     that logging and the watcher work. An IPv6 probe that fails with no route (ENETUNREACH or
     EADDRNOTAVAIL) never reaches the OUTPUT chain, so it needs no log line; any other IPv6 failure must log.
  6. Writes the manifest with `verified: true` only if every command succeeded, both rulesets check out and both
     probes behaved as above; otherwise `verified: false` with the problems, and exits 1.

The VM is disposable, so there is no restore step; kill the watcher (its pid is in the manifest) after export.
Exit status: 0 verified, 1 not verified, 2 refused.
"""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time

MANIFEST_SCHEMA = "stopcheck-f-egress/1"
PREFIX_UID = "stopcheck-egress-uid "
PREFIX_OTHER = "stopcheck-egress-other "
PROBE_TARGETS = {"v4": ("1.1.1.1", 443), "v6": ("2606:4700:4700::1111", 443)}
SAVE_TOOLS = {"v4": "iptables-save", "v6": "ip6tables-save"}
RULE_TOOLS = {"v4": "iptables", "v6": "ip6tables"}
NO_ROUTE = {"errnos": (99, 101), "names": ("EADDRNOTAVAIL", "ENETUNREACH")}   # Linux errno values
ND_TYPES = ("router-solicitation", "neighbour-solicitation", "neighbour-advertisement")

PROBE_PROGRAM = "\n".join([
    "import errno,json,socket,sys",
    "h,p=sys.argv[1],int(sys.argv[2])",
    "s=socket.socket(socket.AF_INET6 if ':' in h else socket.AF_INET,socket.SOCK_STREAM)",
    "s.settimeout(5)",
    "try:",
    " s.connect((h,p));print(json.dumps({'ok':True}))",
    "except OSError as e:",
    " n=e.errno",
    " print(json.dumps({'ok':False,'errno':n,'error':errno.errorcode.get(n,type(e).__name__) if n else type(e).__name__}))",
    "finally:",
    " s.close()",
])


# ---------------------------------------------------------------- rules (pure)

def rule_commands(uid):
    """The iptables and ip6tables commands, in order (flush, accepts, owner LOG, LOG for every other owner, policy
    DROP last)."""
    cmds = []
    for fam in ("v4", "v6"):
        tool = RULE_TOOLS[fam]
        cmds.append([tool, "-w", "-F", "OUTPUT"])
        cmds.append([tool, "-w", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"])
        cmds.append([tool, "-w", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                     "-j", "ACCEPT"])
        if fam == "v6":
            for t in ND_TYPES:
                cmds.append([tool, "-w", "-A", "OUTPUT", "-p", "ipv6-icmp", "--icmpv6-type", t, "-j", "ACCEPT"])
        cmds.append([tool, "-w", "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(int(uid)), "-j", "LOG",
                     "--log-prefix", PREFIX_UID, "--log-uid"])
        # LOG does not end a packet's traversal, so the general rule excludes the runner's uid; otherwise each of the
        # runner's drops would be logged twice, once under each prefix (round-4 review).
        cmds.append([tool, "-w", "-A", "OUTPUT", "-m", "owner", "!", "--uid-owner", str(int(uid)), "-j", "LOG",
                     "--log-prefix", PREFIX_OTHER, "--log-uid"])
        cmds.append([tool, "-w", "-P", "OUTPUT", "DROP"])
    return cmds


def _filter_table(save_text):
    lines, inside = [], False
    for line in (save_text or "").splitlines():
        line = line.strip()
        if line == "*filter":
            inside = True
            continue
        if inside and line == "COMMIT":
            break
        if inside:
            lines.append(line)
    return lines


def check_ruleset(save_text, family, uid):
    """Problems with an iptables-save filter table after closure (empty list: as expected)."""
    table = _filter_table(save_text)
    if not table:
        return ["no filter table in the saved ruleset"]
    problems = []
    if not any(re.match(r"^:OUTPUT DROP\b", ln) for ln in table):
        problems.append("OUTPUT policy is not DROP")
    rules = [ln for ln in table if ln.startswith("-A OUTPUT ")]
    expected = [lambda r: "-o lo" in r and r.endswith("-j ACCEPT"),
                lambda r: "--ctstate" in r and "ESTABLISHED" in r and "RELATED" in r and r.endswith("-j ACCEPT")]
    if family == "v6":
        expected += [lambda r: "ipv6-icmp" in r and r.endswith("-j ACCEPT")] * len(ND_TYPES)
    def owner_is(r, negated):
        m = re.search(r"(!\s+)?--uid-owner (\d+)\b", r)
        return bool(m) and bool(m.group(1)) == negated and int(m.group(2)) == int(uid)

    expected += [lambda r: owner_is(r, False) and "-j LOG" in r and PREFIX_UID.strip() in r and "--log-uid" in r,
                 lambda r: owner_is(r, True) and "-j LOG" in r and PREFIX_OTHER.strip() in r and "--log-uid" in r]
    if len(rules) != len(expected):
        problems.append("OUTPUT has {} rules, expected {}: {}".format(len(rules), len(expected), rules))
    else:
        for i, (rule, ok) in enumerate(zip(rules, expected)):
            if not ok(rule):
                problems.append("OUTPUT rule {} is not the expected one: {}".format(i + 1, rule))
    return problems


def evaluate_probe(family, result, log_lines):
    """Problems with one outbound probe (empty list: it failed as required and logged where it must)."""
    if not isinstance(result, dict):
        return ["no probe result"]
    if result.get("ok") is not False:
        return ["the outbound connection did not fail: egress is open"]
    no_route = result.get("errno") in NO_ROUTE["errnos"] or result.get("error") in NO_ROUTE["names"]
    if family == "v6" and no_route:
        return []
    if not log_lines:
        return ["no uid-prefixed log line for the probe, so logging is not shown to work"]
    return []


# ---------------------------------------------------------------- kernel log (pure)

_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})\s+(.*)$")


def parse_kernel_line(line):
    """(epoch seconds, rest) for a `journalctl -o short-iso-precise` line, or (None, line)."""
    m = _TS.match(line or "")
    if not m:
        return None, line
    y, mo, d, h, mi, s, frac, off, rest = m.groups()
    if off == "Z":
        tz = timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    micro = int((frac or "0").ljust(6, "0")[:6])
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro, tzinfo=tz)
    return dt.timestamp(), rest


def _uid_of(text):
    m = re.search(r"\bUID=(\d+)\b", text)
    return int(m.group(1)) if m else None


def drop_lines(lines, prefix, uid, since, until, exclude_uid=None):
    """Kernel LOG lines with `prefix` (and UID=`uid` when given; not UID=`exclude_uid` when that is given) whose
    timestamp is in the half-open window [since, until). The runner ends the window at its end-of-window probe's
    start, so the probe is never a drop."""
    out = []
    for line in lines:
        t, text = parse_kernel_line(line)
        if t is None or prefix.strip() not in text:
            continue
        if uid is not None and _uid_of(text) != int(uid):
            continue
        if exclude_uid is not None and _uid_of(text) == int(exclude_uid):
            continue
        if since <= t < until:
            out.append(line)
    return out


def dport_of(text):
    m = re.search(r"\bDPT=(\d+)\b", text or "")
    return int(m.group(1)) if m else None


def dns_lines(lines):
    """The lines whose destination port is 53 (DNS over UDP or TCP), e.g. the resolver's upstream queries."""
    return [line for line in lines if dport_of(line) == 53]


def probe_log_lines(lines, uid, host, since=None):
    """Uid-prefixed LOG lines whose DST is `host` (the kernel prints IPv6 in full form, so compare parsed). With
    `since`, only lines stamped at or after it count, so a stale line (an earlier probe, a watcher that has since died)
    never stands for this probe; an unstamped line then never counts either."""
    target = ipaddress.ip_address(host)
    out = []
    for line in lines:
        t, text = parse_kernel_line(line)
        if since is not None and (t is None or t < since):
            continue
        if PREFIX_UID.strip() not in text or _uid_of(text) != int(uid):
            continue
        m = re.search(r"\bDST=(\S+)", text)
        try:
            if m and ipaddress.ip_address(m.group(1)) == target:
                out.append(line)
        except ValueError:
            continue
    return out


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------- the root step

class RealHost:
    """Runs the setup commands for real (root only)."""

    def run(self, argv, timeout=30):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return {"argv": argv, "rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-4000:]}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"argv": argv, "rc": None, "error": "{}: {}".format(type(e).__name__, e)}

    def probe(self, user, family, host, port):
        rec = self.run(["runuser", "-u", user, "--", "python3", "-I", "-c", PROBE_PROGRAM, host, str(port)], 20)
        for line in reversed((rec.get("stdout") or "").splitlines()):
            try:
                return dict(json.loads(line), raw_rc=rec.get("rc"))
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def start_watcher(self, path, since_epoch):
        argv = ["journalctl", "-k", "-f", "--no-pager", "-o", "short-iso-precise", "--since",
                "@{}".format(int(since_epoch))]
        out = open(path, "ab")
        os.chmod(path, 0o644)
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        out.close()
        return {"pid": proc.pid, "argv": argv, "path": path}


def close_egress(user, uid, out_dir, host, wait_s=15.0, poll_s=0.5):
    """Close egress, verify it, and write <out_dir>/manifest.json. Returns the manifest."""
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o755)
    t0 = time.time()
    problems, commands = [], []
    before = {fam: host.run([SAVE_TOOLS[fam]]) for fam in ("v4", "v6")}
    watcher = host.start_watcher(os.path.join(out_dir, "kernel.log"), t0 - 5)
    for argv in rule_commands(uid):
        rec = host.run(argv)
        commands.append(rec)
        if rec.get("rc") != 0:
            problems.append("{} rc {}: {}".format(" ".join(argv), rec.get("rc"),
                                                  (rec.get("stderr") or rec.get("error") or "").strip()[:300]))
    after = {fam: host.run([SAVE_TOOLS[fam]]) for fam in ("v4", "v6")}
    for fam in ("v4", "v6"):
        if after[fam].get("rc") != 0:
            problems.append("{} rc {}".format(SAVE_TOOLS[fam], after[fam].get("rc")))
        problems += ["{} ruleset: {}".format(fam, p) for p in check_ruleset(after[fam].get("stdout"), fam, uid)]
    nft = host.run(["nft", "list", "ruleset"]) if shutil.which("nft") else None
    probes = {}
    for fam, (h, port) in PROBE_TARGETS.items():
        started = time.time()
        probes[fam] = {"target": [h, port], "started_wall": started, "result": host.probe(user, fam, h, port),
                       "log_lines": []}
    deadline = time.time() + wait_s
    while True:
        lines = read_lines(watcher["path"])
        for fam, (h, _) in PROBE_TARGETS.items():
            probes[fam]["log_lines"] = probe_log_lines(lines, uid, h, since=probes[fam]["started_wall"])
        if all(not evaluate_probe(f, probes[f]["result"], probes[f]["log_lines"]) for f in probes) \
                or time.time() >= deadline:
            break
        time.sleep(poll_s)
    for fam in ("v4", "v6"):
        problems += ["{} probe: {}".format(fam, p)
                     for p in evaluate_probe(fam, probes[fam]["result"], probes[fam]["log_lines"])]
    manifest = {"schema": MANIFEST_SCHEMA, "created_wall": t0, "host": os.uname().nodename if hasattr(os, "uname")
                else None, "user": user, "uid": int(uid), "prefix_uid": PREFIX_UID, "prefix_other": PREFIX_OTHER,
                "rules_before": {f: before[f].get("stdout") for f in before},
                "rules_after": {f: after[f].get("stdout") for f in after},
                "nft_ruleset": (nft or {}).get("stdout"), "commands": commands, "probes": probes,
                "watcher": watcher, "verified": not problems, "problems": problems}
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    os.chmod(path, 0o644)
    return manifest


def refusal_reasons(platform=None, euid=None, user=None, getpwnam=None):
    reasons = []
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return ["not Linux ({})".format(platform)]
    euid = os.geteuid() if euid is None else euid
    if euid != 0:
        reasons.append("egress closure needs root; run it with sudo, separately from the non-root runner")
    if getpwnam is None:
        import pwd
        getpwnam = pwd.getpwnam
    try:
        getpwnam(user)
    except KeyError:
        reasons.append("no such user: {!r}".format(user))
    return reasons


def main(argv=None):
    ap = argparse.ArgumentParser(description="Experiment F spike: close egress as root and verify it.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("close", help="close egress, start the kernel-log watcher, probe, write the manifest")
    c.add_argument("--user", required=True, help="the non-root user the runner and Gateway run as")
    c.add_argument("--out-dir", default="/var/lib/stopcheck-openclaw/egress")
    args = ap.parse_args(argv)
    reasons = refusal_reasons(user=args.user)
    if reasons:
        print("REFUSED:", file=sys.stderr)
        for r in reasons:
            print("  - " + r, file=sys.stderr)
        return 2
    import pwd
    uid = pwd.getpwnam(args.user).pw_uid
    manifest = close_egress(args.user, uid, args.out_dir, RealHost())
    print("manifest {} verified={}".format(os.path.join(args.out_dir, "manifest.json"), manifest["verified"]))
    for p in manifest["problems"]:
        print("  - " + p, file=sys.stderr)
    return 0 if manifest["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
