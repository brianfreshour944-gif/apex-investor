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

# Persistent-storage paths (Coolify mounts volumes here). The app runs as APP_UID/APP_GID.
APP_UID=10001
APP_GID=10001
DATA_DIR=/app/data
REPORTS_DIR=/app/reports

# --- ownership repair, then drop privileges (root only) ---------------------
# An EXISTING named volume (or a Coolify bind mount, whose host directory is created as root)
# is NOT re-seeded by the image, so it can be root-owned while the app runs as UID 10001. That
# is exactly the failure this block fixes: the report generates but cannot be saved. Root runs
# ONLY this repair; the scheduler and the Python process run as APP_UID. We deliberately do not
# loosen the directory to world-writable as a shortcut.
if [ "$(id -u)" -eq 0 ]; then
  # Deliberate, verified privilege-drop mechanism. Do not fail obscurely if it is absent.
  if ! command -v setpriv >/dev/null 2>&1; then
    echo "FATAL: setpriv is required to drop privileges but was not found" >&2
    exit 1
  fi

  mkdir -p "$DATA_DIR" "$REPORTS_DIR/daily" || {
    echo "FATAL: cannot create persistent directories ($DATA_DIR, $REPORTS_DIR/daily)" >&2
    exit 1
  }

  # Do NOT silently ignore a repair failure: if ownership cannot be fixed, stop with a clear
  # error rather than starting a scheduler that will fail on every write.
  if ! chown -R "$APP_UID:$APP_GID" "$DATA_DIR" "$REPORTS_DIR"; then
    echo "FATAL: cannot repair persistent-volume ownership ($DATA_DIR, $REPORTS_DIR)" >&2
    exit 1
  fi

  # Mark this re-exec so the unprivileged run is recognised as the legitimate post-drop run.
  export APEX_ENTRYPOINT_PRIVDROPPED=1
  exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups -- "$0" "$@"
fi

# Anything reaching here is the unprivileged process; it MUST be APP_UID. A mismatch (e.g. a
# stale compose `user:` directive) is a configuration error, not something to run through.
if [ "$(id -u)" -ne "$APP_UID" ]; then
  echo "FATAL: application must run as UID $APP_UID, but is UID $(id -u)" >&2
  exit 1
fi

# A container started DIRECTLY as APP_UID (e.g. a `user:` override in the runtime config) skips
# the root ownership-repair block above, so a mounted volume may still be root-owned and
# unwritable — the exact failure this entrypoint exists to prevent. Only the legitimate
# re-executed process carries the marker, so anything else fails closed with an explanation.
if [ "${APEX_ENTRYPOINT_PRIVDROPPED:-}" != "1" ]; then
  echo "FATAL: reached application startup as UID $APP_UID without the root ownership-repair step." >&2
  echo "       Start the container as root (the default) so the entrypoint can repair the" >&2
  echo "       persistent volumes first; do not start it directly as UID $APP_UID." >&2
  exit 1
fi

REPORT_TIME_UTC="${REPORT_TIME_UTC:-21:30}"
PORTFOLIO_MODE="${PORTFOLIO_MODE:-example}"

cd /app || {
  echo "FATAL: cannot change to /app" >&2
  exit 1
}

# F6: fail visibly on invalid configuration instead of looping forever.
# A malformed REPORT_TIME_UTC must STOP the container with a clear error, so Coolify shows the
# failure rather than leaving a broken scheduler running.
if ! printf '%s' "$REPORT_TIME_UTC" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$'; then
  echo "FATAL: invalid REPORT_TIME_UTC='$REPORT_TIME_UTC' (expected HH:MM, 00:00-23:59 UTC)" >&2
  exit 1
fi

# H4: reject an invalid PORTFOLIO_MODE instead of silently falling through to example mode.
# Anything other than exactly "live" or "example" is a configuration error, not a demo run.
case "$PORTFOLIO_MODE" in
  live|example) ;;
  *)
    echo "FATAL: invalid PORTFOLIO_MODE='$PORTFOLIO_MODE' (expected 'live' or 'example')" >&2
    exit 1
    ;;
esac

# F5: same-day idempotency. The scheduler runs once at startup AND once per scheduled time, so a
# restart on the same day would otherwise duplicate the report (extra backups, noisy audit trail).
# The state file lives on the persistent volume (/app/data), so the guard survives redeploys.
# H5: the state file is PER MODE, so a successful example run can never suppress the day's live
# report (and vice versa). NOTE: renaming the marker from the old shared ".last_report_run" means
# the first run after this change will re-run once, even if today was already marked done.
STATE_FILE="/app/data/.last_report_run.${PORTFOLIO_MODE}"

run_report() {
  local today rc
  today=$(date -u +%F)
  if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$today" ]; then
    echo "already reported for $today (state file) — skipping duplicate run"
    return 0
  fi
  echo "=== apex-investor report run @ $(date -u +%FT%TZ) ==="
  if [ "$PORTFOLIO_MODE" = "live" ]; then
    if [ -f data/portfolio.json ]; then
      python scripts/daily_report.py
      rc=$?
      [ "$rc" -eq 0 ] && printf '%s\n' "$today" > "$STATE_FILE"
      [ "$rc" -ne 0 ] && echo "WARNING: report run exited non-zero ($rc) — day not marked done"
    else
      echo "PORTFOLIO_MODE=live but data/portfolio.json is missing — FAILING CLOSED, no report."
    fi
  else
    echo "NOTE: PORTFOLIO_MODE=example — demo portfolio only, never the live ledger."
    python scripts/daily_report.py --example
    rc=$?
    [ "$rc" -eq 0 ] && printf '%s\n' "$today" > "$STATE_FILE"
    [ "$rc" -ne 0 ] && echo "WARNING: report run exited non-zero ($rc) — day not marked done"
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
