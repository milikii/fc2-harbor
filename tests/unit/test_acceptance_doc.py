"""Every test name cited in docs/acceptance.md must exist.

The acceptance document is evidence; if it points at a test that was renamed or
deleted, the document is lying. This test keeps it honest.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "acceptance.md"
TEST_NAME = re.compile(r"`(?:[a-z_/]+\.py)?(?:::)?([a-z][a-z0-9_]{6,})`")


def all_test_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        names.update(
            re.findall(
                r"^async def (test_[a-z0-9_]+)|^def (test_[a-z0-9_]+)", path.read_text(), re.M
            )
            and [
                n
                for pair in re.findall(r"^(?:async )?def (test_[a-z0-9_]+)", path.read_text(), re.M)
                for n in [pair]
            ]
        )
    return names


def test_acceptance_document_cites_only_existing_tests() -> None:
    text = DOC.read_text(encoding="utf-8")
    existing = all_test_names()
    cited = {match for match in TEST_NAME.findall(text) if match.startswith("test_")}

    assert cited, "acceptance.md cites no tests at all"
    missing = sorted(name for name in cited if name not in existing)
    assert not missing, f"acceptance.md cites tests that do not exist: {missing}"


def test_acceptance_document_covers_all_nineteen_items() -> None:
    text = DOC.read_text(encoding="utf-8")
    for index in range(1, 20):
        assert f"## 24.{index} " in text, f"missing section for item 24.{index}"
