"""Guard against a whole class of runtime failure.

Every `livekit.plugins.*` package calls `Plugin.register_plugin()` at import
time, and that raises `RuntimeError("Plugins must be registered on the main
thread")` unless the import happens on the main thread. Job runners execute in
worker threads, so a plugin import hidden inside a function that a job calls
will blow up at runtime - but only once a call actually starts, which makes it
easy to miss.

This test enforces the rule statically: plugin imports live at module scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "voice_agent"


def _function_scoped_plugin_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and (child.module or "").startswith(
                "livekit.plugins"
            ):
                offenders.append(f"{node.name}: from {child.module} import ...")
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith("livekit.plugins"):
                        offenders.append(f"{node.name}: import {alias.name}")
    return offenders


def test_no_plugin_imports_inside_functions():
    failures: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = _function_scoped_plugin_imports(tree)
        if offenders:
            failures[str(path.relative_to(SRC))] = offenders

    assert not failures, (
        "livekit.plugins imports must be at module scope (they register on "
        f"import and require the main thread). Found: {failures}"
    )
