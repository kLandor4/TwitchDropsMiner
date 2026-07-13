"""Tests for webui/translations.py.

Verifies that fork-specific translation keys are merged into the Translator
and that language switching with webui/lang/<language>.json overrides works
without corrupting default_translation.

Expected values are read from the source-of-truth files (the Python dict and
the JSON language files) — not hardcoded — so the tests stay valid when
translations change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import webui.translations  # noqa: side effects
from translate import _, default_translation
from webui.translations import default_webui_translation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_LANG_DIR = PROJECT_ROOT / "webui" / "lang"


def _flatten(d: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    """Yield (path, value) for every leaf in a nested dict."""
    result: list[tuple[tuple[str, ...], str]] = []
    for k, v in d.items():
        path = prefix + (k,)
        if isinstance(v, dict):
            result.extend(_flatten(v, path))
        else:
            result.append((path, v))
    return result


@pytest.fixture
def restore_english():
    """Ensure the Translator is back to English after the test."""
    yield
    _.set_language("English")


@pytest.fixture
def deutsch_override(tmp_path, monkeypatch):
    """Point the webui lang dir at a temp dir with a partial Deutsch override."""
    import webui.translations as wt

    monkeypatch.setattr(wt, "_WEBUI_LANG_DIR", tmp_path)
    (tmp_path / "Deutsch.json").write_text(
        json.dumps({"webui": {"help": {"about": "Über"}}}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# English keys from the Python dict (source of truth)
# ---------------------------------------------------------------------------

_ENGLISH_KEYS = _flatten(default_webui_translation)


@pytest.mark.parametrize(
    "path,expected",
    _ENGLISH_KEYS,
    ids=[".".join(p) for p, _ in _ENGLISH_KEYS],
)
def test_webui_key(path: tuple[str, ...], expected: str):
    """Fork keys from the Python dict are merged into the live Translator."""
    assert _(*path) == expected


# ---------------------------------------------------------------------------
# Language files: every key in each JSON resolves via _()
# ---------------------------------------------------------------------------

_LANG_FILES = sorted(WEBUI_LANG_DIR.glob("*.json"))


@pytest.mark.usefixtures("restore_english")
@pytest.mark.parametrize(
    "lang_file",
    _LANG_FILES,
    ids=[f.stem for f in _LANG_FILES],
)
def test_webui_keys_match_language_file(lang_file: Path):
    """Every key in webui/lang/<language>.json resolves via _() on switch."""
    data = json.loads(lang_file.read_text(encoding="utf-8"))
    _.set_language(lang_file.stem)
    for path, expected in _flatten(data):
        assert _(*path) == expected, f"{'.'.join(path)} in {lang_file.name}"


# ---------------------------------------------------------------------------
# Partial override: overridden keys use the file, others fall back to English
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("restore_english", "deutsch_override")
def test_partial_override_and_fallback():
    """A partial webui/lang/ override applies its keys; others fall back."""
    _.set_language("Deutsch")
    assert _("webui", "help", "about") == "Über"
    assert _("webui", "login", "logout") == "Logout"


@pytest.mark.usefixtures("restore_english", "deutsch_override")
def test_override_doesnt_corrupt_default_translation():
    """A non-English webui/lang/ override must not mutate default_translation."""
    assert default_translation["webui"]["help"]["about"] == "About"

    _.set_language("Deutsch")
    assert _("webui", "help", "about") == "Über"

    _.set_language("English")
    assert default_translation["webui"]["help"]["about"] == "About"
    assert _("webui", "help", "about") == "About"
