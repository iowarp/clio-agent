"""Largest-Triangle-Three-Buckets (LTTB) downsampling over one numeric series.

LTTB (Steinarsson, 2013) keeps the visually significant shape of a line series
while reducing it to a fixed number of points: the first and last points are
always kept, the interior is split into equal buckets, and each bucket
contributes the point forming the largest triangle with the previously kept
point and the average of the next bucket. Used by the artifact ``table-query``
route to shrink per-entity time series before they are shipped to a chart.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    # Type-only: dspy installs a lazy ``numpy`` module, and importing the
    # ``numpy.typing`` submodule before numpy has fully loaded re-enters numpy's
    # own import (a circular-import ImportError). Annotations are strings here.
    import numpy.typing as npt


def lttb_indices(
    x: npt.ArrayLike,
    y: npt.ArrayLike,
    n_out: int,
) -> npt.NDArray[np.int64]:
    """Return the sorted row indices LTTB keeps when reducing ``(x, y)`` to ``n_out``.

    ``x`` must already be sorted ascending and both arrays must be finite and of
    equal length (the caller drops null/NaN rows first). When ``n_out`` is at
    least the series length every index is returned; ``n_out == 1`` keeps only
    the first point and ``n_out == 2`` keeps both endpoints.

    Raises:
        ValueError: if the arrays differ in length or ``n_out`` is not positive.
    """

    xs = np.asarray(x, dtype=np.float64)
    ys = np.asarray(y, dtype=np.float64)
    if xs.shape != ys.shape or xs.ndim != 1:
        raise ValueError("lttb requires two one-dimensional arrays of equal length")
    if n_out < 1:
        raise ValueError("lttb n_out must be a positive integer")
    n = int(xs.shape[0])
    if n_out >= n:
        return np.arange(n, dtype=np.int64)
    if n_out == 1:
        return np.zeros(1, dtype=np.int64)
    if n_out == 2:
        return np.array([0, n - 1], dtype=np.int64)

    every = (n - 2) / (n_out - 2)
    kept = np.empty(n_out, dtype=np.int64)
    kept[0] = 0
    anchor = 0
    for bucket in range(n_out - 2):
        avg_start = math.floor((bucket + 1) * every) + 1
        avg_end = min(math.floor((bucket + 2) * every) + 1, n)
        if avg_start >= avg_end:
            avg_start, avg_end = n - 1, n
        avg_x = float(xs[avg_start:avg_end].mean())
        avg_y = float(ys[avg_start:avg_end].mean())

        range_start = math.floor(bucket * every) + 1
        range_end = min(math.floor((bucket + 1) * every) + 1, n - 1)
        if range_start >= range_end:
            range_end = range_start + 1
        ax = xs[anchor]
        ay = ys[anchor]
        areas = np.abs(
            (ax - avg_x) * (ys[range_start:range_end] - ay)
            - (ax - xs[range_start:range_end]) * (avg_y - ay)
        )
        anchor = range_start + int(np.argmax(areas))
        kept[bucket + 1] = anchor
    kept[n_out - 1] = n - 1
    return kept


__all__ = ["lttb_indices"]
