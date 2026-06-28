# openviking_extra — Hermes plugin exposing uncovered OpenViking endpoints

## Why

The bundled `openviking` memory provider plugin in Hermes exposes **11 of 114** OpenViking HTTP API endpoints. The community has been asking for the rest ([issue #5627](https://github.com/NousResearch/hermes-agent/issues/5627)). This plugin covers the top 6 community-requested gaps as core Hermes tools.

## What's included

| Tool | OpenViking API | Community priority | Use case |
|---|---|---|---|
| `viking_write` | `POST /api/v1/resources/temp_upload` + `POST /api/v1/resources` (2-step with `create_parent=True`) | High | Write exact content to a `viking://` URI without going through session-commit extraction (which can be lossy) |
| `viking_link` | `POST /api/v1/relations/link` | High | Cross-category relations (entity ↔ project ↔ event) |
| `viking_grep` | `POST /api/v1/search/grep` | Medium | Exact text search complement to the bundled semantic search |
| `viking_glob` | `POST /api/v1/search/glob` | Medium | Filename pattern search across `viking://` namespaces |
| `viking_extract` | `POST /api/v1/sessions/{id}/extract` | Medium | Mid-session memory extraction (don't wait for session end) |
| `viking_relation_graph` | `POST /api/v1/relations/build_graph` | Bonus | Knowledge graph from linked entities across spaces |

## Why a plugin (not a skill or MCP)

Per Hermes AGENTS.md constraint #2 ("Core is narrow waist"), the Footprint Ladder ranks:

1. Extend existing code
2. CLI command + skill
3. Service-gated tool (check_fn)
4. Plugin
5. MCP server in catalog
6. New core tool (last resort)

A plugin is the **lowest-friction surface** that gives Hermes LLM access without bloating the core tool schema or adding an MCP transport.

## Architecture

4-file structure (per `mem_7be449090808.md` recommended layout):

```
openviking_extra/
├── plugin.yaml    # manifest (name, kind, version, description, requires_env)
├── __init__.py    # def register(ctx): ... — called by plugin loader at startup
├── schemas.py     # OpenAI function-calling schemas (vocab-true LLM-facing descriptions)
└── tools.py       # handler functions + HTTP client + helpers
```

## Handler contract (CRITICAL)

Every handler **MUST** (per `mem_40737a8c25ea.md` / 6-17 incident post-mortem):

1. **Signature**: `def handler(args: Dict[str, Any], **kwargs) -> str`
2. **First executable line**: `if not isinstance(args, dict): args = {}`
3. **Return**: `json.dumps(result)` — NEVER a raw dict
4. **Errors**: `{"error": "msg"}` — NEVER raise exceptions
5. **Timeouts**: all HTTP calls use 30s timeout (no blocking on hung server)

These rules are NOT aspirational — they exist because the 6-17 incident broke the bundled memory provider with wrong signatures and required an audit script (`audit_handler_signatures.py --all`) to detect.

## Configuration

Reads from environment (same vars as the bundled memory provider):

```bash
export OPENVIKING_ENDPOINT="http://127.0.0.1:1933"   # default in ~/.hermes/.env
export OPENVIKING_ACCOUNT="default"                   # multi-tenant
export OPENVIKING_USER="default"
export OPENVIKING_AGENT="hermes"                     # identifies the actor peer
# Optional, for remote auth:
export OPENVIKING_API_KEY="sk-..."
```

`check_requirements()` calls `GET /health` and gates all tools on success. If OpenViking is unreachable, **all tools are silently excluded** from the LLM's schema (Hermes's plugin loader does this).

## Per-profile scope (mem_a869489ad7aa.md)

Installed at `~/.hermes/plugins/openviking_extra/`. Visible in `default` profile. To enable in other profiles (e.g. `minimax` or `company-researcher`):

```bash
# Whole-directory copy, NOT symlink of individual files (plugin's
# register_skill uses relative paths)
cp -r ~/.hermes/plugins/openviking_extra/ ~/.hermes/profiles/<profile>/plugins/

# Then add to that profile's config.yaml:
#   plugins:
#     enabled:
#       - openviking_extra

# Restart the profile's gateway to pick up the new plugin
```

## Verification

```bash
# Lint: every handler must satisfy the contract
python3 -c "
import sys, importlib.util, inspect
sys.path.insert(0, '~/.hermes/plugins')
spec = importlib.util.spec_from_file_location('openviking_extra',
    '~/.hermes/plugins/openviking_extra/__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import m.tools as t
for name in ('viking_write','viking_link','viking_grep','viking_glob',
             'viking_extract','viking_relation_graph'):
    h = getattr(t, name)
    sig = list(inspect.signature(h).parameters.keys())
    assert sig == ['args','kwargs'], f'{name}: bad sig {sig}'
    body = inspect.getsource(h)
    assert 'isinstance(args, dict)' in body, f'{name}: missing coerce'
    assert 'json.dumps' in body, f'{name}: missing json.dumps'
print('All 6 handlers pass contract')
"

# Smoke test: write + grep + glob round-trip
python3 -c "
import sys, json, uuid, time
sys.path.insert(0, '~/.hermes/plugins')
import importlib.util
spec = importlib.util.spec_from_file_location('openviking_extra',
    '~/.hermes/plugins/openviking_extra/__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
uri = f'viking://resources/test/openviking_extra_smoke_{uuid.uuid4().hex[:8]}.md'
marker = uuid.uuid4().hex
r = m.tools.viking_write({'uri': uri, 'content': f'# {marker}\\n'})
assert json.loads(r).get('status') == 'ok'
time.sleep(1)
r = m.tools.viking_grep({'uri': uri, 'pattern': marker})
matches = json.loads(r).get('matches', {}).get('result', {}).get('matches', [])
assert matches, 'grep did not find marker'
print('Smoke test passed')
"
```

## Known limitations (as of 2026-06-28)

1. **`viking_write` uses 2-step temp_upload + add_resource** because this OpenViking server's `POST /api/v1/content/write` requires the file to already exist in `replace` mode. If the server fixes this in a future version, the handler can be simplified.
2. **The 114-endpoint OpenViking API surface is only ~5% covered** by this plugin (6 of 114). The remaining 108 endpoints can be added as needed — submit an issue or PR.
3. **Read-only mode for some endpoints** — the plugin doesn't expose `DELETE /api/v1/admin/accounts/{id}` or other destructive ops. The 6 tools included are all safe by default.
4. **No streaming** — `viking_extract` waits for completion. For very long sessions, use `wait=False` and poll separately.

## Files

- `plugin.yaml` — manifest with `requires_env: [OPENVIKING_ENDPOINT]`
- `__init__.py` — `register(ctx)` + `check_requirements()` + `REQUIRED_ENV` constant
- `schemas.py` — 6 OpenAI function-calling schemas (~10K chars total)
- `tools.py` — 6 handlers + HTTP client + URI helpers (~13K chars)

## See also

- OpenViking docs: https://docs.openviking.ai/en/agent-integrations/05-hermes
- Hermes plugin authoring: https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- Issue #5627: https://github.com/NousResearch/hermes-agent/issues/5627
- Memory `mem_7be449090808.md`: Plugin auto-wire surface (17+ integration points)
- Memory `mem_40737a8c25ea.md`: Hermes core frozen + handler signature invariants
- Memory `mem_a869489ad7aa.md`: Per-profile plugin visibility
