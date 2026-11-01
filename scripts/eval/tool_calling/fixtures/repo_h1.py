"""H1 (and H3): ``ledgerlite``, a small ledger package whose tests start failing.

The fourth commit, "Accept European decimal commas in amounts", changes
``parse_amount`` so an amount with a thousands comma and no decimals ("1,250")
parses as 1.25. A report test fails; the last commit, a cosmetic change to
``report.py``, is a decoy. Tests are ``unittest`` style so both
``python -m unittest`` and pytest run them.
"""

from __future__ import annotations

INIT = '''"""Small personal ledger: parse a CSV of entries and report totals."""

__version__ = "0.3.0"
'''

MODELS = '''"""Ledger entry model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Entry:
    """One ledger line."""

    when: date
    category: str
    amount: Decimal
    memo: str = ""

    @property
    def month(self) -> str:
        return f"{self.when.year:04d}-{self.when.month:02d}"
'''

PARSER_V1 = '''"""Parse ledger CSV lines into entries."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ledgerlite.models import Entry


class LedgerFormatError(ValueError):
    """A line that is not a valid ledger entry."""


def parse_amount(text: str) -> Decimal:
    """Parse an amount such as ``1,250.40`` or ``-$12``."""
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise LedgerFormatError(f"not an amount: {text!r}") from exc


def parse_row(row: list[str]) -> Entry:
    """Turn one CSV row (date, category, amount, memo) into an entry."""
    if len(row) < 3:
        raise LedgerFormatError(f"expected at least 3 fields, got {len(row)}")
    year, month, day = (int(part) for part in row[0].split("-"))
    memo = row[3].strip() if len(row) > 3 else ""
    return Entry(date(year, month, day), row[1].strip().lower(), parse_amount(row[2]), memo)


def load_entries(path: Path) -> list[Entry]:
    """Read every entry of a ledger CSV file, skipping the header."""
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    return [parse_row(row) for row in rows[1:] if row and not row[0].startswith("#")]
'''

PARSER_V2 = PARSER_V1.replace(
    """    cleaned = text.strip().replace("$", "").replace(",", "")
""",
    """    cleaned = text.strip().replace("$", "")
    if cleaned.count(",") == 1 and "." not in cleaned:
        # European style: a single comma is the decimal separator ("12,50").
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
""",
).replace(
    '"""Parse an amount such as ``1,250.40`` or ``-$12``."""',
    ('"""Parse an amount such as ``1,250.40``, ``12,50`` or ``-$12``."""'),
)

REPORT_V1 = '''"""Totals by month and by category."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ledgerlite.models import Entry


def monthly_totals(entries: list[Entry]) -> dict[str, Decimal]:
    """Net amount per ``YYYY-MM`` month, in month order."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        totals[entry.month] += entry.amount
    return dict(sorted(totals.items()))


def category_totals(entries: list[Entry]) -> dict[str, Decimal]:
    """Net amount per category, largest spend first."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for entry in entries:
        totals[entry.category] += entry.amount
    return dict(sorted(totals.items(), key=lambda item: item[1]))


def format_report(entries: list[Entry]) -> str:
    """A plain-text report of both tables."""
    lines = ["Month     Total"]
    lines += [f"{month}  {total:>10.2f}" for month, total in monthly_totals(entries).items()]
    lines += ["", "Category  Total"]
    lines += [f"{name:<9} {total:>10.2f}" for name, total in category_totals(entries).items()]
    return "\\n".join(lines)
'''

REPORT_V2 = REPORT_V1.replace(
    """    lines = ["Month     Total"]
    lines += [f"{month}  {total:>10.2f}" for month, total in monthly_totals(entries).items()]
    lines += ["", "Category  Total"]
    lines += [f"{name:<9} {total:>10.2f}" for name, total in category_totals(entries).items()]
    return "\\n".join(lines)
""",
    """    width = 10
    lines = [f"{'Month':<9} {'Total':>{width}}"]
    lines += [f"{month:<9} {total:>{width}.2f}" for month, total in monthly_totals(entries).items()]
    lines += ["", f"{'Category':<9} {'Total':>{width}}"]
    lines += [f"{name:<9} {total:>{width}.2f}" for name, total in category_totals(entries).items()]
    return "\\n".join(lines)
""",
)

