"""openviking_extra plugin — exposes the 96 OpenViking HTTP endpoints
NOT covered by the bundled memory provider plugin.

Top community requests from NousResearch/hermes-agent#5627 implemented:
- viking_write          (POST /api/v1/content/write) — direct knowledge write
- viking_link           (POST /api/v1/relations/link) — cross-category linking
- viking_grep           (POST /api/v1/search/grep)   — exact text search
- viking_glob           (POST /api/v1/search/glob)   — filename pattern search
- viking_extract        (POST /api/v1/sessions/{id}/extract) — mid-session memory
- viking_relation_graph (POST /api/v1/relations/build_graph) — knowledge graph

Why a plugin (not a skill or MCP server): per Hermes AGENTS.md
constraint #2 ("Core is narrow waist"), the Footprint Ladder ranks:
extend existing code → CLI + skill → service-gated tool → plugin → MCP.
A plugin is the lowest-friction surface that gives Hermes LLM access
without bloating the core tool schema or adding an MCP transport.

Per-profile scope (mem_a869489ad7aa.md): this plugin is installed in
``~/.hermes/plugins/openviking_extra/`` which is the **default** profile
home. Other profiles (``minimax``, ``company-researcher``) need a
``cp -r`` to ``~/.hermes/profiles/<name>/plugins/`` to see these tools.

Handler invariants (mem_40737a8c25ea.md / 6-17 incident post-mortem):
  - signature: (args: Dict[str, Any], **kwargs) -> str
  - first line defensive coerce
  - return json.dumps(...) — NEVER raw dict
  - errors as {"error": "msg"} — NEVER raise

Telemetry (optional, opt-in via plugins.openviking_extra.telemetry_enabled
in config.yaml OR OPENVIKING_EXTRA_TELEMETRY=1 env var): the post_tool_call
hook below writes JSONL to ~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl
for every tool call. Per Hermes AGENTS.md: opt-in gating is required for
any outbound telemetry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from . import telemetry, tools
from .schemas import TOOL_SCHEMAS


# Plugin version — single source of truth is plugin.yaml. Read at import time
# so telemetry.py can record the correct version in JSONL without duplication.
# Fall back to "unknown" if plugin.yaml is missing or unparseable.
def _load_version() -> str:
    try:
        import yaml
        with (Path(__file__).parent / "plugin.yaml").open() as f:
            return yaml.safe_load(f).get("version", "unknown")
    except Exception:
        return "unknown"


__version__ = _load_version()


# Required env vars for the toolset to be loaded at all.
# If any are missing the plugin loader hides all tools silently
# (check_fn returns False).
REQUIRED_ENV = ("OPENVIKING_ENDPOINT",)


def check_requirements(**kwargs) -> bool:
    """Gate the whole toolset on OpenViking server reachability.

    Returns True iff:
      - OPENVIKING_ENDPOINT is set
      - GET /health on that endpoint returns 200

    Tools are silently excluded from the LLM's schema when this returns
    False — Hermes does the gating, not the handler.
    """
    return tools.check_requirements()


def _on_post_tool_call(
    tool_name: str,
    args: dict,
    result: str,
    task_id: str,
    duration_ms: int = 0,
    **kwargs,
) -> None:
    """Hermes post_tool_call hook — fires after every tool call.

    Per mem_42ff1e5958a4.md the signature is:
      post_tool_call(tool_name, args, result, task_id, duration_ms, **kwargs)

    We log only OUR tool calls (viking_*) to avoid polluting the log with
    reads/writes/terminal calls. The handler is wrapped in try/except so a
    logging crash can never break the agent (6-17 invariant: "All hooks
    fail-safe — crash → logged + skipped, never break agent").
    """
    if not tool_name.startswith("viking_"):
        return  # not one of ours
    try:
        # Extract error from result JSON if present (our handlers return
        # {"error": "..."} on failure). Pass-through error kwarg too in case
        # dispatcher already provides one.
        error = kwargs.get("error")
        telemetry.record(
            tool=tool_name,
            args=args,
            result=result,
            task_id=task_id,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception:
        # Telemetry must never crash the agent. Swallow.
        pass


def register(ctx) -> None:
    """Called once by the plugin loader at gateway startup.

    For each (handler, schema, emoji) in tools._TOOLS, register the
    matching tool as a Hermes core tool in the ``openviking_extra`` toolset.
    The ``check_fn`` gate runs once at registration time — if OpenViking
    is unreachable, all tools in this set are silently excluded.

    Also wires the ``post_tool_call`` hook for opt-in telemetry (gated by
    config.yaml: plugins.openviking_extra.telemetry_enabled).

    Tools come from tools._TOOLS (the (handler, schema, emoji) tuples that
    match the format expected by
    ~/.hermes/skills/software-development/hermes-user-plugin-authoring/scripts/audit_handler_signatures.py
    — the audit that protects against the 6-17 incident).
    """
    # Opt-in telemetry: pull from config.yaml if present.
    # The loader doesn't pass us the config directly, so we read it here.
    # (Reading ~/.hermes/config.yaml is safe — it's the user's own config.)
    try:
        import yaml
        cfg_path = None
        # Try per-profile config first (where plugins.enabled actually is)
        for candidate in (
            Path.home() / ".hermes" / "config.yaml",
        ):
            if candidate.exists():
                cfg_path = candidate
                break
        if cfg_path:
            with cfg_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            telemetry_enabled = (
                cfg.get("plugins", {})
                  .get("openviking_extra", {})
                  .get("telemetry_enabled", False)
            )
            telemetry.set_enabled(bool(telemetry_enabled))
    except Exception:
        # If config read fails, telemetry stays disabled (default OFF).
        pass

    for handler, schema, emoji in tools._TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="openviking_extra",
            schema=schema,
            handler=handler,
            check_fn=check_requirements,
            requires_env=list(REQUIRED_ENV),
            is_async=False,
            emoji=emoji,
        )

    # Wire the post_tool_call hook (no-op unless telemetry_enabled).
    ctx.register_hook("post_tool_call", _on_post_tool_call)

    # Optional: register a bundled skill documenting the toolset.
    # Currently disabled — adding a skill adds ~3K tokens to system
    # prompt's <available_skills> index and the schemas.py descriptions
    # are sufficient for LLM tool selection. Uncomment if John wants
    # the skill surfaced.
    #
    # from pathlib import Path
    # _SKILL_PATH = Path(__file__).parent / "SKILL.md"
    # if _SKILL_PATH.exists():
    #     ctx.register_skill(
    #         name="openviking_extra",
    #         path=str(_SKILL_PATH),
    #         description=(
    #             "Direct HTTP access to OpenViking endpoints NOT in the "
    #             "bundled memory provider. 6 tools: write, link, grep, "
    #             "glob, extract, relation_graph."
    #         ),
    #     )
