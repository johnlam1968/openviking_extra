"""Telemetry logger for openviking_extra plugin.

Writes one JSONL record per tool call to ~/.hermes/logs/openviking_extra/
when the user opts in via `plugins.openviking_extra.telemetry_enabled: true`
in their Hermes config.

JSONL format (one record per line, parseable with jq / pandas):
    {
      "ts": "2026-06-28T03:15:42-04:00",       # ISO8601 localtime
      "tool": "viking_write",                  # tool name (viking_*)
      "task_id": "abc-123-...",                 # Hermes dispatcher task_id
      "duration_ms": 142,                       # tool call duration
      "args": {"uri": "...", "content": "..."}, # truncated args (sensitive fields redacted)
      "result_status": "ok" | "error",          # parsed from handler JSON output
      "result_summary": "first 200 chars...",   # truncated for grep-ability
      "error": "404 NOT_FOUND ...",             # present only when status=error
      "plugin_version": "0.3.0"                 # from plugin.yaml at load time
    }

Design notes:
- **Opt-in only.** Default behavior = NO TELEMETRY. The hook short-circuits
  before any file I/O if the user hasn't enabled it. Per Hermes AGENTS.md
  "Outbound telemetry / usage attribution without opt-in gating" — not
  acceptable to ship.
- **One file per day.** Path: ~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl
  Matches the pdf-integrity-watchdog pattern (one file per run, dated).
- **Best-effort writes.** If the log file can't be written (disk full, perms,
  read-only filesystem), the error is swallowed — telemetry must never break
  the plugin (per 6-17 invariant: "All hooks fail-safe — crash → logged +
  skipped, never break agent").
- **Sensitive data redaction.** Arg fields with name 'content' are truncated
  to 200 chars (logs contain too much data otherwise). Other arg fields pass
  through. If a tool ever handles secrets (e.g. credentials), expand this
  redaction list.
- **No raw prompt context** is logged — only the tool's own args dict.

This module is intentionally simple (one file, ~100 lines). If the schema
grows beyond ~5 fields, split into telemetry/{schema.py,writer.py}.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Plugin version — try to import the authoritative value from the package
# __init__ (single source of truth = plugin.yaml). Fall back to a hardcoded
# value if import fails (e.g. telemetry called before plugin load).
def _get_plugin_version() -> str:
    """Return the plugin's authoritative version string.

    Tries multiple strategies in order:
    1. Use __package__ + importlib to import the parent package
       (works when loaded as part of a real package via `import`)
    2. Use sys.modules lookup for the already-loaded parent package
       (works when __init__ has already been imported)
    3. Read plugin.yaml directly from disk (always works, even when
       telemetry is loaded standalone for testing)
    4. Fall back to a hardcoded value as last resort
    """
    # Strategy 1 + 2: try to find the loaded parent package
    parent_pkg_name = (__package__ or "").rsplit(".", 1)[0] if __package__ else ""
    if parent_pkg_name and parent_pkg_name in sys.modules:
        return getattr(sys.modules[parent_pkg_name], "__version__", "unknown")
    # Also try common names directly
    for candidate in ("openviking_extra", __package__):
        if candidate and candidate in sys.modules:
            mod = sys.modules[candidate]
            ver = getattr(mod, "__version__", None)
            if ver:
                return ver
    # Strategy 3: read plugin.yaml directly
    try:
        import yaml
        yaml_path = Path(__file__).parent / "plugin.yaml"
        if yaml_path.exists():
            with yaml_path.open() as f:
                return yaml.safe_load(f).get("version", "unknown")
    except Exception:
        pass
    return "0.3.0"  # keep in sync with plugin.yaml


_PLUGIN_VERSION = _get_plugin_version()

# Per Hermes AGENTS.md "What we don't want" #6: outbound telemetry without
# opt-in gating. We default OFF and require explicit config.yaml enable.
_DEFAULT_ENABLED = False

# Arg fields that may contain sensitive or bulky content. Truncated to
# _MAX_ARG_FIELD_CHARS. Add to this list as new sensitive args appear.
_SENSITIVE_ARG_FIELDS = frozenset({"content", "api_key", "password", "token", "secret"})
_MAX_ARG_FIELD_CHARS = 200

# Per-tool-call log record fields cap (prevents a huge result from bloating
# the log). The result is JSON, so it's already structured — truncation is
# for grep-ability not storage cost.
_MAX_RESULT_SUMMARY_CHARS = 200

# Daily rotation: file path uses YYYY-MM-DD.jsonl
_LOG_DIR = Path.home() / ".hermes" / "logs" / "openviking_extra"


# ---------------------------------------------------------------------------
# Config gate
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """Return True iff telemetry is enabled for this plugin.

    Three-tier check (most-specific wins):
    1. Env var OPENVIKING_EXTRA_TELEMETRY=1 (highest priority — useful for cron jobs)
    2. config.yaml: plugins.openviking_extra.telemetry_enabled: true
       (looked up by the plugin loader at register() time; cached in _CACHED)
    3. Default OFF

    Per Hermes AGENTS.md: "Outbound telemetry / usage attribution without
    opt-in gating" → no analytics until user-facing opt-in exists.
    """
    # Tier 1: env var override
    env_val = os.environ.get("OPENVIKING_EXTRA_TELEMETRY", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False

    # Tier 2: cached config value (set by set_enabled() at register time)
    return _CACHED.get("enabled", _DEFAULT_ENABLED)


_CACHED: Dict[str, Any] = {}


def set_enabled(enabled: bool) -> None:
    """Called by register() with the config.yaml value (if present).

    Plugin loader calls this once before checking is_enabled() so the
    config-file value takes effect for the lifetime of the gateway.
    """
    _CACHED["enabled"] = bool(enabled)


# ---------------------------------------------------------------------------
# Log file management
# ---------------------------------------------------------------------------

def _log_path(now: Optional[datetime] = None) -> Path:
    """Return today's log file path. Auto-creates the parent dir."""
    now = now or datetime.now().astimezone()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"


