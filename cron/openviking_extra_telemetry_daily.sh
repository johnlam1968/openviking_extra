#!/usr/bin/env bash
# openviking_extra_telemetry_daily.sh — Daily telemetry analyzer for the
# openviking_extra plugin. Runs `analyze_telemetry.py --brief --open-issues`
# and emits a 3-5 line Telegram-ready message if real anomalies are detected.
# Also files GitHub issues automatically for each unique tool/pattern
# (with dedup + caps so the repo doesn't get spammed).
#
# Per cron-script-only contract (skills/devops/cron-script-only/SKILL.md):
#   - exit 0, empty stdout → silent tick (no delivery)
#   - exit 0, non-empty stdout → stdout delivered verbatim
#   - exit 1 → error alert delivered (script crashed)
#   - exit 1 with --brief content → anomaly alert (handled by the analyzer)
#
# The analyzer exits 1 when real anomalies exist; we re-emit its stdout
# to our stdout so Telegram gets the brief message on anomalies.
#
# Internal log: $LOG_DIR/openviking_extra_telemetry_<ts>.log (always written
# for debugging; not used for delivery).

set -uo pipefail

VENV_PY="${HERMES_PY:-/home/john/.hermes/hermes-agent/venv/bin/python3}"
ANALYZER="/home/john/.hermes/skills/devops/openviking-extra-telemetry/scripts/analyze_telemetry.py"

LOG_DIR="$HOME/.hermes/cron/output"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/openviking_extra_telemetry_${TS}.log"

# 7-day window — backfill with --days if needed
DAYS="${OPENVIKING_TELEMETRY_DAYS:-7}"
# Hard caps for auto-issue creation
MAX_ISSUES="${OPENVIKING_TELEMETRY_MAX_ISSUES:-5}"
DEDUP_DAYS="${OPENVIKING_TELEMETRY_DEDUP_DAYS:-7}"

# Run the analyzer once. --brief gives the 3-5 line Telegram message.
# --open-issues files GitHub issues for unique tool/pattern anomalies.
out="$("$VENV_PY" "$ANALYZER" --brief --open-issues --days "$DAYS" \
                  --max-issues "$MAX_ISSUES" --dedup-days "$DEDUP_DAYS" 2>&1)"
rc=$?

# Write a debug log (always, even on silent ticks)
{
  echo "[openviking_extra_telemetry] start $TS (window=${DAYS}d)"
  echo "[openviking_extra_telemetry] analyzer exit=$rc"
  echo "---- analyzer output ----"
  printf '%s\n' "$out"
  echo "---- end ----"
} > "$LOG" 2>&1

# Unexpected exit code = script or analyzer crashed. Emit error to stdout
# so the cron error-alert gate fires (cron contract: exit 1 = error alert,
# but ALSO stdout non-empty → deliver verbatim, which is what we want for
# the Telegram alert path).
if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then
  echo "[openviking_extra_telemetry] FAILED (unexpected exit=$rc) — see $LOG" >&2
  exit $rc
fi

# rc=1 means real anomalies. Emit the brief as the Telegram message body.
# rc=0 means silent tick — emit nothing to stdout.
if [ $rc -eq 1 ] && [ -n "$out" ]; then
  printf '%s\n' "$out"
  exit 1
fi

# Silent tick.
exit 0