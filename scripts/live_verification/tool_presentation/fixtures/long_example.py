"""A deliberately long Python fixture for bounded read and syntax rendering."""

from collections.abc import Iterable


def normalize(values: Iterable[float]) -> list[float]:
    items = list(values)
    if not items:
        return []
    low = min(items)
    high = max(items)
    if high == low:
        return [0.0 for _ in items]
    return [(value - low) / (high - low) for value in items]


def labeled_rows(values: Iterable[float]) -> list[dict[str, float | str]]:
    normalized = normalize(values)
    return [
        {"index": index, "label": f"sample-{index:02d}", "value": value}
        for index, value in enumerate(normalized)
    ]


SAMPLE_VALUES = [
    12.0,
    18.5,
    14.25,
    27.75,
    31.0,
    29.5,
    42.25,
    38.0,
    45.5,
    51.25,
    49.0,
    58.75,
]


if __name__ == "__main__":
    for row in labeled_rows(SAMPLE_VALUES):
        print(f"{row['label']}: {row['value']:.3f}")

