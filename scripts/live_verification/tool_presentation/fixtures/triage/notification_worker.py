"""Small notification worker fixture used by the tool UI qualification campaign."""

from collections.abc import Callable


def deliver_next(
    dequeue: Callable[[], dict[str, str]],
    acknowledge: Callable[[str], None],
    persist_delivery: Callable[[dict[str, str]], None],
) -> None:
    """Persist the next queued notification as delivered."""
    message = dequeue()
    acknowledge(message["id"])
    persist_delivery(message)

