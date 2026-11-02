"""Build synthetic Experiment A trials for the regression tests by editing a real round 1 record in memory.

A scenario starts from one round 1 trial (its record, pings and stand-in events) and replaces only the parts it
is about. Times are given relative to the trial's own t_stop. Nothing on disk is changed."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LG = os.environ.get("STOPCHECK_LG") or os.path.join(ROOT, "langgraph")  # tests/mutate.py points it at a mutant
R1 = os.path.join(LG, "results", "expA-round1")
if LG not in sys.path:
    sys.path.insert(0, LG)
import analyze_x as ax  # noqa: E402

_CACHE = {}


def _jl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _load():
    if not _CACHE:
        _CACHE["trials"] = {t["trial"]: t for t in _jl(os.path.join(R1, "trials.jsonl"))}
        _CACHE["pings"] = _jl(os.path.join(R1, "pings.jsonl"))
        _CACHE["outside"] = _jl(os.path.join(R1, "mocks", "outside.jsonl"))
        _CACHE["issuer"] = _jl(os.path.join(R1, "mocks", "issuer.jsonl"))
    return _CACHE


class Scenario:
    """A mutable copy of one round 1 trial."""

    def __init__(self, base_trial):
        c = _load()
        self.R = copy.deepcopy(c["trials"][base_trial])
        self.id = self.R["trial"]
        self.t0 = self.R["t_stop"]
        self.pings = [copy.deepcopy(p) for p in c["pings"] if p.get("trial") == self.id]
        self.outside = [copy.deepcopy(e) for e in c["outside"] if e.get("trial") == self.id]
        self.issuer = [copy.deepcopy(e) for e in c["issuer"] if e.get("trial") == self.id or e.get("trial") is None]

    # ---- pings
    def drop_pings(self, canary, after_rel=None, before_rel=None, keep=None):
        """Remove a canary's pings in (after_rel, before_rel] (rel t_stop; None = unbounded)."""
        lo = -1e18 if after_rel is None else self.t0 + after_rel
        hi = 1e18 if before_rel is None else self.t0 + before_rel
        self.pings = [p for p in self.pings if not (p.get("canary") == canary and lo < p["mono"] <= hi
                                                    and (keep is None or not keep(p)))]

    def add_ping(self, canary, rel, sender_rel=None, **fields):
        """A ping arriving at t_stop + rel; with sender_rel, it also carries a sender stamp."""
        p = {"t": self.R["t_stop_wall"] + rel, "mono": self.t0 + rel, "canary": canary, "trial": self.id,
             "n": fields.pop("n", 0)}
        if sender_rel is not None:
            p["smono"] = f"{self.t0 + sender_rel:.6f}"
        p.update({k: (f"{self.t0 + v:.6f}" if k == "smono_init" else v) for k, v in fields.items()})
        self.pings.append(p)
        return p

    def stamp_all(self, offset=-0.002):
        """Give every ping a sender stamp `offset` s before its arrival (as round 2 pings will have)."""
        for p in self.pings:
            p["smono"] = f"{p['mono'] + offset:.6f}"

    # ---- stand-in events
    def drop_outside(self, marker, event=None, after_rel=None):
        lo = -1e18 if after_rel is None else self.t0 + after_rel
        self.outside = [e for e in self.outside if not (e.get("marker") == marker and
                                                        (event is None or e.get("event") == event)
                                                        and e["mono"] > lo)]

    def add_outside(self, event, marker, rel, **fields):
        e = {"event": event, "marker": marker, "trial": self.id, "wall": self.R["t_stop_wall"] + rel,
             "mono": self.t0 + rel}
        e.update(fields)
        self.outside.append(e)
        return e

    # ---- status reads
    def set_read(self, key, run=None, thread=None, reply_rel=None, down=False):
        """Set a status read's run and thread answer. run/thread: a status string, '404', or None to keep.
        reply_rel: the reply stamp (rel t_stop) for both requests. down: the server did not answer."""
        snap = self.R.setdefault(key, {})
        if down:
            self.R[key] = {"t": snap.get("t", self.t0), "server": "unreachable (no answer on 127.0.0.1:2024/ok)"}
            return
        for k, v in (("parent_run", run), ("parent_thread", thread)):
            if v is None:
                continue
            snap[k] = {"error": "HTTP 404: not found"} if v == "404" else {"status": v}
        snap["server"] = "up"
        if reply_rel is not None:
            snap["t_reply"] = {k: self.t0 + reply_rel for k in ("parent_run", "parent_thread")}

    # ---- export (for writing expected values independently of the scorer)
    def write(self, folder):
        """Write this scenario as a results folder: trials.jsonl, pings.jsonl, mocks/outside.jsonl, issuer.jsonl."""
        os.makedirs(os.path.join(folder, "mocks"), exist_ok=True)
        for name, rows in (("trials.jsonl", [self.R]), ("pings.jsonl", sorted(self.pings, key=lambda p: p["mono"])),
                           ("mocks/outside.jsonl", sorted(self.outside, key=lambda e: e["mono"])),
                           ("mocks/issuer.jsonl", sorted(self.issuer, key=lambda e: e["mono"]))):
            with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
                for x in rows:
                    f.write(json.dumps(x) + "\n")

    # ---- scoring
    def trial(self):
        mocks = {"outside": sorted(self.outside, key=lambda e: e["mono"]),
                 "issuer": sorted(self.issuer, key=lambda e: e["mono"])}
        return ax.Trial(self.R, sorted(self.pings, key=lambda p: p["mono"]), mocks)

    def score(self):
        T = self.trial()
        v = ax.validity(T)
        rows = ax.score_trial(T, self.R["graph"], v)
        return v, rows


def find(rows, row, carrier, outcome, field="kind"):
    hit = [r for r in rows if r["row"] == row and r["carrier"] == carrier and r["outcome"] == outcome]
    if len(hit) != 1:
        raise AssertionError(f"{len(hit)} rows for ({row}, {carrier}, {outcome})")
    return hit[0][field] if field else hit[0]
