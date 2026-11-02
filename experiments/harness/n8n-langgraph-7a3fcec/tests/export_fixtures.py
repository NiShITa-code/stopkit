"""Write every scenario of scenarios_rev6.py as a results folder under tests/fixtures/<name>/ (not committed;
rebuilt from round 1's committed records), plus fixtures/INDEX.json naming the rows each scenario asks about."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scenarios_rev6 as sc  # noqa: E402

out = os.path.join(HERE, "fixtures")
index = {}
for name, (fn, rows, purpose) in sc.S.items():
    s = fn()
    s.write(os.path.join(out, name))
    index[name] = {"base_trial": s.id, "graph": s.R["graph"], "stop": s.R["lever"], "purpose": purpose,
                   "rows": [{"row": r, "carrier": c, "outcome": o} for r, c, o in rows]}
with open(os.path.join(out, "INDEX.json"), "w", encoding="utf-8") as f:
    json.dump(index, f, indent=1)
print(f"{len(index)} scenarios written to {out}")
