"""Measured macOS --desktop first installs before and after A+B (M4 Mac mini, 2026-11-09).

Usage: uv run --no-project --with matplotlib python before_after.py OUT_DIR
"""

import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

out = sys.argv[1]
steps = ["resolve", "download", "verify", "install", "prepare"]
colors = ["#999999", "#4e79a7", "#e15759", "#f28e2b", "#59a14f"]
# Seconds per step; each pair of rows was measured in the same session.
rows = {
    "#1966 DMG\n(session 1, 10.5 MB/s)": [0.0, 52.6, 0.1, 14.5, 40.7],
    "new script, v0.9.5.1 .app.tar.gz\n(session 1, no plan)": [0.5, 54.9, 0.0, 2.4, 45.1],
    "#1966 DMG\n(session 2, 17.2 MB/s)": [0.0, 31.9, 0.1, 14.7, 45.2],
    "new: streamed .aar + plan prefetch\n(session 2, app held to 12 MB/s)": [
        0.1,
        36.7,
        0.0,
        2.6,
        17.3,
    ],
}
fig, ax = plt.subplots(figsize=(10, 4.2))
y = list(range(len(rows)))[::-1]
for values, row in zip(rows.values(), y, strict=True):
    left = 0.0
    for i, value in enumerate(values):
        ax.barh(row, value, left=left, color=colors[i], label=steps[i] if row == y[0] else None)
        left += value
    ax.text(left + 1.5, row, f"{left:.0f} s", va="center", fontsize=9)
ax.set_yticks(y)
ax.set_yticklabels(rows.keys(), fontsize=8)
ax.set_xlabel("seconds, scratch CLIO_DESKTOP_DIR")
ax.set_title("macOS --desktop first install by step, measured", fontsize=10)
ax.legend(ncol=5, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.18))
ax.set_xlim(0, 125)
fig.tight_layout()
fig.savefig(f"{out}/before-after.png", dpi=130)
