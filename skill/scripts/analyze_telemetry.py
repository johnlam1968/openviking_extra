#!/usr/bin/env python3
"""Analyze openviking_extra plugin telemetry and produce improvement report.

Reads JSONL records from ~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl
and produces:

  1. **Volume stats**: per-tool call counts, error rates, p50/p95 latency
  2. **Failure patterns**: recurring error messages that suggest bug fixes
  3. **Usage patterns**: which tools are underused, which are overused
  4. **Suggested improvements**: concrete plugin changes based on the data

Usage:
    python3 analyze_telemetry.py                    # all available days
    python3 analyze_telemetry.py --days 7          # last 7 days
    python3 analyze_telemetry.py --since 2026-06-25 # from a specific date
    python3 analyze_telemetry.py --json             # machine-readable output

Exit codes:
    0 = no anomalies (or only informational output)
    1 = anomalies detected (review the suggestions)
    2 = error (file not found, etc.)

This is the "continuous improvement" loop closure for the plugin:
    production usage → JSONL log → this script → suggested fixes → plugin PR
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# Default log directory — matches where telemetry.py writes
LOG_DIR = Path.home() / ".hermes" / "logs" / "openviking_extra"

# Thresholds for "anomaly" detection (suggested improvements trigger at these)
MIN_CALLS_FOR_LATENCY_ALERT = 10
P95_LATENCY_MS_ALERT = 5000      # 5s p95 = caller is probably hitting timeouts
ERROR_RATE_ALERT = 0.20          # 20% error rate = something is systematically broken


def collect_records(days: int = 30, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read all JSONL records from the last N days (or since a date)."""
    if not LOG_DIR.exists():
        return []

    if since:
        try:
            cutoff = datetime.fromisoformat(since)
        except ValueError:
            print(f"ERROR: --since must be ISO date (YYYY-MM-DD), got {since!r}", file=sys.stderr)
            sys.exit(2)
    else:
        cutoff = datetime.now() - timedelta(days=days)
    # Normalize cutoff to UTC-naive so we can compare against both naive and
    # aware timestamps from record['ts'] (which may or may not have tzinfo).
    if cutoff.tzinfo is not None:
        cutoff = cutoff.replace(tzinfo=None)

    records: List[Dict[str, Any]] = []
    # Iterate date-stamped log files from cutoff to today
    today = datetime.now().date()
    for n in range((today - cutoff.date()).days + 1):
        day = today - timedelta(days=n)
        log_file = LOG_DIR / f"{day.isoformat()}.jsonl"
        if not log_file.exists():
            continue
        try:
            with log_file.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Skip corrupt lines; don't crash on partial writes
                        pass
        except OSError as e:
            print(f"WARN: couldn't read {log_file}: {e}", file=sys.stderr)

    # Also filter by record timestamp (in case log file dates are stale)
    records = [
        r for r in records
        if _parse_ts(r.get("ts", "")) >= cutoff
    ]
    return records


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO8601 timestamp; fall back to epoch 0 if malformed.

    Returns a timezone-NAIVE datetime. The cutoff used for filtering is
    always naive (datetime.now() - timedelta), so we strip tzinfo from
    any parsed value to enable comparison.
    """
    try:
        dt = datetime.fromisoformat(ts_str)
        # Strip tzinfo so we can compare against naive cutoff. ISO8601 timestamps
        # from telemetry are already in local time (timezone-aware), so dropping
        # the offset doesn't change the instant being represented.
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0)


def compute_volume_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-tool volume, error rate, latency percentiles."""
    by_tool: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_tool[r.get("tool", "unknown")].append(r)

    stats = {}
    for tool, recs in sorted(by_tool.items()):
        total = len(recs)
        errors = sum(1 for r in recs if r.get("result_status") == "error")
        latencies = [r.get("duration_ms", 0) for r in recs if r.get("duration_ms")]
        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        stats[tool] = {
            "total_calls": total,
            "errors": errors,
            "error_rate": round(errors / total, 3) if total else 0,
            "p50_ms": p50,
            "p95_ms": p95,
        }
    return stats


