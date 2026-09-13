from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    station: str
    value: float


def summarize(readings: list[Reading]) -> str:
    total = sum(reading.value for reading in readings)
    return f"{len(readings)} readings, total {total:.1f}"


if __name__ == "__main__":
    sample = [Reading("P123", 2.5), Reading("P456", 3.5)]
    print(summarize(sample))

