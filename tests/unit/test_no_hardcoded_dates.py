"""Guard: tests must not depend on wall-clock dates written as literals.

Two incidents in a row: the static Nyaa RSS fixture carried absolute ``pubDate``
values, and the first-run lookback window (24h, wall clock) silently dropped every
item once the fixture aged out. The fix was to make those dates relative — this test
makes sure the next hard-coded "current year" date fails immediately instead of
turning into a flaky test weeks later.

Rule: no date literal in a year >= this guard's threshold (2026) anywhere under
``tests/`` — neither ``YYYY-MM-DD`` strings nor ``datetime(YYYY, …)`` calls. Use
``datetime.now(UTC) - timedelta(...)`` or rewrite the fixture date to a clearly
historical year.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Dates from this year on are considered "could be mistaken for now".
FIRST_FORBIDDEN_YEAR = 2026

TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Extensions worth scanning: code and fixtures.
SCANNED_SUFFIXES = {".py", ".xml", ".html", ".json", ".txt", ".yaml", ".yml", ".md"}

ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-\d{2}-\d{2}(?!\d)")
DATETIME_CALL_RE = re.compile(r"datetime\(\s*(\d{4})\s*,")


def _offending_lines(path: Path) -> list[str]:
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        years = [int(match.group(1)) for match in ISO_DATE_RE.finditer(line)]
        years += [int(match.group(1)) for match in DATETIME_CALL_RE.finditer(line)]
        if any(year >= FIRST_FORBIDDEN_YEAR for year in years):
            try:
                where = path.relative_to(TESTS_ROOT)
            except ValueError:  # a tmp_path sample outside the repository
                where = path
            problems.append(f"{where}:{number}: {line.strip()}")
    return problems


def test_no_hard_coded_current_year_dates_in_tests() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        offenders.extend(_offending_lines(path))

    assert not offenders, (
        "hard-coded dates from "
        f"{FIRST_FORBIDDEN_YEAR} onwards found — use a relative timestamp "
        "(datetime.now(UTC) - timedelta(...)) or a clearly historical year:\n"
        + "\n".join(offenders)
    )


def test_the_guard_itself_detects_a_planted_date(tmp_path: Path) -> None:
    """The scanner must actually catch something, not pass vacuously."""

    sample = tmp_path / "sample.py"
    # Built from the constant so this file itself carries no forbidden literal.
    year = FIRST_FORBIDDEN_YEAR
    sample.write_text(
        f"stamp = datetime({year}, 11, 14, 10, 0)\n"
        f'when = "{year}-11-14T10:00:00Z"\n',
        encoding="utf-8",
    )
    problems = _offending_lines(sample)
    assert len(problems) == 2


def test_the_guard_ignores_historical_and_relative_dates(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'old = datetime(2020, 11, 14, 10, 0)\n'
        'recent = datetime.now(UTC) - timedelta(hours=1)\n'
        'text = "Sat, 14 Nov 2020 10:00:00 -0000"\n',
        encoding="utf-8",
    )
    assert _offending_lines(sample) == []
