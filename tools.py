"""Tool handlers for openviking_extra plugin.

CRITICAL INVARIANTS (from memory mem_40737a8c25ea.md / 6-17 incident post-mortem):

  1. Signature: ``def handler(args: Dict[str, Any], **kwargs) -> str``
     - First line must be: ``if not isinstance(args, dict): args = {}``
     - ``**kwargs`` receives ``task_id`` from the dispatcher
       (tools/registry.py:390-404)
  2. Returns: ``json.dumps(result)`` — NEVER raw dicts
  3. Errors: ``{"error": "msg"}`` — NEVER raise exceptions
     (dispatcher catches + sanitizes, but don't rely on it)
  4. All HTTP calls have timeouts (default 30s)
  5. Never log full responses — could contain user content

These rules exist because the 6-17 incident corrupted the bundled
memory provider's handler signatures and required an audit script
(``audit_handler_signatures.py --all``) to detect. Don't repeat that.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# HTTP client (stdlib only — no extra deps)
# ---------------------------------------------------------------------------

# Standard request timeout (seconds). Don't make it longer — a hung
# OpenViking server will block the agent's tool call.
_REQUEST_TIMEOUT = 30.0


def _endpoint() -> Optional[str]:
    """Return the OpenViking HTTP endpoint from env, or None if unset.

    Same env var as the bundled memory provider plugin. If unset, the
    plugin's check_fn gates all tools off.
    """
    ep = os.environ.get("OPENVIKING_ENDPOINT", "").strip()
    return ep.rstrip("/") if ep else None


def _auth_headers() -> Dict[str, str]:
    """Build the multi-tenant headers OpenViking expects.

    Per OpenViking docs (mem_a7a3082a9904.md + official API): in dev mode,
    OPENVIKING_API_KEY is optional. The X-OpenViking-Account/User/Agent
    headers identify the tenant for multi-tenant deployments.
    """
    headers = {"Content-Type": "application/json"}
    for env, header in (
        ("OPENVIKING_API_KEY", "x-api-key"),
        ("OPENVIKING_AUTHORIZATION", "authorization"),
    ):
        v = os.environ.get(env, "").strip()
        if v:
            headers[header] = v
    for env, header in (
        ("OPENVIKING_ACCOUNT", "X-OpenViking-Account"),
        ("OPENVIKING_USER", "X-OpenViking-User"),
        ("OPENVIKING_AGENT", "X-OpenViking-Agent"),
        ("OPENVIKING_ACTOR_PEER", "X-OpenViking-Actor-Peer"),
    ):
        v = os.environ.get(env, "").strip()
        if v:
            headers[header] = v
    return headers


def _request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Any]:
    """Make a JSON HTTP call to OpenViking.

    Returns:
        (success: bool, body: Any)
        - On success: (True, parsed_json_body)
        - On HTTP error: (False, {"status": code, "error": error_text})
        - On network/timeout: (False, {"error": "msg"})

    NEVER raises. All exceptions caught and converted to error dict.
    """
    endpoint = _endpoint()
    if not endpoint:
        return False, {"error": "OPENVIKING_ENDPOINT not set"}

    url = endpoint + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )

    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body_bytes,
        method=method,
        headers=_auth_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return True, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return True, {"raw": raw}
    except urllib.error.HTTPError as e:
        # Server returned 4xx/5xx. Try to read body for error context.
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
            except json.JSONDecodeError:
                err_json = {"error_text": err_body}
        except Exception:
            err_json = {}
        return False, {"status": e.code, "error": err_json.get("error", str(e))}
    except urllib.error.URLError as e:
        return False, {"error": f"connection failed: {e.reason}"}
    except TimeoutError:
        return False, {"error": f"request timed out after {_REQUEST_TIMEOUT}s"}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


def _to_uri(value: str) -> str:
    """Coerce a user-supplied URI/path to a viking:// URI.

    Accepts: 'viking://...', 'resources/...', absolute paths (rejected).
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    if v.startswith("viking://"):
        return v
    # Allow 'resources/foo.md' → 'viking://resources/foo.md' convenience.
    if v.startswith("resources/") or v.startswith("user/") or v.startswith("agent/"):
        return "viking://" + v
    # Reject absolute paths and other forms.
    return ""


