"""Points on a drawn molecule in a screenshot: strongly coloured pixels, grid-sampled.

Usage: python3 _find_drawn.py shot.png [n]. Prints JSON [[x, y], ...] in image pixels,
most-surrounded first, so a click lands on the drawing rather than on its edge. The
axes gizmo (bottom-left quarter) and the top band (controls) are skipped.
"""

import json
import sys

from PIL import Image

image = Image.open(sys.argv[1]).convert("RGB")
want = int(sys.argv[2]) if len(sys.argv) > 2 else 6
width, height = image.size
pixels = image.load()


def coloured(x: int, y: int) -> bool:
    r, g, b = pixels[x, y]
    return max(r, g, b) - min(r, g, b) > 60


step = 6
hits = []
for y in range(int(height * 0.12), int(height * 0.95), step):
    for x in range(0, width, step):
        if x < width * 0.25 and y > height * 0.5:
            continue  # the axes gizmo
        if coloured(x, y):
            score = sum(
                coloured(min(width - 1, max(0, x + dx)), min(height - 1, max(0, y + dy)))
                for dx in (-4, 0, 4)
                for dy in (-4, 0, 4)
            )
            hits.append((score, x, y))
hits.sort(reverse=True)
chosen: list[tuple[int, int]] = []
for _, x, y in hits:
    if all(abs(x - cx) + abs(y - cy) > 40 for cx, cy in chosen):
        chosen.append((x, y))
    if len(chosen) >= want:
        break
print(json.dumps(chosen))
