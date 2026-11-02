# OpenClaw as a config file

[agent.toml](agent.toml) describes OpenClaw 2026.10.2's Gateway to the kit: how to start it, how to point it at the kit's stand-in model (its `openclaw.json`, written for each run), how to give it a turn, the name of its shell tool (`exec`) and its own stop, `sessions.abort`. It is the agent that the ready-made binding [src/stopkit/bindings/openclaw.py](../../src/stopkit/bindings/openclaw.py) runs, with the same configuration, written as a file instead of code; [docs/CONFIG.md](../../docs/CONFIG.md) explains every field.

On a test machine set up as the README says, as root:

```bash
bash setup/openclaw.sh
python3 -B -m stopkit doctor examples/openclaw/agent.toml
python3 -B -m stopkit check examples/openclaw/agent.toml --stop none --stop abort --stop sigterm --stop kill --stop kill-scope --stop reference
```

These six stops are the ones the ready-made binding tests: its `gateway-sigterm` is the file's `sigterm`, with the same declared scope. The file also plants more work than the binding does (section 4 of docs/CONFIG.md), so its runs cover more surfaces.
