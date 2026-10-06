"""Contact sheets retain selected pages without replacing detailed review images."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image

from clio_agent.runtime.document_stack.contact_sheets import make_contact_sheets


def test_sparse_pages_are_grouped_in_order_and_original_images_survive(tmp_path: Path) -> None:
    """A seven-page mixed-orientation selection produces two bounded overview sheets."""
    pages = [2, 4, 9, 10, 11, 18, 20]
    colours = ["red", "green", "blue", "orange", "purple", "black", "yellow"]
    images: list[dict[str, Any]] = []
    originals: dict[Path, bytes] = {}
    for index, (number, colour) in enumerate(zip(pages, colours, strict=True)):
        path = tmp_path / f"page-{number:04d}.png"
        size = (900, 600) if index % 2 else (600, 900)
        with Image.new("RGB", size, colour) as image:
            image.save(path)
        originals[path] = path.read_bytes()
        images.append({"page": number, "path": str(path)})

    sheets = make_contact_sheets(images, tmp_path)

    assert [sheet["pages"] for sheet in sheets] == [pages[:6], pages[6:]]
    for index, sheet in enumerate(sheets):
        path = Path(sheet["path"])
        assert sheet["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        with Image.open(path) as image:
            assert image.size == (1008, 2320 if index == 0 else 784)
            assert image.width * image.height < 3_000_000
    with Image.open(sheets[0]["path"]) as overview:
        # Left/right cells retain the corresponding source colour, without cropping.
        assert overview.getpixel((250, 100)) == (255, 0, 0)
        assert overview.getpixel((750, 100)) == (0, 128, 0)
        assert overview.getpixel((750, 1650)) == (0, 0, 0)
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_empty_selection_creates_no_sheet(tmp_path: Path) -> None:
    """No overview is invented when the render has no pages."""
    before = set(tmp_path.iterdir())
    assert make_contact_sheets([], tmp_path) == []
    assert set(tmp_path.iterdir()) == before
