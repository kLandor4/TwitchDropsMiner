"""Verify every _() call in webui/ resolves to an existing translation key.

AST-scans all .py files under webui/ for calls to _(...) where all arguments
are string literals, then resolves each path against the merged translation
dict (upstream + fork patches). Any missing key fails the test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import webui.translations  # noqa: side-effect: merges fork strings into _
from translate import _

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"


def _find_translation_calls(tree: ast.AST) -> list[tuple[str, ...]]:
    """Return all _(literal, literal, ...) call paths found in *tree*."""
    paths: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_"):
            continue
        args: list[str] = []
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                args.append(arg.value)
            else:
                break
        if args and len(args) == len(node.args):
            paths.append(tuple(args))
    return paths


def _collect_all_paths() -> list[tuple[str, tuple[str, ...]]]:
    """Scan all .py files under webui/ for _() calls with literal args."""
    results: list[tuple[str, tuple[str, ...]]] = []
    for pyfile in sorted(WEBUI_DIR.rglob("*.py")):
        tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
        for path in _find_translation_calls(tree):
            results.append((pyfile.relative_to(PROJECT_ROOT).as_posix(), path))
    return results


_ALL_PATHS = _collect_all_paths()


@pytest.mark.parametrize(
    "file,path",
    _ALL_PATHS,
    ids=[f"{f}:{'.'.join(p)}" for f, p in _ALL_PATHS],
)
def test_translation_key_exists(file: str, path: tuple[str, ...]) -> None:
    """Every _() call with literal args must resolve to an existing key."""
    try:
        _(*path)
    except Exception as exc:
        pytest.fail(f"{file}: _({', '.join(repr(a) for a in path)}) -> {exc}")


def test_at_least_some_calls_found() -> None:
    """Sanity check: the scan found _() calls (guards against a broken AST scan)."""
    assert len(_ALL_PATHS) > 20, f"Expected 20+ _() calls, found {len(_ALL_PATHS)}"
