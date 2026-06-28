"""Tool schemas for openviking_extra plugin.

Each schema is an OpenAI function-calling dict that the LLM sees when
deciding whether/how to call the tool. Keep descriptions concise but
specific so the LLM picks the right tool.

Schema fields (per Hermes tools/registry.py convention):
    name        - tool name (must match ctx.register_tool(name=...))
    description - when to call this tool (vocab-true phrasing)
    parameters  - JSON Schema for the arguments dict
"""

# ---------------------------------------------------------------------------
# viking_write — direct content write to a viking:// URI
# ---------------------------------------------------------------------------
# Top community request (issue #5627): bypass session-commit lossiness by
# writing exact content to specific URIs.
VIKING_WRITE_SCHEMA = {
    "name": "viking_write",
    "description": (
        "Write text content directly to a file at a viking:// URI. "
        "Bypasses session-commit extraction, so the content is stored "
        "verbatim (no LLM rewriting). Use when you need precise storage "
        "of code snippets, structured notes, or any content where fidelity "
        "matters. For atomic facts/preferences, use viking_remember instead. "
        "Requires the URI to be in a writable space (viking://resources/... "
        "or viking://user/memories/...)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": (
                    "Target viking:// URI. Must be in a writable namespace "
                    "(viking://resources/<path>, viking://user/memories/<path>)."
                ),
            },
            "content": {
                "type": "string",
                "description": "Exact text content to write (UTF-8).",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "default": "replace",
                "description": (
                    "replace overwrites the file; append adds to the end. "
                    "create is implicit if file doesn't exist (replace mode)."
                ),
            },
            "wait": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Whether to wait for vector indexing to complete. "
                    "Default true; set false for bulk writes where you'll "
                    "wait separately."
                ),
            },
        },
        "required": ["uri", "content"],
    },
}


# ---------------------------------------------------------------------------
# viking_link — create relations between viking:// entries
# ---------------------------------------------------------------------------
# Top community request (issue #5627): cross-category linking.
# E.g. "Yang Zhi works on Trend Intelligence System" → link person to project.
VIKING_LINK_SCHEMA = {
    "name": "viking_link",
    "description": (
        "Link two or more viking:// entries to express a relationship "
        "(e.g. an entity to a project, a person to an event, a pattern to "
        "a case). Improves retrieval quality for queries that span "
        "categories. Without explicit links, every memory is siloed in "
        "its category directory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_uri": {
                "type": "string",
                "description": "Source URI (e.g. viking://user/memories/entities/<name>).",
            },
            "to_uris": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Target URI(s) to link from_uri to. Accepts a single "
                    "string or a list."
                ),
            },
            "reason": {
                "type": "string",
                "default": "",
                "description": (
                    "Human-readable explanation of the relationship "
                    "(stored alongside the link for retrieval context)."
                ),
            },
        },
        "required": ["from_uri", "to_uris"],
    },
}


# ---------------------------------------------------------------------------
# viking_grep — exact text search across viking:// entries
# ---------------------------------------------------------------------------
# Medium community request (issue #5627): complement to semantic search.
# Useful for finding exact terms, file names, error messages.
VIKING_GREP_SCHEMA = {
    "name": "viking_grep",
    "description": (
        "Exact-text search across viking:// entries (grep semantics). "
        "Use when you know the exact term, function name, error message, or "
        "phrase to find — vector search may rank it low. Returns matching "
        "nodes with line-level snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "default": "viking://",
                "description": "Root URI to search under (e.g. viking://resources/).",
            },
            "pattern": {
                "type": "string",
                "description": "Text pattern to search for (grep-compatible regex).",
            },
            "case_insensitive": {
                "type": "boolean",
                "default": False,
                "description": "If true, match case-insensitively.",
            },
            "exclude_uri": {
                "type": "string",
                "description": "Optional URI subtree to exclude from results.",
            },
            "node_limit": {
                "type": "integer",
                "description": "Max matching nodes to return (default: server default).",
            },
            "level_limit": {
                "type": "integer",
                "default": 5,
                "description": "Max directory depth to traverse.",
            },
        },
        "required": ["uri", "pattern"],
    },
}


# ---------------------------------------------------------------------------
# viking_glob — filename pattern search
# ---------------------------------------------------------------------------
# Medium community request (issue #5627): find entries by name pattern.
VIKING_GLOB_SCHEMA = {
    "name": "viking_glob",
    "description": (
        "Find viking:// entries by name pattern (glob semantics). Use when "
        "you know roughly what a file is called but not where it lives in "
        "the hierarchy. E.g. pattern='*decision*.md' finds all decision "
        "memos across the resource tree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. '*audit*', '2026-*.md').",
            },
            "uri": {
                "type": "string",
                "default": "viking://",
                "description": "Root URI to search under.",
            },
            "node_limit": {
                "type": "integer",
                "description": "Max matching nodes to return.",
            },
        },
        "required": ["pattern"],
    },
}


# ---------------------------------------------------------------------------
# viking_extract — manual session memory extraction
# ---------------------------------------------------------------------------
# Medium community request (issue #5627): mid-session extraction so
# long-running conversations can have memories indexed BEFORE session ends.
VIKING_EXTRACT_SCHEMA = {
    "name": "viking_extract",
    "description": (
        "Trigger OpenViking to extract and index memories from a session "
        "NOW (not just at session end). Use during long conversations when "
        "you want memories to be searchable mid-session. Requires an "
        "existing session_id — usually the current session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "OpenViking session ID to extract from.",
            },
        },
        "required": ["session_id"],
    },
}


# ---------------------------------------------------------------------------
# viking_relation_graph — build a knowledge graph from linked entities
# ---------------------------------------------------------------------------
# Surfaces the entity↔project↔event graph that viking_link creates.
# Useful for "show me everything connected to X" queries.
VIKING_RELATION_GRAPH_SCHEMA = {
    "name": "viking_relation_graph",
    "description": (
        "Build a knowledge graph from the relations (viking_link calls) "
        "across multiple viking:// spaces. Returns nodes and edges for "
        "visualization or further traversal. Use to answer 'what's "
        "connected to X?' queries across categories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "space_uris": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of viking:// space URIs to include in the graph "
                    "(e.g. ['viking://user/memories/entities/', "
                    "'viking://user/memories/events/'])."
                ),
            },
            "output_uri": {
                "type": "string",
                "description": (
                    "Where to save the generated graph (e.g. "
                    "'viking://resources/graphs/2026-06-28.md')."
                ),
            },
        },
        "required": ["space_uris", "output_uri"],
    },
}


# Ordered list for plugin registration
TOOL_SCHEMAS = [
    VIKING_WRITE_SCHEMA,
    VIKING_LINK_SCHEMA,
    VIKING_GREP_SCHEMA,
    VIKING_GLOB_SCHEMA,
    VIKING_EXTRACT_SCHEMA,
    VIKING_RELATION_GRAPH_SCHEMA,
]
