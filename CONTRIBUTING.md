# Contributing to openviking_extra

Thanks for your interest in improving this plugin! This guide covers
development setup, the test/lint loop, and the release workflow.

## Repository layout

```
openviking-extra/
├── .github/
│   └── workflows/
│       ├── ci.yml             # lint + audit on every PR
│       └── release.yml        # cut GitHub release on tag push
├── .gitignore
├── CHANGELOG.md               # Keep-a-Changelog format
├── CONTRIBUTING.md            # this file
├── LICENSE                    # MIT
├── README.md                  # usage + architecture
├── plugin.yaml                # plugin manifest (name, version, provides_tools)
├── __init__.py                # register(ctx) — entry point for plugin loader
├── tools.py                   # handler functions + HTTP client + _TOOLS registry
├── schemas.py                 # OpenAI function-calling schemas
├── telemetry.py               # opt-in JSONL logger + post_tool_call hook
└── .github/
```

## Development setup

The plugin lives in two places on disk:

1. **Source repo** — `~/CodingProjects/openviking-extra/` (this repo, git-tracked)
2. **Install** — `~/.hermes/plugins/openviking_extra/` (per-profile `cp -r` for visibility)

### Quick loop

```bash
# 1. Edit source in this repo
$EDITOR ~/CodingProjects/openviking-extra/tools.py

# 2. Sync to all 3 profile install locations
for profile in minimax company-researcher; do
  cp -r ~/CodingProjects/openviking-extra/* ~/.hermes/plugins/openviking_extra/
  cp -r ~/CodingProjects/openviking-extra/* ~/.hermes/profiles/$profile/plugins/openviking_extra/
done

# 3. Restart gateway (handlers are cached in ToolRegistry)
hermes gateway restart

# 4. In a NEW session, the new handlers are loaded
```

### Why both copies

Per Hermes's plugin discovery order (`mem_7be449090808.md` from the Hermes docs):
- Bundled `<repo>/plugins/<name>/` (always available)
- User `~/.hermes/plugins/<name>/` (opt-in via `plugins.enabled`)
- Project `./.hermes/plugins/` (requires `HERMES_ENABLE_PROJECT_PLUGINS=1`)
- Pip entry-points `hermes_agent.plugins`
- Nix `services.hermes-agent.extraPlugins`

`~/.hermes/plugins/` is the **default profile** home. To make the plugin
visible in `minimax` and `company-researcher` profiles, the directory must
be `cp -r`'d into each profile's `plugins/` subdirectory and added to
that profile's `plugins.enabled` list.

## Test / lint loop

Before opening a PR, run these locally:

```bash
# 1. Audit handler signatures (catches the 6-17 incident class of bugs)
~/.hermes/hermes-agent/venv/bin/python3 \
  ~/.hermes/skills/software-development/hermes-user-plugin-authoring/scripts/audit_handler_signatures.py \
  --all

# Must show: 6 safe, 0 suspect, 0 broken for this plugin

# 2. Smoke test the plugin loads + register() runs without exceptions
python3 -c "
import sys, importlib.util
from unittest.mock import MagicMock
sys.path.insert(0, '$(pwd)')
spec = importlib.util.spec_from_file_location('openviking_extra', '__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ctx = MagicMock(); m.register(ctx)
print(f'Registered {ctx.register_tool.call_count} tools, {ctx.register_hook.call_count} hook(s)')
"

# 3. End-to-end smoke test against a live OpenViking server
python3 -c "
import sys, json
sys.path.insert(0, '$(pwd)')
import importlib.util
spec = importlib.util.spec_from_file_location('openviking_extra', '__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
# Test viking_glob (always works, no setup needed)
result = m.tools.viking_glob({'pattern': '*', 'uri': 'viking://resources/'})
parsed = json.loads(result)
assert parsed.get('matches', {}).get('result', {}).get('matches', []) is not None
print('✓ viking_glob smoke test passed')
"
```

CI (`.github/workflows/ci.yml`) runs the audit + plugin-load + smoke test
on every PR.

## Plugin handler contract

Every handler MUST (per `mem_40737a8c25ea.md` / 6-17 incident post-mortem):

1. **Signature**: `def handler(args: Dict[str, Any], **kwargs) -> str`
2. **First executable line**: `if not isinstance(args, dict): args = {}`
3. **Return**: `json.dumps(result)` — NEVER raw dicts
4. **Errors**: `{"error": "msg"}` — NEVER raise exceptions
5. **Timeouts**: all HTTP calls use 30s timeout (no blocking on hung server)

The audit script enforces #1 + #2 automatically. The other three are
review-time checks.

## Telemetry contract

If you add a new tool, also consider:

- The `post_tool_call` hook in `__init__.py` filters by `tool_name.startswith("viking_")`.
  New tools follow this convention automatically.
- Sensitive args should be added to `_SENSITIVE_ARG_FIELDS` in `telemetry.py`
  to prevent large/sensitive payloads from bloating the log.
- Don't log raw prompts — only the tool's own args dict.

## Release workflow

We follow [SemVer](https://semver.org/) (MAJOR.MINOR.PATCH):

- **PATCH** (0.0.X) — bug fixes, no API changes
- **MINOR** (0.X.0) — new tools, new features, backwards-compatible
- **MAJOR** (X.0.0) — breaking changes to existing tool signatures

### Cutting a release

```bash
# 1. Update CHANGELOG.md — move entries from [Unreleased] to a new dated section
$EDITOR CHANGELOG.md

# 2. Bump version in plugin.yaml
$EDITOR plugin.yaml  # change "version: 0.X.Y"

# 3. Commit + tag + push
git add CHANGELOG.md plugin.yaml
git commit -m "chore(release): v0.X.Y"
git tag v0.X.Y
git push origin main --tags

# 4. GitHub Actions auto-creates the release (see release.yml)
gh release view v0.X.Y  # verify
```

GitHub Actions will:
- Run the full CI suite one more time
- Create a GitHub release with auto-generated notes
- Attach the source archive

### For plugin consumers

After a release, users update by:

```bash
cd ~/CodingProjects/openviking-extra && git pull
# Then re-sync to install locations (see "Quick loop" above)
# Then restart gateway
```

## Reporting issues

- **Bugs / feature requests**: open an issue in this repo
- **Upstream Hermes Agent concerns**: open an issue at
  https://github.com/NousResearch/hermes-agent (the bundled memory provider
  plugin lives there — issues like #5627 about missing endpoints should go there)

## Code of conduct

Be excellent to each other. The plugin is small; the goal is to make the
OpenViking surface area useful for every Hermes Agent user.