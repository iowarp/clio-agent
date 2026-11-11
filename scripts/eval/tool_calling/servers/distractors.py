"""Distractor servers: ``synthetic-calendar`` and ``synthetic-inventory`` (15 tools each)."""

from __future__ import annotations

import random
from typing import Any

try:
    from .toolkit import CallLog, CollectionResponder, ToolSpec, build_server, p, t
except ImportError:  # run from a plugin folder
    from toolkit import (  # type: ignore[no-redef]
        CallLog,
        CollectionResponder,
        ToolSpec,
        build_server,
        p,
        t,
    )

CALENDAR = "synthetic-calendar"
INVENTORY = "synthetic-inventory"

_CAL = p("calendar_id", "str", "Calendar id, e.g. cal.work.")
_EVT = p("event_id", "str", "Event id, e.g. evt-1042.")
_DATE = "Date as YYYY-MM-DD."

CALENDAR_SPECS: list[ToolSpec] = [
    t("calendar_list_calendars", "list", "List calendars you can read or write."),
    t(
        "calendar_list_events",
        "list",
        "List events in a calendar between two dates.",
        _CAL,
        p("start_date", "str", _DATE),
        p("end_date", "str", _DATE),
    ),
    t("calendar_get_event", "read", "Read one event.", _EVT),
    t(
        "calendar_create_event",
        "write",
        "Create an event.",
        _CAL,
        p("title", "str", "Event title."),
        p("start", "str", "Start as YYYY-MM-DDTHH:MM."),
        p("end", "str", "End as YYYY-MM-DDTHH:MM."),
        p("location", "str?", "Where."),
    ),
    t(
        "calendar_update_event",
        "write",
        "Change an event's title, time or location.",
        _EVT,
        p("title", "str?", "New title."),
        p("start", "str?", "New start."),
        p("end", "str?", "New end."),
    ),
    t("calendar_delete_event", "write", "Delete an event.", _EVT),
    t(
        "calendar_find_free_slots",
        "read",
        "Free slots of a given length across your calendars.",
        p("duration_minutes", "int", "Slot length."),
        p("start_date", "str", _DATE),
        p("end_date", "str", _DATE),
    ),
    t(
        "calendar_add_attendee",
        "write",
        "Invite someone.",
        _EVT,
        p("email", "str", "Their email address."),
    ),
    t(
        "calendar_remove_attendee",
        "write",
        "Uninvite someone.",
        _EVT,
        p("email", "str", "Their email address."),
    ),
    t(
        "calendar_set_reminder",
        "write",
        "Add a reminder before an event.",
        _EVT,
        p("minutes_before", "int", "Minutes before the start.", 15),
    ),
    t("calendar_list_reminders", "list", "Upcoming reminders."),
    t(
        "calendar_search_events",
        "read",
        "Find events whose title or notes contain text.",
        p("query", "str", "Text to look for."),
    ),
    t(
        "calendar_share_calendar",
        "write",
        "Share a calendar.",
        _CAL,
        p("email", "str", "Who to share with."),
        p("role", "enum:reader|writer|owner", "Their access.", "reader"),
    ),
    t(
        "calendar_get_availability",
        "read",
        "Someone's busy times on a day.",
        p("email", "str", "Their email address."),
        p("date", "str", _DATE),
    ),
    t(
        "calendar_respond_to_invite",
        "write",
        "Accept or decline an invitation.",
        _EVT,
        p("response", "enum:accept|decline|tentative", "Your answer."),
    ),
]

_SKU = p("sku", "str", "Item SKU, e.g. SKU-20417.")

INVENTORY_SPECS: list[ToolSpec] = [
    t(
        "inventory_list_items",
        "list",
        "List stocked items.",
        p("category", "str?", "Only this category."),
    ),
    t("inventory_get_item", "read", "Stock level, location and reorder point of an item.", _SKU),
    t(
        "inventory_search",
        "read",
        "Find items by name.",
        p("query", "str", "Text to look for."),
    ),
    t(
        "inventory_adjust_stock",
        "write",
        "Add or remove stock.",
        _SKU,
        p("delta", "int", "Positive to add, negative to remove."),
        p("reason", "str", "Why the stock changed."),
    ),
    t(
        "inventory_set_reorder_point",
        "write",
        "Set the level that triggers a reorder.",
        _SKU,
        p("level", "int", "Units."),
    ),
    t("inventory_list_low_stock", "list", "Items at or below their reorder point."),
    t(
        "inventory_create_item",
        "write",
        "Add a new item.",
        p("name", "str", "Item name."),
        p("category", "str", "Category."),
        p("unit", "str", "Unit of measure.", "each"),
    ),
    t("inventory_archive_item", "write", "Stop stocking an item.", _SKU),
    t("inventory_list_locations", "list", "Warehouses and bins."),
    t(
        "inventory_move_stock",
        "write",
        "Move units between locations.",
        _SKU,
        p("from_location", "str", "Source location."),
        p("to_location", "str", "Destination location."),
        p("quantity", "int", "Units to move."),
    ),
    t(
        "inventory_get_movements",
        "read",
        "Recent stock movements of an item.",
        _SKU,
        p("days", "int", "How many days back.", 30),
    ),
    t(
        "inventory_create_purchase_order",
        "write",
        "Order more of an item.",
        _SKU,
        p("quantity", "int", "Units."),
        p("supplier", "str", "Supplier name."),
    ),
    t(
        "inventory_list_purchase_orders",
        "list",
        "Purchase orders.",
        p("status", "enum:open|received|cancelled", "Only this status.", "open"),
    ),
    t(
        "inventory_receive_purchase_order",
        "write",
        "Mark an order received.",
        p("po_id", "str", "Order id, e.g. PO-3311."),
    ),
    t("inventory_list_suppliers", "list", "Suppliers and their lead times."),
]


def _calendar_rows(rng: random.Random) -> dict[str, dict[str, dict[str, Any]]]:
    calendars = {"cal.work": {"name": "Work"}, "cal.family": {"name": "Family"}}
    titles = ("Standup", "Dentist", "Planning", "School pickup", "Review", "Lunch")
    events = {
        f"evt-{1000 + i}": {
            "title": rng.choice(titles),
            "calendar_id": rng.choice(sorted(calendars)),
            "start": f"2026-11-{rng.randint(2, 27):02d}T{rng.randint(8, 17):02d}:00",
        }
        for i in range(24)
    }
    return {"calendar": {**calendars, **events}}


def _inventory_rows(rng: random.Random) -> dict[str, dict[str, dict[str, Any]]]:
    names = ("hex bolt M6", "washer 6mm", "cable tie", "fuse 5A", "relay 12V", "zip bag")
    items = {
        f"SKU-{20400 + i}": {
            "name": names[i % len(names)],
            "on_hand": rng.randint(0, 400),
            "reorder_point": rng.choice((20, 50, 100)),
        }
        for i in range(30)
    }
    orders = {f"PO-{3300 + i}": {"status": "open"} for i in range(5)}
    return {"inventory": {**items, **orders}}


def build_calendar(seed: int, log: CallLog) -> Any:
    rows = _calendar_rows(random.Random(seed + 11))
    return build_server(
        CALENDAR,
        "Personal and work calendars: events, attendees, reminders and availability.",
        CALENDAR_SPECS,
        CollectionResponder(rows),
        log,
    )


def build_inventory(seed: int, log: CallLog) -> Any:
    rows = _inventory_rows(random.Random(seed + 13))
    return build_server(
        INVENTORY,
        "A parts inventory: items, stock levels, locations, suppliers and purchase orders.",
        INVENTORY_SPECS,
        CollectionResponder(rows),
        log,
    )
