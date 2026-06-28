"""CI plugin load test — extracted to a file to avoid YAML escaping issues.

Run from anywhere; uses importlib to find the openviking_extra package
relative to this script's location.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock


def find_package() -> Path | None:
    """Walk up from this file to find openviking_extra/__init__.py.

    Returns the package directory, or None if not found.
    """
    start = Path(__file__).resolve().parent
    # Check current dir first, then parents
    for candidate in [start, *start.parents]:
        pkg = candidate / "openviking_extra" / "__init__.py"
        if pkg.exists():
            return candidate / "openviking_extra"
    # Last resort: rglob from cwd
    for p in Path.cwd().rglob("__init__.py"):
        if p.parent.name == "openviking_extra":
            return p.parent
    return None


def main() -> int:
    pkg_dir = find_package()
    if pkg_dir is None:
        print("✗ Could not find openviking_extra package", file=sys.stderr)
        return 1
    print(f"Found package at: {pkg_dir}")
    sys.path.insert(0, str(pkg_dir.parent))

    try:
        import openviking_extra
    except Exception as e:
        print(f"✗ Import failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(f"Loaded: {openviking_extra.__version__}")
    ctx = MagicMock()
    openviking_extra.register(ctx)
    print(f"Registered: {ctx.register_tool.call_count} tools, {ctx.register_hook.call_count} hooks")
    if ctx.register_tool.call_count != 6:
        print(f"✗ Expected 6 tools, got {ctx.register_tool.call_count}", file=sys.stderr)
        return 3
    if ctx.register_hook.call_count != 1:
        print(f"✗ Expected 1 hook, got {ctx.register_hook.call_count}", file=sys.stderr)
        return 4
    print("✓ Plugin loads correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())