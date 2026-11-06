#!/usr/bin/env bash
# Apex Investor container scheduler.
#
# Runs scripts/daily_report.py once at startup, then daily at REPORT_TIME_UTC
# (default 21:30 UTC, after US market close). No broker, no orders — report only.
#
# Env:
#   REPORT_TIME_UTC  "HH:MM" in UTC for the scheduled daily run (default 21:30)
#   PORTFOLIO_MODE   "example" (demo, bundled fixture) or "live" (data/portfolio.json).
#                    In live mode a missing portfolio FAILS CLOSED (no report, no demo data).
set -uo pipefail

REPORT_TIME_UTC="${REPORT_TIME_UTC:-21:30}"
PORTFOLIO_MODE="${PORTFOLIO_MODE:-example}"

cd /app

run_report() {
  echo "=== apex-investor report run @ $(date -u +%FT%TZ) ==="
  if [ "$PORTFOLIO_MODE" = "live" ]; then
    if [ -f data/portfolio.json ]; then
      python scripts/daily_report.py || echo "WARNING: report run exited non-zero"
    else
      echo "PORTFOLIO_MODE=live but data/portfolio.json is missing — FAILING CLOSED, no report."
    fi
  else
    echo "NOTE: PORTFOLIO_MODE=example — demo portfolio only, never the live ledger."
    python scripts/daily_report.py --example || echo "WARNING: report run exited non-zero"
  fi
}

# Seconds until the next occurrence of REPORT_TIME_UTC (GNU date, UTC).
next_run_seconds() {
  local now target
  now=$(date -u +%s)
  target=$(date -u -d "today ${REPORT_TIME_UTC}" +%s)
  if [ "$target" -le "$now" ]; then
    target=$((target + 86400))
  fi
  echo $((target - now))
}

run_report
while true; do
  wait_s=$(next_run_seconds)
  echo "next scheduled run in ${wait_s}s (REPORT_TIME_UTC=${REPORT_TIME_UTC})"
  sleep "$wait_s"
  run_report
done
