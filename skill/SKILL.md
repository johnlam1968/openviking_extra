---
name: openviking-extra-telemetry
description: "Use when openviking_extra plugin (the one exposing viking_write / viking_link / viking_grep / viking_glob / viking_extract / viking_relation_graph for Hermes Agent) needs observability analysis, when the JSONL log at ~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl needs to be turned into actionable improvement suggestions, or when you want to detect recurring error patterns and latency regressions before users complain. Runs daily analysis via cron, alerts on anomalies, surfaces concrete plugin fix candidates. Companion skill to the openviking_extra plugin itself."
version: 1.0.0
author: John + Hermes Agent
license: MIT
---

# openviking-extra-telemetry

**Trigger**: ANY time you want to understand how the openviking_extra plugin is being used in production — error rates, latency regressions, underused tools, or recurring error patterns that suggest a bug fix.

**Why this skill exists**: The openviking_extra plugin has an opt-in telemetry hook (`post_tool_call`) that writes JSONL records to `~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl`. Raw JSONL is hard to read at scale. This skill turns it into actionable signal: which tools fail, what's slow, which tools are unused, what error messages keep recurring.

**The improvement loop this closes**:
```
production traffic → JSONL log → analyze_telemetry.py → suggestions → plugin PR
```

## What this skill provides

### Tool 1: `analyze_telemetry.py` (analysis script)

Located at `scripts/analyze_telemetry.py`. Self-contained Python (no extra deps).

```bash
# Default: last 30 days, text report
python3 scripts/analyze_telemetry.py

# Last 7 days
python3 scripts/analyze_telemetry.py --days 7

# From a specific date
python3 scripts/analyze_telemetry.py --since 2026-06-25

# Machine-readable JSON (for piping into jq / dashboards)
python3 scripts/analyze_telemetry.py --json
```

**Exit codes** (designed for cron alerting):
- `0` = no anomalies (informational only)
- `1` = anomalies detected (review the suggestions)
- `2` = error (bad args, file not found, etc.)

### Output structure

A text report has three sections:

1. **Volume by tool** — table of `calls / errors / err_rate / p50_ms / p95_ms`
2. **Top recurring failures** — error messages grouped + normalized (URIs/UUIDs/numbers → placeholders) so "404 on foo.md" clusters with "404 on bar.md"
3. **Suggested improvements** — concrete, actionable. Currently fires on:
   - Error rate ≥ 20% (over enough calls)
   - p95 latency ≥ 5000ms
   - Same error message recurring ≥ 3 times
   - Tool with zero calls in window (unused — consider deprecating)

JSON mode returns the same data as a single dict.

### Daily cron recipe (for users who want alerts)

Add to `~/.hermes/config.yaml`:

```yaml
cron:
  - id: "openviking-extra-telemetry-daily"
    name: "openviking_extra plugin telemetry analysis"
    schedule: "0 6 * * *"   # 6 AM daily
    mode: "no-agent"
    script: "/home/john/.hermes/skills/devops/openviking-extra-telemetry/scripts/analyze_telemetry.py"
    args: ["--days", "7"]
    deliver: "telegram"
```

This sends a daily report to Telegram. When the script exits 1 (anomalies), the message body contains the suggestions; when it exits 0, it's just the volume table.

## What the JSONL contains

Records are written by the openviking_extra plugin's `post_tool_call` hook. One record per tool call:

```json
{
  "ts": "2026-06-28T03:15:42-04:00",
  "tool": "viking_write",
  "task_id": "abc-123-...",
  "duration_ms": 142,
  "args": {"uri": "...", "content": "truncated to 200 chars..."},
  "result_status": "ok" | "error" | "unknown",
  "result_summary": "first 200 chars of handler return...",
  "error": "only present when result_status=error",
  "plugin_version": "0.3.0"
}
```

**Disabled by default.** Enable per Hermes AGENTS.md "Outbound telemetry without opt-in gating" by adding to `~/.hermes/config.yaml`:

```yaml
plugins:
  openviking_extra:
    telemetry_enabled: true
```

Or via env var: `OPENVIKING_EXTRA_TELEMETRY=1`.

## Real-world example (from initial test)

With 16 synthetic records (8 glob OK, 5 grep OK, 3 write errors at p95=6.7s):

```
======================================================================
openviking_extra telemetry report (last 30 days, 16 records)
======================================================================

## Volume by tool

  tool                       calls  errors  err_rate   p50_ms   p95_ms
  -----------------------------------------------------------------
  viking_glob                    8       0      0.0%       70       85
  viking_grep                    5       0      0.0%      220      240
  viking_write                   3       3    100.0%     6600     6700

## Top recurring failures

  [3x] {"code": "NOT_FOUND", "message": "File not found: <URI>"}

## Suggested improvements

  🔁  Recurring failure (3x): ...
  💤  viking_extract: zero calls in window...
```

This output is **actionable**: 100% error rate on `viking_write` plus a clear failure pattern is exactly the kind of signal that justifies a plugin fix PR.

## Caveats

1. **JSONL is append-only with no rotation beyond daily.** Files grow linearly with traffic. For high-volume users (>10k calls/day) add logrotate config:
   ```bash
   # /etc/logrotate.d/openviking-extra (or ~/.hermes/config equivalent)
   ~/.hermes/logs/openviking_extra/*.jsonl {
       daily
       rotate 7
       compress
       missingok
   }
   ```

2. **Sensitive arg fields are truncated but not redacted.** If you ever pass `api_key`, `token`, etc. as a tool arg, expand `_SENSITIVE_ARG_FIELDS` in `~/.hermes/plugins/openviking_extra/telemetry.py`.

3. **The analyzer's thresholds are heuristics.** `ERROR_RATE_ALERT=0.20` and `P95_LATENCY_MS_ALERT=5000` may need tuning for your workload. Edit `scripts/analyze_telemetry.py` directly.

4. **Zero-data state produces "💤 unused" suggestions for all tools.** If you just enabled telemetry, ignore the first report.

## See also

- openviking_extra plugin source: `~/.hermes/plugins/openviking_extra/`
- Telemetry module: `~/.hermes/plugins/openviking_extra/telemetry.py`
- post_tool_call hook: `~/.hermes/plugins/openviking_extra/__init__.py::_on_post_tool_call`
- Plugin documentation: `~/.hermes/plugins/openviking_extra/README.md`
- Memory `mem_42ff1e5958a4.md`: Hermes hook system signature
- Memory `mem_40737a8c25ea.md`: fail-safe hooks invariant
- Memory `mem_d9931d443cef.md`: 6-17 incident lesson (why telemetry MUST NOT crash the agent)