CLI = '''"""``python -m ledgerlite.cli LEDGER.csv`` prints the report."""

from __future__ import annotations

import sys
from pathlib import Path

from ledgerlite.parser import load_entries
from ledgerlite.report import format_report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m ledgerlite.cli LEDGER.csv", file=sys.stderr)
        return 2
    print(format_report(load_entries(Path(args[0]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

SAMPLE = """date,category,amount,memo
2026-01-03,rent,"-1,250",January rent
2026-01-09,groceries,-84.20,market
2026-01-15,salary,"3,400.00",payroll
2026-01-21,groceries,-61.75,market
2026-02-03,rent,"-1,250",February rent
2026-02-11,utilities,-96.10,power and water
2026-02-15,salary,"3,400.00",payroll
2026-02-19,groceries,-73.05,market
"""

TEST_PARSER_V1 = """import unittest
from decimal import Decimal

from ledgerlite.parser import LedgerFormatError, parse_amount, parse_row


class ParseAmountTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_amount("12.50"), Decimal("12.50"))

    def test_thousands_and_sign(self):
        self.assertEqual(parse_amount("-$1,250.40"), Decimal("-1250.40"))

    def test_rejects_words(self):
        with self.assertRaises(LedgerFormatError):
            parse_amount("twelve")


class ParseRowTest(unittest.TestCase):
    def test_row(self):
        entry = parse_row(["2026-01-09", " Groceries ", "-84.20", "market"])
        self.assertEqual(entry.category, "groceries")
        self.assertEqual(entry.month, "2026-01")


if __name__ == "__main__":
    unittest.main()
"""

TEST_PARSER_V2 = TEST_PARSER_V1.replace(
    """    def test_rejects_words(self):""",
    """    def test_european_decimal_comma(self):
        self.assertEqual(parse_amount("12,50"), Decimal("12.50"))

    def test_rejects_words(self):""",
)

TEST_REPORT = """import unittest
from decimal import Decimal
from pathlib import Path

from ledgerlite.parser import load_entries
from ledgerlite.report import category_totals, monthly_totals

SAMPLE = Path(__file__).parent / "data" / "sample.csv"


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.entries = load_entries(SAMPLE)

    def test_monthly_totals(self):
        self.assertEqual(
            monthly_totals(self.entries),
            {"2026-01": Decimal("2004.05"), "2026-02": Decimal("1980.85")},
        )

    def test_rent_is_largest_spend(self):
        totals = category_totals(self.entries)
        self.assertEqual(next(iter(totals)), "rent")
        self.assertEqual(totals["rent"], Decimal("-2500"))


if __name__ == "__main__":
    unittest.main()
"""

README_V1 = """# ledgerlite

A small personal ledger. Entries live in a CSV file (`date,category,amount,memo`).

Run the tests with `python -m unittest discover -s tests -t .`.
"""

README_V2 = (
    README_V1
    + """
## Report

`python -m ledgerlite.cli ledger.csv` prints net totals per month, then per
category with the largest spend first.
"""
)

PYPROJECT = """[project]
name = "ledgerlite"
version = "0.3.0"
requires-python = ">=3.10"
"""

#: The commit that introduces the regression (0-based index into COMMITS).
REGRESSION_INDEX = 3

COMMITS: list[tuple[str, dict[str, str | None]]] = [
    (
        "Initial ledger parser and entry model",
        {
            "pyproject.toml": PYPROJECT,
            "README.md": README_V1,
            "ledgerlite/__init__.py": INIT,
            "ledgerlite/models.py": MODELS,
            "ledgerlite/parser.py": PARSER_V1,
            "tests/__init__.py": "",
            "tests/test_parser.py": TEST_PARSER_V1,
        },
    ),
    (
        "Add monthly and category totals",
        {
            "ledgerlite/report.py": REPORT_V1,
            "tests/test_report.py": TEST_REPORT,
            "tests/data/sample.csv": SAMPLE,
        },
    ),
    ("Add command-line report", {"ledgerlite/cli.py": CLI, "README.md": README_V2}),
    (
        "Accept European decimal commas in amounts",
        {"ledgerlite/parser.py": PARSER_V2, "tests/test_parser.py": TEST_PARSER_V2},
    ),
    ("Align report columns", {"ledgerlite/report.py": REPORT_V2}),
]

ANSWER = {
    "root_cause_files": ["ledgerlite/parser.py"],
    "root_cause_functions": ["parse_amount"],
    "failing_tests": [
        "tests/test_report.py::ReportTest::test_monthly_totals",
        "tests/test_report.py::ReportTest::test_rent_is_largest_spend",
    ],
    "decoy_commit_index": 4,
}
