# Changelog

All notable changes to this project are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), version numbers follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned
- More endpoints from issue #5627 (admin/*, observer/*, pack/*)
- Streaming responses for viking_extract
- Auto-rotation of JSONL logs (logrotate integration)

## [0.3.0] - 2026-06-28

### Added
- **Telemetry**: opt-in `post_tool_call` hook writes JSONL to
  `~/.hermes/logs/openviking_extra/YYYY-MM-DD.jsonl`. Gated by
  `plugins.openviking_extra.telemetry_enabled: true` in `~/.hermes/config.yaml`
  OR `OPENVIKING_EXTRA_TELEMETRY=1` env var. Default OFF (per Hermes AGENTS.md
  "Outbound telemetry without opt-in gating").
- **`telemetry.py`** module: fail-safe writes (never crash the agent per 6-17
  invariant), sensitive arg redaction (`content`/`api_key`/`password`/`token`/`secret`
  truncated to 200 chars), daily rotation, plugin version baked into each record.
- **Companion skill** `~/.hermes/skills/devops/openviking-extra-telemetry/` with
  `analyze_telemetry.py` — turns JSONL into actionable improvement suggestions
  (error rates, latency percentiles, recurring failure patterns, unused tools).

### Changed
- **`tools._TOOLS`** structure now matches the company_db pattern so
  `audit_handler_signatures.py --all` sees the handlers. All 6 pass audit
  (6 safe, 0 suspect, 0 broken).
- **`register()`** now reads `~/.hermes/config.yaml` at load time to honor
  `telemetry_enabled` setting. Wires `ctx.register_hook("post_tool_call", ...)`.
- **Version source-of-truth** moved to `plugin.yaml`; `__init__.py` reads it at
  import time so JSONL records match the actually-loaded version.

## [0.2.0] - 2026-06-28

### Changed
- Documentation corrected: was claiming "96 endpoints", actually "6 of 96".

## [0.1.0] - 2026-06-28

### Added
- **Initial release**. 6 tools exposing OpenViking HTTP endpoints NOT covered by
  the bundled memory provider plugin:
  - `viking_write` — POST /api/v1/content/write (via 2-step temp_upload +
    add_resource because OpenViking's replace mode requires pre-existing file)
  - `viking_link` — POST /api/v1/relations/link
  - `viking_grep` — POST /api/v1/search/grep
  - `viking_glob` — POST /api/v1/search/glob
  - `viking_extract` — POST /api/v1/sessions/{id}/extract
  - `viking_relation_graph` — POST /api/v1/relations/build_graph
- Per-tool `check_fn` gates all 6 on OpenViking server reachability.
- Stdlib-only (no extra dependencies beyond what Hermes already pulls in).
- Tested end-to-end against OpenViking v0.4.4 server.

[Unreleased]: https://github.com/johnlam1968/openviking-extra/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/johnlam1968/openviking-extra/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/johnlam1968/openviking-extra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/johnlam1968/openviking-extra/releases/tag/v0.1.0