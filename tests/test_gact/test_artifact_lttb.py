"""Unit tests for the LTTB downsampler behind the artifact table-query route."""

from __future__ import annotations

import numpy as np
import pytest

from clio_agent.gact.artifacts.lttb import lttb_indices


def test_short_series_is_returned_whole() -> None:
    assert lttb_indices([0, 1, 2], [5, 6, 7], 10).tolist() == [0, 1, 2]
    assert lttb_indices([0, 1, 2], [5, 6, 7], 3).tolist() == [0, 1, 2]


def test_tiny_targets_keep_endpoints() -> None:
    x = np.arange(10)
    assert lttb_indices(x, x, 1).tolist() == [0]
    assert lttb_indices(x, x, 2).tolist() == [0, 9]


def test_output_size_endpoints_and_order() -> None:
    x = np.arange(1_000, dtype=float)
    y = np.sin(x / 30.0)

    kept = lttb_indices(x, y, 50)

    assert kept.size == 50
    assert kept[0] == 0 and kept[-1] == 999
    assert np.all(np.diff(kept) > 0)


def test_spike_is_preserved() -> None:
    x = np.arange(100, dtype=float)
    y = np.zeros(100)
    y[37] = 50.0

    kept = lttb_indices(x, y, 10)

    assert 37 in kept.tolist()


def test_uneven_x_spacing_is_supported() -> None:
    x = np.cumsum(np.linspace(0.1, 5.0, 200))
    y = np.cos(x)

    kept = lttb_indices(x, y, 17)

    assert kept.size == 17
    assert np.all(np.diff(kept) > 0)


def test_invalid_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        lttb_indices([0, 1], [0], 2)
    with pytest.raises(ValueError):
        lttb_indices([0, 1], [0, 1], 0)
