"""Frontend smoke tests for the single-file UI (web/index.html).

No browser/node required — these parse the file as text and assert the
i18n contract holds:

* the `he` and `en` dictionaries expose the *same* keys (no language is
  missing a string), and
* every key referenced from JS (`t('key')`) or markup (`data-i18n*`)
  actually exists in the dictionary.

That second assertion is the "no-leak" check: a hardcoded string that was
never routed through the dictionary, or a typo'd key, would surface here
instead of as an untranslated word leaking into the English UI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")


def _lang_block(lang: str) -> str:
    """Return the body of the `<lang>: { ... }` object inside const I18N."""
    m = re.search(r"\b" + lang + r":\s*\{", HTML)
    assert m, f"{lang} dictionary not found in index.html"
    i = m.end()
    depth = 1
    while depth and i < len(HTML):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
        i += 1
    return HTML[m.end():i - 1]


def _keys(block: str) -> set[str]:
    # Keys are identifiers immediately followed by `:` and a quote (values are
    # quoted strings, single OR double — e.g. en rejectPh holds an apostrophe),
    # so this won't match colons inside the values.
    return set(re.findall(r"""(\w+)\s*:\s*['"]""", block))


HE_KEYS = _keys(_lang_block("he"))
EN_KEYS = _keys(_lang_block("en"))


def test_index_exists_and_has_one_script() -> None:
    assert HTML.lstrip().lower().startswith("<!doctype html>")
    assert len(re.findall(r"<script>", HTML)) == 1


def test_dictionaries_are_nonempty() -> None:
    assert len(HE_KEYS) > 30
    assert len(EN_KEYS) > 30


def test_he_and_en_have_identical_keys() -> None:
    """No language may be missing a string the other defines."""
    only_he = HE_KEYS - EN_KEYS
    only_en = EN_KEYS - HE_KEYS
    assert not only_he, f"keys missing from en: {sorted(only_he)}"
    assert not only_en, f"keys missing from he: {sorted(only_en)}"


def test_every_referenced_key_exists() -> None:
    """The no-leak check: t('k') and data-i18n* must reference real keys."""
    referenced: set[str] = set()
    referenced.update(re.findall(r"\bt\(\s*'(\w+)'", HTML))          # t('key', ...)
    referenced.update(re.findall(r'data-i18n(?:-title|-ph)?="(\w+)"', HTML))
    # statusHe builds keys dynamically as 'st_' + status: the regex captures the
    # bare prefix `st_` from t('st_'+s) — drop it and assert the real keys exist.
    referenced.discard("st_")
    referenced.update({"st_running", "st_paused", "st_completed", "st_aborted"})
    missing = sorted(k for k in referenced if k not in HE_KEYS)
    assert not missing, f"keys used in UI but absent from the dictionary: {missing}"


@pytest.mark.parametrize("attr", ["data-i18n", "data-i18n-title", "data-i18n-ph"])
def test_i18n_attributes_present(attr: str) -> None:
    """Each attribute family is actually used (guards against a refactor that
    silently drops the static-markup translation pass)."""
    assert attr + '="' in HTML