# ---------------------------------------------------------------------------
# Gate: check_fn gates the whole toolset on OpenViking server reachability
# ---------------------------------------------------------------------------

def check_requirements(**kwargs) -> bool:
    """Return True iff the OpenViking server is reachable + endpoint configured.

    Used by the plugin loader to decide whether to expose the tools to the
    LLM at all. If False, tools are silently excluded from the schema.
    """
    if not _endpoint():
        return False
    ok, _ = _request("GET", "/health")
    return ok


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def viking_write(args: Dict[str, Any], **kwargs) -> str:
    """Write text content to a viking:// URI directly.

    Top community request (issue #5627): bypass session-commit lossiness
    by writing exact content to specific URIs.

    Implementation note: this OpenViking server's ``POST /api/v1/content/write``
    requires the file to ALREADY exist (no auto-create in replace mode).
    The proper create-or-replace flow is the 2-step:
        1. POST /api/v1/resources/temp_upload (multipart file upload)
        2. POST /api/v1/resources with {temp_file_id, to=uri, create_parent=True}

    For append mode: parent must exist and file may or may not exist
    (append creates if missing — uses the simpler content/write endpoint).
    """
    if not isinstance(args, dict):
        args = {}
    uri = _to_uri(args.get("uri", ""))
    content = args.get("content", "")
    mode = args.get("mode", "replace")
    wait = bool(args.get("wait", True))

    if not uri:
        return json.dumps({"error": "uri is required and must be a valid viking:// path"})
    if not isinstance(content, str):
        return json.dumps({"error": "content must be a string"})
    if mode not in ("replace", "append"):
        return json.dumps({"error": f"mode must be 'replace' or 'append' (got {mode!r})"})

    if mode == "append":
        # Append path: simpler, uses /api/v1/content/write directly.
        payload = {"uri": uri, "content": content, "mode": "append", "wait": wait}
        ok, body = _request("POST", "/api/v1/content/write", payload)
        if not ok:
            return json.dumps({"error": body.get("error", "append failed"), "details": body})
        return json.dumps({"status": "ok", "uri": uri, "mode": "append", "result": body})

    # mode == "replace": use the 2-step temp_upload + add_resource flow.
    # This auto-creates the file (and any missing parent directories).
    endpoint = _endpoint()
    if not endpoint:
        return json.dumps({"error": "OPENVIKING_ENDPOINT not set"})

    try:
        # Step 1: temp_upload (multipart POST)
        boundary = "----viking_extra_boundary_" + str(os.getpid()) + "_" + str(time.time_ns())
        body_bytes = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="content"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{endpoint}/api/v1/resources/temp_upload",
            data=body_bytes,
            method="POST",
            headers={
                **_auth_headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            upload_result = json.loads(resp.read().decode("utf-8", errors="replace"))
        temp_id = (
            upload_result.get("result", {}).get("temp_file_id")
            if isinstance(upload_result, dict)
            else None
        )
        if not temp_id:
            return json.dumps(
                {"error": "temp_upload returned no temp_file_id",
                 "details": upload_result}
            )

        # Step 2: POST /api/v1/resources with create_parent=True
        add_payload = {
            "temp_file_id": temp_id,
            "to": uri,
            "create_parent": True,
            "wait": wait,
        }
        ok, body = _request("POST", "/api/v1/resources", add_payload)
        if not ok:
            return json.dumps({"error": body.get("error", "add_resource failed"), "details": body})
        return json.dumps({
            "status": "ok",
            "uri": uri,
            "mode": "replace",
            "root_uri": body.get("result", {}).get("root_uri"),
            "result": body,
        })
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            err_json = json.loads(err_body)
        except Exception:
            err_json = {"raw": str(e)}
        return json.dumps({"error": err_json.get("error", str(e)), "details": err_json})
    except urllib.error.URLError as e:
        return json.dumps({"error": f"connection failed: {e.reason}"})
    except TimeoutError:
        return json.dumps({"error": f"upload timed out after {_REQUEST_TIMEOUT}s"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def viking_link(args: Dict[str, Any], **kwargs) -> str:
    """Link two or more viking:// entries to express a relationship.

    Top community request (issue #5627): cross-category linking.
    """
    if not isinstance(args, dict):
        args = {}
    from_uri = _to_uri(args.get("from_uri", ""))
    to_uris_raw = args.get("to_uris", [])
    reason = str(args.get("reason", ""))

    if not from_uri:
        return json.dumps({"error": "from_uri is required"})
    if isinstance(to_uris_raw, str):
        to_uris = [_to_uri(to_uris_raw)]
    elif isinstance(to_uris_raw, list):
        to_uris = [_to_uri(u) for u in to_uris_raw if u]
    else:
        return json.dumps({"error": "to_uris must be a string or list of strings"})

    to_uris = [u for u in to_uris if u]
    if not to_uris:
        return json.dumps({"error": "at least one valid to_uri is required"})

    payload = {"from_uri": from_uri, "to_uris": to_uris, "reason": reason}
    ok, body = _request("POST", "/api/v1/relations/link", payload)
    if not ok:
        return json.dumps({"error": body.get("error", "link failed"), "details": body})
    return json.dumps({
        "status": "ok",
        "from_uri": from_uri,
        "to_uris": to_uris,
        "result": body,
    })


def viking_grep(args: Dict[str, Any], **kwargs) -> str:
    """Exact-text search across viking:// entries (grep semantics).

    Medium community request (issue #5627): complement to semantic search.
    """
    if not isinstance(args, dict):
        args = {}
    uri = _to_uri(args.get("uri", "viking://"))
    pattern = str(args.get("pattern", ""))
    case_insensitive = bool(args.get("case_insensitive", False))
    exclude_uri = _to_uri(args.get("exclude_uri", "")) or None
    node_limit = args.get("node_limit")
    level_limit = int(args.get("level_limit", 5))

    if not pattern:
        return json.dumps({"error": "pattern is required"})

    payload: Dict[str, Any] = {
        "uri": uri,
        "pattern": pattern,
        "case_insensitive": case_insensitive,
        "level_limit": level_limit,
    }
    if exclude_uri:
        payload["exclude_uri"] = exclude_uri
    if node_limit is not None:
        try:
            payload["node_limit"] = int(node_limit)
        except (TypeError, ValueError):
            pass

    ok, body = _request("POST", "/api/v1/search/grep", payload)
    if not ok:
        return json.dumps({"error": body.get("error", "grep failed"), "details": body})
    return json.dumps({"uri": uri, "pattern": pattern, "matches": body})


def viking_glob(args: Dict[str, Any], **kwargs) -> str:
    """Find viking:// entries by name pattern (glob semantics).

    Medium community request (issue #5627): find entries by name pattern.
    """
    if not isinstance(args, dict):
        args = {}
    pattern = str(args.get("pattern", ""))
    uri = _to_uri(args.get("uri", "viking://"))
    node_limit = args.get("node_limit")

    if not pattern:
        return json.dumps({"error": "pattern is required"})

    payload: Dict[str, Any] = {"pattern": pattern, "uri": uri}
    if node_limit is not None:
        try:
            payload["node_limit"] = int(node_limit)
        except (TypeError, ValueError):
            pass

    ok, body = _request("POST", "/api/v1/search/glob", payload)
    if not ok:
        return json.dumps({"error": body.get("error", "glob failed"), "details": body})
    return json.dumps({"uri": uri, "pattern": pattern, "matches": body})


def viking_extract(args: Dict[str, Any], **kwargs) -> str:
    """Trigger OpenViking session memory extraction NOW.

    Medium community request (issue #5627): mid-session extraction so
    long conversations can have memories indexed before session ends.
    """
    if not isinstance(args, dict):
        args = {}
    session_id = str(args.get("session_id", "")).strip()
    if not session_id:
        return json.dumps({"error": "session_id is required"})

    path = f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/extract"
    ok, body = _request("POST", path, {})
    if not ok:
        return json.dumps({"error": body.get("error", "extract failed"), "details": body})
    return json.dumps({"status": "ok", "session_id": session_id, "result": body})


def viking_relation_graph(args: Dict[str, Any], **kwargs) -> str:
    """Build a knowledge graph from linked entities across spaces.

    Surfaces the entity↔project↔event graph from viking_link calls.
    """
    if not isinstance(args, dict):
        args = {}
    space_uris_raw = args.get("space_uris", [])
    output_uri = _to_uri(args.get("output_uri", ""))

    if isinstance(space_uris_raw, str):
        space_uris = [_to_uri(space_uris_raw)]
    elif isinstance(space_uris_raw, list):
        space_uris = [_to_uri(u) for u in space_uris_raw if u]
    else:
        return json.dumps({"error": "space_uris must be a string or list"})
    space_uris = [u for u in space_uris if u]

    if not space_uris:
        return json.dumps({"error": "at least one valid space_uri is required"})
    if not output_uri:
        return json.dumps({"error": "output_uri is required"})

    payload = {"space_uris": space_uris, "output_uri": output_uri}
    ok, body = _request("POST", "/api/v1/relations/build_graph", payload)
    if not ok:
        return json.dumps({"error": body.get("error", "build_graph failed"), "details": body})
    return json.dumps({"status": "ok", "output_uri": output_uri, "result": body})


# Handler registry — name → function. Order matters for the tool list UI.
HANDLERS = {
    "viking_write": viking_write,
    "viking_link": viking_link,
    "viking_grep": viking_grep,
    "viking_glob": viking_glob,
    "viking_extract": viking_extract,
    "viking_relation_graph": viking_relation_graph,
}


# (handler, schema, emoji) tuples — the format expected by
# ~/.hermes/skills/software-development/hermes-user-plugin-authoring/scripts/audit_handler_signatures.py
# (the audit tool that protects against the 6-17 incident). Adding
# schemas here makes the audit see our handlers.
#
# Schemas are imported lazily to avoid a circular import
# (schemas imports nothing from tools, so this is one-way).
_TOOLS = [
    # (handler_fn, schema_dict, emoji)
    (viking_write, None, "✏️"),       # schemas filled in by _init_schemas()
    (viking_link, None, "🔗"),
    (viking_grep, None, "🔍"),
    (viking_glob, None, "📁"),
    (viking_extract, None, "💾"),
    (viking_relation_graph, None, "🕸️"),
]


def _init_schemas() -> None:
    """Build the _TOOLS list from HANDLERS + TOOL_SCHEMAS.

    Called once at module import. Keeps _TOOLS schema references identical
    to HANDLERS (single source of truth — no risk of drift).
    """
    # Local import to avoid circular dependency at module load time.
    from .schemas import TOOL_SCHEMAS
    global _TOOLS
    _TOOLS = [
        (HANDLERS[s["name"]], s, emoji_for(s["name"]))
        for s in TOOL_SCHEMAS
    ]


_EMOJI = {
    "viking_write": "✏️",
    "viking_link": "🔗",
    "viking_grep": "🔍",
    "viking_glob": "📁",
    "viking_extract": "💾",
    "viking_relation_graph": "🕸️",
}


def emoji_for(name: str) -> str:
    return _EMOJI.get(name, "🔧")


_init_schemas()
