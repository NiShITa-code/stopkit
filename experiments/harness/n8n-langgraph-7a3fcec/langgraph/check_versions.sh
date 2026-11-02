#!/bin/bash
# Condition E, step 1: ask PyPI (through pip) for the releases of the five LangGraph packages
# and compare them with pilot-01's pins. Writes results/pypi_versions.txt.
# Usage (from Windows): wsl.exe -d Ubuntu -- bash /mnt/c/work/agent-shutdown-test/pilot-02-validation/check_versions.sh
P=/mnt/c/work/agent-shutdown-test/pilot-02-validation
PIP=/root/pilot-venv/bin/pip
declare -A PIN=( [langgraph]=1.2.12 [langgraph-api]=0.15.1 [langgraph-runtime-inmem]=0.35.1 [langgraph-cli]=0.4.32 [langgraph-sdk]=0.4.5 )
{
  echo "checked at: $(date '+%Y-%m-%d %H:%M:%S %Z') with $($PIP --version)"
  echo "command: pip index versions <package>   (stable releases only; pip's default excludes pre-releases)"
  for p in langgraph langgraph-api langgraph-runtime-inmem langgraph-cli langgraph-sdk; do
    line=$($PIP index versions "$p" 2>/dev/null | head -1)
    avail=$($PIP index versions "$p" 2>/dev/null | grep -i "Available versions" | head -1)
    newest=$(echo "$line" | sed -E 's/.*\(([^)]*)\).*/\1/')
    echo "== $p  pinned ${PIN[$p]}  newest stable on PyPI: $newest"
    echo "   ${avail:0:300}"
  done
} 2>&1 | tee "$P/results/pypi_versions.txt"
