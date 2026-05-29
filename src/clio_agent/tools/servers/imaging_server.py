"""Scientific image inspection tools for small PNG fixtures."""

from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np
from fastmcp import FastMCP
from PIL import Image

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

imaging_server = FastMCP("imaging")


def _component_count(mask: np.ndarray) -> int:
    """Count connected foreground regions in a 2D boolean mask."""
    if mask.ndim != 2 or not mask.any():
        return 0

    seen = np.zeros(mask.shape, dtype=bool)
    height, width = mask.shape
    components = 0

    for row in range(height):
        for col in range(width):
            if not mask[row, col] or seen[row, col]:
                continue
            components += 1
            queue: deque[tuple[int, int]] = deque([(row, col)])
            seen[row, col] = True
            while queue:
                cur_row, cur_col = queue.popleft()
                for next_row, next_col in (
                    (cur_row - 1, cur_col),
                    (cur_row + 1, cur_col),
                    (cur_row, cur_col - 1),
                    (cur_row, cur_col + 1),
                ):
                    if (
                        0 <= next_row < height
                        and 0 <= next_col < width
                        and mask[next_row, next_col]
                        and not seen[next_row, next_col]
                    ):
                        seen[next_row, next_col] = True
                        queue.append((next_row, next_col))
    return components


@imaging_server.tool()
def inspect_png(filepath: str, threshold: int = 32) -> dict[str, Any]:
    """Inspect a PNG image for dimensions, intensity, and foreground regions.

    Agent story: Use this when a user asks whether a microscopy or scientific
    image is suitable for collaborator review, segmentation, or downstream
    analysis.

    Args:
        filepath: Path to a PNG image.
        threshold: Grayscale intensity threshold used to estimate foreground.

    Returns:
        Dictionary with dimensions, image mode, channel count, intensity
        statistics, foreground pixel summary, foreground bounding box, and
        connected-region estimate.
    """
    try:
        safe_path = validate_read_path(filepath)
        threshold = max(0, min(int(threshold or 32), 255))
        with Image.open(safe_path) as image:
            image.load()
            mode = image.mode
            width, height = image.size
            channels = len(image.getbands())
            gray = image.convert("L")
            arr = np.asarray(gray, dtype=np.uint8)

        foreground = arr > threshold
        foreground_count = int(np.count_nonzero(foreground))
        bbox: list[int] = []
        if foreground_count:
            rows, cols = np.where(foreground)
            bbox = [
                int(cols.min()),
                int(rows.min()),
                int(cols.max()),
                int(rows.max()),
            ]

        return {
            "filepath": str(safe_path),
            "format": "PNG",
            "mode": mode,
            "width": width,
            "height": height,
            "channels": channels,
            "threshold": threshold,
            "intensity": {
                "min": int(arr.min()),
                "max": int(arr.max()),
                "mean": float(round(float(arr.mean()), 3)),
                "std": float(round(float(arr.std()), 3)),
            },
            "foreground_pixels": foreground_count,
            "foreground_fraction": float(round(foreground_count / arr.size, 6)),
            "foreground_bbox": bbox,
            "connected_regions": _component_count(foreground),
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