def _truncate(value: Any, max_chars: int) -> Any:
    """Truncate string values to max_chars. Non-strings pass through."""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"...[+{len(value)-max_chars} chars]"
    return value


def _redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of args with sensitive fields truncated."""
    if not isinstance(args, dict):
        return {"_unparseable": str(args)[:_MAX_ARG_FIELD_CHARS]}
    out = {}
    for k, v in args.items():
        if k in _SENSITIVE_ARG_FIELDS:
            out[k] = _truncate(v, _MAX_ARG_FIELD_CHARS)
        else:
            out[k] = v
    return out


def _parse_result_status(result: str) -> tuple[str, str]:
    """Try to extract status/error from a handler's JSON output.

    Returns (status, error_msg). status is "ok" | "error" | "unknown".
    """
    if not isinstance(result, str):
        return ("unknown", "")
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            if "error" in parsed:
                err = parsed["error"]
                # err may be a string OR a dict (e.g. {"code": "...", "message": "..."})
                if isinstance(err, dict):
                    return ("error", err.get("message", str(err))[:200])
                return ("error", str(err)[:200])
            if parsed.get("status") == "ok":
                return ("ok", "")
        return ("unknown", "")
    except (json.JSONDecodeError, ValueError):
        return ("unknown", "")


# ---------------------------------------------------------------------------
# Public API: called by post_tool_call hook
# ---------------------------------------------------------------------------

def record(
    tool: str,
    args: Dict[str, Any],
    result: str,
    task_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Log one tool call to today's JSONL. No-op if disabled or write fails.

    Called by the post_tool_call hook in __init__.py. MUST be safe to call
    in the hot path — exceptions swallowed, all failures silent.
    """
    if not is_enabled():
        return

    # If the dispatcher passed us an error string (exception caught), use it.
    # Otherwise parse result JSON to detect {"error": "..."} returns.
    if error is None:
        status, err_from_result = _parse_result_status(result)
        if status == "error":
            error = err_from_result
            record_status = "error"
        else:
            record_status = "ok" if status == "ok" else "unknown"
    else:
        record_status = "error"

    record_obj: Dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(),
        "tool": tool,
        "task_id": task_id or "",
        "duration_ms": duration_ms if duration_ms is not None else 0,
        "args": _redact_args(args),
        "result_status": record_status,
        "result_summary": _truncate(result, _MAX_RESULT_SUMMARY_CHARS),
        "plugin_version": _PLUGIN_VERSION,
    }
    if error:
        record_obj["error"] = _truncate(error, _MAX_RESULT_SUMMARY_CHARS)

    try:
        path = _log_path()
        # Append-mode write with explicit encoding. Atomic on most filesystems
        # for short writes (< PIPE_BUF = 4096 bytes, our case).
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record_obj, ensure_ascii=False) + "\n")
    except Exception:
        # Swallow — telemetry must NEVER break the agent.
        # Optional: print to stderr for debugging, but stay silent in prod.
        pass


# ---------------------------------------------------------------------------
# Helpers (used by the companion skill to analyze logs)
# ---------------------------------------------------------------------------

def today_log_path() -> Path:
    """Public accessor for today's log path. Used by the analysis skill."""
    return _log_path()
