"""Experiment C's wall/monotonic clock-consistency rule (RULES-expC C.9; pre-freeze batch of 13 Oct, item 5), the one
implementation that the harness and the scorer share. harness_c.check_trial_clocks applies it to the receiver's paired
stamps when a trial ends (TrialRun.finalize); analyze_c recomputes it from the same stamps in the trial's receiver.jsonl
and reports both verdicts.

The rule is n8n/clockwall_b.py's (RULES-expB B.9), copied with its constants and algorithm unchanged so that Experiment
C's freeze binds no n8n file: each interval between paired wall/monotonic stamps must agree within 0.1 s plus 1,000 ppm
of the interval, over at least 10 s of coverage. The receiver stamps every line on arrival with CLOCK_REALTIME (`t`),
CLOCK_MONOTONIC (`mono`) and CLOCK_MONOTONIC_RAW (`raw`) (receiver_c.py).

Pure and side-effect free: standard library only, no file, clock, process or network access. Tests:
tests/test_clockwall_c.py.
"""
import math

WALL_STEP_TOL_S = 0.1           # each wall/monotonic interval may deviate by 0.1 s ...
WALL_DRIFT_TOL_PPM = 1000.0     # ... plus 1,000 ppm of the interval (common/clockguard.py's RATE_TOL_PPM)
WALL_COVERAGE_S = 10.0          # the paired stamps must span at least 10 s of monotonic time


def check_wall_stamps(pairs, step_tol_s=WALL_STEP_TOL_S, drift_tol_ppm=WALL_DRIFT_TOL_PPM, window_s=WALL_COVERAGE_S):
    """Require wall/mono intervals to agree within 0.1 s plus 1000 ppm of the interval.

    Both monotonic and raw can pause together while realtime advances, so their rate agreement is
    insufficient for the platform's own timers (Temporal's history event times and timeouts). Running extrema test
    EVERY interval in linear time after sorting: offset - drift*mono finds forward steps; offset + drift*mono finds
    backward steps. Missing paired evidence or less than ten seconds of coverage is unresolved and fails the guard.
    This receipt describes clock consistency, not the cause of a clock discontinuity.
    """
    rows, rejected = [], 0
    for mono, wall in pairs:
        if (isinstance(mono, (int, float)) and isinstance(wall, (int, float)) and
                math.isfinite(mono) and math.isfinite(wall)):
            rows.append((mono, wall))
        else:
            rejected += 1
    rows.sort()
    reasons = []
    out = {"ok": False, "reasons": reasons, "stamps": len(rows), "rejected_stamps": rejected,
           "step_tolerance_s": step_tol_s, "drift_tolerance_ppm": drift_tol_ppm,
           "minimum_coverage_s": window_s,
           "policy": "each wall/mono interval within 0.1 s plus 1000 ppm of that interval"}
    if rejected:
        reasons.append(f"{rejected} receiver stamps lack finite paired wall/monotonic evidence")
    if len(rows) < 2 or rows[-1][0] - rows[0][0] < window_s:
        reasons.append(f"too few paired wall/monotonic stamps (need two at least {window_s:g} s apart)")
        return out
    drift = drift_tol_ppm / 1_000_000.0
    low, high = 0, 0
    worst, max_adjacent = None, 0.0

    def interval(i, j):
        duration = rows[j][0] - rows[i][0]
        deviation = (rows[j][1] - rows[i][1]) - duration
        allowed = step_tol_s + drift * duration
        return {"start_mono": rows[i][0], "end_mono": rows[j][0],
                "start_wall": rows[i][1], "end_wall": rows[j][1], "span_mono_s": duration,
                "deviation_s": deviation, "allowed_deviation_s": allowed,
                "excess_s": abs(deviation) - allowed}

    for j in range(1, len(rows)):
        max_adjacent = max(max_adjacent, abs(interval(j - 1, j)["deviation_s"]))
        for i in (low, high):
            candidate = interval(i, j)
            if worst is None or candidate["excess_s"] > worst["excess_s"]:
                worst = candidate
        offset = rows[j][1] - rows[j][0]
        low_offset = rows[low][1] - rows[low][0]
        high_offset = rows[high][1] - rows[high][0]
        if offset - drift * rows[j][0] < low_offset - drift * rows[low][0]:
            low = j
        if offset + drift * rows[j][0] > high_offset + drift * rows[high][0]:
            high = j
    whole = interval(0, len(rows) - 1)
    out.update(span_mono_s=whole["span_mono_s"], span_wall_s=rows[-1][1] - rows[0][1],
               maximum_adjacent_deviation_s=max_adjacent, whole_span_deviation_s=whole["deviation_s"],
               worst_interval=worst)
    if worst["excess_s"] > 0:
        reasons.append(f"wall/monotonic discontinuity: interval deviation {worst['deviation_s']:+.6f} s "
                       f"exceeds allowed {worst['allowed_deviation_s']:.6f} s")
    out["ok"] = not reasons
    return out


def receiver_pairs(rows):
    """The (monotonic, wall) stamps of each receiver line: its `mono` and its wall time `t` (receiver_c.py)."""
    return [(row.get("mono"), row.get("t")) for row in rows]


def check_receiver_rows(rows):
    """check_wall_stamps over every line of receiver.jsonl, as the harness's across-trial clock check applies it."""
    return check_wall_stamps(receiver_pairs(rows))
