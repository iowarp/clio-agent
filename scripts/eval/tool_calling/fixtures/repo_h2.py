"""H2: ``recstream``, a fixed-size record file format whose reader got slower.

The fourth commit, "Add random access to records", adds ``read_record_at`` and
rewrites ``iter_records`` on top of it, so iteration reopens the file and seeks
once per record instead of streaming buffered chunks. Results stay correct;
only ``bench.py`` shows the slowdown. The last commit, a header check in
``format.py``, is a decoy.
"""

from __future__ import annotations

INIT = '''"""Fixed-size binary records: write them, stream them back."""

__version__ = "1.4.0"
'''

FORMAT_V1 = '''"""On-disk layout: an 8-byte header, then 24-byte records (id, timestamp, value)."""

from __future__ import annotations

import struct

MAGIC = b"RECS0001"
RECORD = struct.Struct("<Iqd4x")
HEADER_SIZE = len(MAGIC)


def pack(record_id: int, timestamp: int, value: float) -> bytes:
    return RECORD.pack(record_id, timestamp, value)


def unpack(blob: bytes) -> tuple[int, int, float]:
    return RECORD.unpack(blob)
'''

FORMAT_V2 = (
    FORMAT_V1
    + '''

class FormatError(ValueError):
    """The file does not start with the record header."""


def check_header(blob: bytes) -> None:
    if blob[:HEADER_SIZE] != MAGIC:
        raise FormatError(f"bad header {blob[:HEADER_SIZE]!r}")
'''
)

WRITER = '''"""Write records to a file."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from recstream.format import MAGIC, pack


def write_records(path: Path, records: Iterable[tuple[int, int, float]]) -> int:
    """Write ``(id, timestamp, value)`` records; return how many were written."""
    count = 0
    with path.open("wb") as stream:
        stream.write(MAGIC)
        for record in records:
            stream.write(pack(*record))
            count += 1
    return count
'''

READER_V1 = '''"""Stream records back from a file."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from recstream.format import HEADER_SIZE, RECORD, unpack

#: Records decoded per read call.
CHUNK_RECORDS = 4096


def record_count(path: Path) -> int:
    return (path.stat().st_size - HEADER_SIZE) // RECORD.size


def iter_records(path: Path) -> Iterator[tuple[int, int, float]]:
    """Yield every record in file order, reading in large chunks."""
    with path.open("rb") as stream:
        stream.seek(HEADER_SIZE)
        while True:
            chunk = stream.read(RECORD.size * CHUNK_RECORDS)
            if not chunk:
                return
            for offset in range(0, len(chunk) - RECORD.size + 1, RECORD.size):
                yield unpack(chunk[offset : offset + RECORD.size])
'''

READER_V2 = '''"""Stream records back from a file, or read one by position."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from recstream.format import HEADER_SIZE, RECORD, unpack


def record_count(path: Path) -> int:
    return (path.stat().st_size - HEADER_SIZE) // RECORD.size


def read_record_at(path: Path, index: int) -> tuple[int, int, float]:
    """The record at position ``index`` (0-based)."""
    with path.open("rb") as stream:
        stream.seek(HEADER_SIZE + index * RECORD.size)
        return unpack(stream.read(RECORD.size))


def iter_records(path: Path) -> Iterator[tuple[int, int, float]]:
    """Yield every record in file order."""
    for index in range(record_count(path)):
        yield read_record_at(path, index)
'''

BENCH = '''"""Time a full read: ``python bench.py [RECORDS]``."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from recstream.reader import iter_records
from recstream.writer import write_records


def main() -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "bench.recs"
        write_records(path, ((i, 1_700_000_000 + i, i * 0.5) for i in range(total)))
        start = time.perf_counter()
        count = sum(1 for _ in iter_records(path))
        elapsed = time.perf_counter() - start
    print(f"read {count} records in {elapsed:.3f}s ({count / elapsed:,.0f} records/s)")


if __name__ == "__main__":
    main()
'''

TEST_ROUNDTRIP_V1 = """import tempfile
import unittest
from pathlib import Path

from recstream.reader import iter_records, record_count
from recstream.writer import write_records


class RoundTripTest(unittest.TestCase):
    def test_roundtrip(self):
        rows = [(i, 1_700_000_000 + i, i / 4) for i in range(10_000)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.recs"
            self.assertEqual(write_records(path, rows), len(rows))
            self.assertEqual(record_count(path), len(rows))
            self.assertEqual(list(iter_records(path)), rows)


if __name__ == "__main__":
    unittest.main()
"""

TEST_ROUNDTRIP_V2 = TEST_ROUNDTRIP_V1.replace(
    "from recstream.reader import iter_records, record_count",
    "from recstream.reader import iter_records, read_record_at, record_count",
).replace(
    "            self.assertEqual(list(iter_records(path)), rows)\n",
    "            self.assertEqual(list(iter_records(path)), rows)\n"
    "            self.assertEqual(read_record_at(path, 1234), rows[1234])\n",
)

README_V1 = """# recstream

Fixed-size binary records (`id`, `timestamp`, `value`) in one file.

- `recstream.writer.write_records(path, rows)` writes them.
- `recstream.reader.iter_records(path)` streams them back.

Tests: `python -m unittest discover -s tests -t .`
"""

README_V2 = (
    README_V1
    + """
## Benchmark

`python bench.py 50000` writes 50,000 records and times reading them all back.
On the reference machine a full read runs at well over a million records per
second.
"""
)

COMMITS: list[tuple[str, dict[str, str | None]]] = [
    (
        "Record format and writer",
        {
            "README.md": README_V1,
            "recstream/__init__.py": INIT,
            "recstream/format.py": FORMAT_V1,
            "recstream/writer.py": WRITER,
            "tests/__init__.py": "",
        },
    ),
    (
        "Buffered record reader",
        {"recstream/reader.py": READER_V1, "tests/test_roundtrip.py": TEST_ROUNDTRIP_V1},
    ),
    ("Add read benchmark", {"bench.py": BENCH, "README.md": README_V2}),
    (
        "Add random access to records",
        {"recstream/reader.py": READER_V2, "tests/test_roundtrip.py": TEST_ROUNDTRIP_V2},
    ),
    ("Validate the record header", {"recstream/format.py": FORMAT_V2}),
]

REGRESSION_INDEX = 3

ANSWER = {
    "root_cause_files": ["recstream/reader.py"],
    "root_cause_functions": ["iter_records", "read_record_at"],
    "decoy_commit_index": 4,
}