def find_failure_patterns(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find recurring error messages worth fixing."""
    # Normalize error messages: strip URI/UUID numbers so "404 on foo.md"
    # and "404 on bar.md" cluster together
    error_msgs: Counter[str] = Counter()
    error_examples: Dict[str, str] = {}
    for r in records:
        if r.get("result_status") != "error":
            continue
        err = r.get("error", "")
        if not err:
            continue
        # Normalize: replace URIs, numbers, UUIDs with placeholders
        norm = re.sub(r"viking://[^\s'\"]+", "<URI>", err)
        norm = re.sub(r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b", "<UUID>", norm)
        norm = re.sub(r"\b\d+\b", "<N>", norm)
        norm = norm[:120]  # cap at 120 chars for grouping
        error_msgs[norm] += 1
        if norm not in error_examples:
            error_examples[norm] = err[:200]

    return [
        {"pattern": pattern, "count": count, "example": error_examples[pattern]}
        for pattern, count in error_msgs.most_common(10)
    ]


def suggest_improvements(
    stats: Dict[str, Any],
    failures: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Turn patterns into concrete plugin-improvement suggestions.

    Returns a dict with two lists:
      - "all": every suggestion (informational + actionable), for the human report
      - "anomalies": only actionable ones (error rate, latency, recurring failures),
        for the cron exit-code. The "💤 unused" suggestions are NOT actionable —
        they're informational only, since a brand-new install always has zero calls.

    Per the cron contract (mem_d9931d443cef.md / cron-script-only skill): exit 0 = silent
    tick, exit 1 = anomaly alert. We only fire exit 1 when there are real anomalies
    (suggest["anomalies"] is non-empty).
    """
    anomalies: List[str] = []
    informational: List[str] = []

    # 1. High error rate (actionable)
    for tool, s in stats.items():
        if s["total_calls"] >= MIN_CALLS_FOR_LATENCY_ALERT and s["error_rate"] >= ERROR_RATE_ALERT:
            anomalies.append(
                f"⚠️  {tool}: {s['error_rate']*100:.0f}% error rate "
                f"({s['errors']}/{s['total_calls']}). Investigate."
            )

    # 2. Slow latency (actionable)
    for tool, s in stats.items():
        if s["total_calls"] >= MIN_CALLS_FOR_LATENCY_ALERT and s["p95_ms"] >= P95_LATENCY_MS_ALERT:
            anomalies.append(
                f"🐌  {tool}: p95 latency {s['p95_ms']}ms. "
                f"May need streaming, batching, or parallel calls."
            )

    # 3. Recurring failures (actionable)
    for f in failures[:3]:
        if f["count"] >= 3:
            anomalies.append(
                f"🔁  Recurring failure ({f['count']}x): "
                f"`{f['pattern'][:80]}` "
                f"— example: `{f['example'][:100]}`"
            )

    # 4. Underused tools (INFORMATIONAL — never alert-worthy on its own,
    # because a fresh install always has zero calls in window)
    expected_tools = {
        "viking_write", "viking_link", "viking_grep", "viking_glob",
        "viking_extract", "viking_relation_graph",
    }
    used_tools = set(stats.keys())
    unused = expected_tools - used_tools
    for tool in unused:
        informational.append(
            f"💤  {tool}: zero calls in window. "
            f"Either unused (consider deprecating) or schema needs better vocab."
        )

    return {"all": anomalies + informational, "anomalies": anomalies}


def format_brief(
    stats: Dict[str, Any],
    anomalies: List[str],
    days: int,
    total_records: int,
) -> str:
    """Short Telegram-friendly message (3-5 lines max).

    Used by the daily cron when anomalies are present. Designed to fit in
    one Telegram bubble without scrolling.
    """
    lines = []
    lines.append(f"📊 openviking_extra telemetry ({days}d, {total_records} calls)")

    # Top 3 anomalies
    for a in anomalies[:3]:
        # Strip emoji prefix to keep lines short
        clean = a.split("  ", 1)[-1] if "  " in a else a
        lines.append(f"  • {clean[:120]}")

    more = len(anomalies) - 3
    if more > 0:
        lines.append(f"  • ...and {more} more")

    return "\n".join(lines)


def format_report(
    stats: Dict[str, Any],
    failures: List[Dict[str, Any]],
    suggestions: List[str],
    days: int,
    total_records: int,
) -> str:
    """Human-readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"openviking_extra telemetry report (last {days} days, {total_records} records)")
    lines.append("=" * 70)
    lines.append("")

    lines.append("## Volume by tool")
    lines.append("")
    lines.append(f"  {'tool':<25} {'calls':>6} {'errors':>7} {'err_rate':>9} {'p50_ms':>8} {'p95_ms':>8}")
    lines.append("  " + "-" * 65)
    for tool, s in sorted(stats.items()):
        lines.append(
            f"  {tool:<25} {s['total_calls']:>6} {s['errors']:>7} "
            f"{s['error_rate']*100:>8.1f}% {s['p50_ms']:>8} {s['p95_ms']:>8}"
        )
    if not stats:
        lines.append("  (no calls recorded)")

    lines.append("")
    lines.append("## Top recurring failures")
    lines.append("")
    if failures:
        for f in failures:
            lines.append(f"  [{f['count']}x] {f['pattern']}")
            lines.append(f"       e.g. {f['example'][:100]}")
    else:
        lines.append("  (no errors recorded)")

    lines.append("")
    lines.append("## Suggested improvements")
    lines.append("")
    if suggestions:
        for s in suggestions:
            lines.append(f"  {s}")
    else:
        lines.append("  ✓ No anomalies detected. Plugin behaving as expected.")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-improvement: open GitHub issues for real anomalies
# ---------------------------------------------------------------------------

# Hard caps (per the user's "no human review" preference + guardrails from
# mem_d9931d443cef.md). Configurable via CLI flags.
DEFAULT_MAX_ISSUES_PER_RUN = 5
DEFAULT_DEDUP_WINDOW_DAYS = 7
GITHUB_REPO = "johnlam1968/openviking_extra"


def _anomaly_key(anomaly: str) -> Optional[str]:
    """Extract a tool-level dedup key from an anomaly string.

    Returns the tool name (e.g. "viking_write") if the anomaly is about
    a specific tool, or None for cross-tool anomalies.

    All anomalies from a single tool in one cron run collapse to the same
    key — so we file ONE issue per tool per day, regardless of how many
    metrics triggered.
    """
    # Anomalies look like:
    #   "⚠️  viking_write: 33% error rate (5/15). Investigate."
    #   "🐌  viking_write: p95 6900ms latency. May need..."
    #   "🔁  Recurring failure (5x): <pattern> — example: ..."
    # The first two name a tool directly; the third doesn't, so we can't
    # attribute it to a single tool. For now, treat each recurring failure
    # as its own issue (different patterns ARE different issues).
    clean = " ".join(anomaly.strip().split())
    for emoji in ("⚠️", "🐌", "🔁"):
        clean = clean.replace(emoji, "").strip()
    if clean.startswith("Recurring"):
        # No tool name in this format — use the pattern as the key
        # (different patterns = different issues, which is correct)
        return f"recurring:{clean[:80]}"
    # First token after the emoji is the tool name
    parts = clean.split(":")
    if len(parts) >= 2:
        return f"tool:{parts[0].strip()}"
    return None


def _issue_title_for(tool_or_key: str, anomalies: List[str]) -> str:
    """Build a deterministic issue title for a consolidated set of anomalies.

    One issue per tool (or per recurring-pattern key), with title reflecting
    the consolidated cause. Body contains all the individual anomaly strings.

    IMPORTANT: titles must be SHORT (≤80 chars) and STABLE across runs so
    GitHub's `--search 'in:title <title>'` reliably matches for dedup.
    Long titles (especially with backticks/quotes) get truncated or
    normalized by GitHub's search, breaking dedup.
    """
    if tool_or_key.startswith("tool:"):
        tool = tool_or_key.removeprefix("tool:")
        return f"[auto] {tool}: anomaly detected"
    elif tool_or_key.startswith("recurring:"):
        # For recurring failures, use a short stable hash of the pattern
        # so different patterns produce different titles (preserving the
        # "different pattern = different issue" semantics) while staying
        # short enough that GitHub's title search matches reliably.
        import hashlib
        # Use the first anomaly string as the dedup key for the pattern
        pattern_source = anomalies[0] if anomalies else tool_or_key
        # Strip just enough to get the actual error pattern (between the
        # backticks that delimit it in the original anomaly string)
        import re
        match = re.search(r"`([^`]+)`", pattern_source)
        pattern = match.group(1) if match else pattern_source
        # Take first 32 chars + short hash (8 hex chars) for stable differentiation
        snippet = pattern[:32].replace('"', "'").replace("\n", " ")
        # Use a 6-char hash of the FULL pattern to differentiate
        # `{\"code\": \"X\"}` from `{\"code\": \"Y\"}` even if first 32 chars match
        pattern_hash = hashlib.sha256(pattern.encode()).hexdigest()[:6]
        return f"[auto] recurring failure: {snippet}…[{pattern_hash}]"
    return f"[auto] {tool_or_key}: anomaly detected"


def _issue_body_for(tool_or_key: str, anomalies: List[str],
                    failures: List[Dict[str, Any]],
                    stats: Dict[str, Any]) -> str:
    """Build a Markdown body with all the detected anomalies.

    Body includes:
    - The tool name (or pattern key) being flagged
    - Each individual anomaly string (one per bullet)
    - Diagnostic context (analyzer source, cadence, repo URL)
    - Recovery hints
    """
    if tool_or_key.startswith("tool:"):
        scope = f"Tool: `{tool_or_key.removeprefix('tool:')}`"
    else:
        scope = f"Pattern: `{tool_or_key.removeprefix('recurring:')}`"

    parts = [
        "## Auto-detected anomalies",
        "",
        scope,
        "",
        "The telemetry analyzer detected the following on the same "
        f"{'tool' if tool_or_key.startswith('tool:') else 'pattern'}:",
        "",
    ]
    for a in anomalies:
        parts.append(f"- {a.strip()}")

    parts.extend([
        "",
        "## Source",
        "",
        "- Detector: `~/.hermes/skills/devops/openviking-extra-telemetry/scripts/analyze_telemetry.py`",
        "- Cadence: daily cron `openviking-extra-telemetry-daily`",
        f"- GitHub: https://github.com/{GITHUB_REPO}",
        "",
        "## What this means",
        "",
        "Filed automatically by the telemetry analyzer. Multiple metrics on "
        "the same scope were triggered in one run (error rate, latency, "
        "and/or recurring failure) — they're grouped here because they "
        "usually share a single root cause.",
        "",
        "**To act**: fix in `~/CodingProjects/openviking_extra/` (or the "
        "installed copy under `~/.hermes/plugins/openviking_extra/`), then "
        "close this issue. Dedup window is 7 days per tool/pattern.",
        "",
        "**If false positive**: close as `not planned` — the dedup window "
        "will still suppress re-fires for 7 days.",
    ])
    return "\n".join(parts)


def _local_recent_filing_check(title: str, minutes: int = 60) -> bool:
    """Defensive dedup: check if we filed THIS title recently in a local state file.

    GitHub's search index has eventual consistency — there can be a window
    where two consecutive cron runs within seconds both miss each other. To
    prevent this, we also track filings in a local JSONL file and skip if
    we've seen the same title within `minutes`.

    This is a fast local check (no network) that complements the GitHub-side
    dedup. It can be cleared manually if needed:
        rm ~/.hermes/logs/openviking_extra/.recent_filings.jsonl
    """
    state_file = LOG_DIR / ".recent_filings.jsonl"
    if not state_file.exists():
        return False
    try:
        cutoff = datetime.now().timestamp() - (minutes * 60)
        with state_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("title") == title:
                        ts = datetime.fromisoformat(
                            entry["ts"].replace("Z", "+00:00")
                        ).timestamp()
                        if ts > cutoff:
                            return True
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return False
    except OSError:
        return False


def _record_local_filing(title: str, url: str) -> None:
    """Append a filing record to the local state file for fast dedup."""
    state_file = LOG_DIR / ".recent_filings.jsonl"
    try:
        with state_file.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "title": title,
                "url": url,
            }) + "\n")
    except OSError:
        pass  # Best-effort — failing this shouldn't break the cron


def _dedup_check(title: str, gh_repo: str, window_days: int) -> bool:
    """Return True if an open or recently-closed issue with this title exists.

    Uses TWO checks in order:
      1. Local state file (fast, ~no latency) — catches the case where we
         filed this title minutes/hours ago and GitHub's search index
         hasn't caught up yet. Covers the race condition between rapid
         cron runs.
      2. GitHub search (slower, but authoritative) — covers all other cases.

    Closed issues older than `window_days` are ignored so recurring
    patterns can re-fire after the window passes.

    Fail-safe: if either check errors, return True (assume duplicate). Better
    to miss one issue than to spam the repo with duplicates.
    """
    # Local check first (catches GitHub search race conditions)
    if _local_recent_filing_check(title, minutes=60):
        return True

    try:
        # Open issues
        proc = subprocess.run(
            ["gh", "issue", "list", "--repo", gh_repo,
             "--state", "open", "--search", f'in:title "{title}"',
             "--json", "number,state", "--limit", "5"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            existing = json.loads(proc.stdout)
            if existing:
                return True

        # Recently-closed (within window)
        proc = subprocess.run(
            ["gh", "issue", "list", "--repo", gh_repo,
             "--state", "closed", "--search", f'in:title "{title}"',
             "--json", "number,state,closedAt", "--limit", "20"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            closed = json.loads(proc.stdout)
            cutoff = datetime.now().timestamp() - (window_days * 86400)
            for issue in closed:
                closed_at = issue.get("closedAt", "")
                if closed_at:
                    try:
                        closed_ts = datetime.fromisoformat(
                            closed_at.replace("Z", "+00:00")
                        ).timestamp()
                        if closed_ts > cutoff:
                            return True
                    except (ValueError, TypeError):
                        continue
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  WARNING: dedup check failed ({e!r}); assuming duplicate", file=sys.stderr)
        return True


def open_issues_for_anomalies(
    anomalies: List[str],
    failures: List[Dict[str, Any]],
    stats: Dict[str, Any],
    gh_repo: str = GITHUB_REPO,
    max_issues: int = DEFAULT_MAX_ISSUES_PER_RUN,
    dedup_window_days: int = DEFAULT_DEDUP_WINDOW_DAYS,
) -> List[Dict[str, str]]:
    """Open GitHub issues for real anomalies, grouped by tool/pattern.

    Consolidation rule: multiple anomalies on the SAME tool collapse to
    ONE issue (different metrics on the same tool usually share a root
    cause). Recurring failures are filed individually by pattern, since
    different patterns ARE different issues.

    Hard caps + dedup window apply. Returns a list of
    {"title", "url", "status"} dicts.
    """
    filed: List[Dict[str, str]] = []

    # Group anomalies by dedup key
    grouped: Dict[str, List[str]] = {}
    for a in anomalies:
        key = _anomaly_key(a)
        if key is None:
            continue  # unparseable anomaly — skip
        grouped.setdefault(key, []).append(a)

    for key, key_anomalies in grouped.items():
        if len(filed) >= max_issues:
            print(f"  cap reached ({max_issues}); stopping", file=sys.stderr)
            break

        title = _issue_title_for(key, key_anomalies)
        body = _issue_body_for(key, key_anomalies, failures, stats)

        if _dedup_check(title, gh_repo, dedup_window_days):
            print(f"  skip (dedup): {title[:80]}", file=sys.stderr)
            continue

        try:
            cmd = [
                "gh", "issue", "create", "--repo", gh_repo,
                "--title", title,
                "--body", body,
            ]
            for label in ("auto-improvement", "telemetry-anomaly"):
                cmd.extend(["--label", label])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if proc.returncode == 0:
                url = proc.stdout.strip().splitlines()[-1] if proc.stdout else ""
                filed.append({
                    "title": title, "url": url,
                    "status": f"filed (consolidated {len(key_anomalies)} metrics)",
                })
                _record_local_filing(title, url)
                print(f"  filed: {title[:80]}", file=sys.stderr)
            elif "could not add label" in proc.stderr:
                # Retry without labels
                print(f"  retry (label missing): {title[:80]}", file=sys.stderr)
                proc2 = subprocess.run(
                    ["gh", "issue", "create", "--repo", gh_repo,
                     "--title", title, "--body", body],
                    capture_output=True, text=True, timeout=30,
                )
                if proc2.returncode == 0:
                    url = proc2.stdout.strip().splitlines()[-1] if proc2.stdout else ""
                    filed.append({
                        "title": title, "url": url,
                        "status": f"filed (no labels, consolidated {len(key_anomalies)} metrics)",
                    })
                    _record_local_filing(title, url)
                    print(f"  filed (no labels): {title[:80]}", file=sys.stderr)
                else:
                    filed.append({
                        "title": title, "url": "",
                        "status": f"failed (retry): {proc2.stderr.strip()[:100]}",
                    })
                    print(f"  failed (retry): {proc2.stderr.strip()[:200]}", file=sys.stderr)
            else:
                filed.append({
                    "title": title, "url": "",
                    "status": f"failed: {proc.stderr.strip()[:100]}",
                })
                print(f"  failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            filed.append({"title": title, "url": "", "status": "timeout"})
            print(f"  timeout: {title[:80]}", file=sys.stderr)

    return filed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=30,
                        help="Number of days to analyze (default: 30)")
    parser.add_argument("--since", type=str, default=None,
                        help="ISO date (YYYY-MM-DD); overrides --days")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON instead of text")
    parser.add_argument("--brief", action="store_true",
                        help="Output a 3-5 line Telegram-friendly message "
                             "(used by the daily cron job)")
    parser.add_argument("--open-issues", action="store_true",
                        help="Open GitHub issues for detected anomalies (auto-improvement). "
                             "Use --max-issues and --dedup-days to tune caps.")
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES_PER_RUN,
                        help=f"Max issues per run (default: {DEFAULT_MAX_ISSUES_PER_RUN})")
    parser.add_argument("--dedup-days", type=int, default=DEFAULT_DEDUP_WINDOW_DAYS,
                        help=f"Skip re-firing same pattern within N days (default: {DEFAULT_DEDUP_WINDOW_DAYS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --open-issues, print what would be filed but don't actually open issues")
    args = parser.parse_args()

    records = collect_records(days=args.days, since=args.since)
    stats = compute_volume_stats(records)
    failures = find_failure_patterns(records)
    sugg = suggest_improvements(stats, failures)
    all_suggestions = sugg["all"]
    anomalies = sugg["anomalies"]

    if args.json:
        output: Dict[str, Any] = {
            "window_days": args.days,
            "total_records": len(records),
            "volume": stats,
            "failures": failures,
            "suggestions": all_suggestions,
            "anomalies": anomalies,
        }
        if args.open_issues:
            grouped_keys: Dict[str, List[str]] = {}
            for a in anomalies:
                key = _anomaly_key(a)
                if key:
                    grouped_keys.setdefault(key, []).append(a)
            output["would_file"] = [
                {
                    "title": _issue_title_for(k, v),
                    "scope": k,
                    "consolidated_anomalies": v,
                }
                for k, v in grouped_keys.items()
            ]
        print(json.dumps(output, indent=2))
    elif args.open_issues:
        # File issues mode. Print brief FIRST (for Telegram delivery),
        # then do the filing (separately handled so brief always shows
        # regardless of filing results).
        print(format_brief(stats, anomalies, args.days, len(records)))
        if args.dry_run:
            print("\n[--dry-run] Would file:")
            grouped_keys = {}
            for a in anomalies:
                key = _anomaly_key(a)
                if key:
                    grouped_keys.setdefault(key, []).append(a)
            for k, v in grouped_keys.items():
                print(f"  - {_issue_title_for(k, v)}  (consolidates {len(v)} metrics)")
        else:
            print("\nFiling:")
            filed_results = open_issues_for_anomalies(
                anomalies, failures, stats,
                max_issues=args.max_issues,
                dedup_window_days=args.dedup_days,
            )
            for r in filed_results:
                print(f"  [{r['status']}] {r['title'][:70]} {r.get('url', '')}")
    elif args.brief:
        # Brief-only mode (no filing). Used for ad-hoc human inspection.
        print(format_brief(stats, anomalies, args.days, len(records)))
    else:
        # Pass all_suggestions to format_report so the human report is unchanged
        print(format_report(stats, failures, all_suggestions, args.days, len(records)))

    # Exit 1 ONLY when real anomalies exist (per cron-script-only contract).
    # "💤 unused" warnings are informational — they don't trigger alerts.
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
