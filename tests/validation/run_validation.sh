#!/usr/bin/env bash
# Run the finding-validation harness with ALL stdout+stderr captured to logs/.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
TS="$(date +%Y%m%d-%H%M%S)"
LOG="logs/validation-${TS}.log"

{
  echo "# smarter-mcp finding validation"
  echo "# started: $(date)"
  echo "# repo: $REPO_ROOT"
  echo "# commit: $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  echo "###############################################################################"
} > "$LOG"

# Combine stdout+stderr, tee to console and log. Disable the RTK hook noise.
uv run --extra all python tests/validation/validate_findings.py >> "$LOG" 2>&1
RC=$?

{
  echo "###############################################################################"
  echo "# harness exit code: $RC"
  echo "# finished: $(date)"
} >> "$LOG"

echo "Log written to: $LOG"
ln -sf "validation-${TS}.log" logs/validation-latest.log
exit $RC
