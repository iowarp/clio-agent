"""YAML frontmatter of the Markdown files blueprints, experts, packs and skills are
written in: ``---`` fenced YAML, then the body."""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from typing import Any

__all__ = ["parse_frontmatter"]


# Parsed frontmatter by text: every turn re-reads the installed blueprints, experts
# and packs, and their YAML parse was ~1.3 s of a turn's prologue. Bounded; each
# caller gets its own copy.
_FRONTMATTER_MEMO: OrderedDict[str, tuple[dict[str, Any], str]] = OrderedDict()
_FRONTMATTER_MEMO_MAX = 1024
_FRONTMATTER_MEMO_LOCK = threading.Lock()


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into its YAML frontmatter mapping and body (memoized on the text)."""

    with _FRONTMATTER_MEMO_LOCK:
        hit = _FRONTMATTER_MEMO.get(text)
        if hit is not None:
            _FRONTMATTER_MEMO.move_to_end(text)
    if hit is None:
        hit = _parse_frontmatter_uncached(text)
        with _FRONTMATTER_MEMO_LOCK:
            _FRONTMATTER_MEMO[text] = hit
            while len(_FRONTMATTER_MEMO) > _FRONTMATTER_MEMO_MAX:
                _FRONTMATTER_MEMO.popitem(last=False)
    meta, body = hit
    return copy.deepcopy(meta), body


def _parse_frontmatter_uncached(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end < 0:
        return {}, text
    frontmatter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    try:
        import yaml  # noqa: PLC0415

        parsed = yaml.safe_load(frontmatter) or {}
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}, body
    except Exception:  # noqa: BLE001,S110 - yaml unavailable/invalid; falls back to the line parser below
        pass
    meta: dict[str, Any] = {}
    cur_key = ""
    cur_map = ""
    cur_map_list = ""
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("\"'")
            if cur_map and cur_map_list:
                container = meta.setdefault(cur_map, {})
                if isinstance(container, dict):
                    items = container.setdefault(cur_map_list, [])
                    if isinstance(items, list):
                        items.append(value)
            elif cur_key and isinstance(meta.get(cur_key), list):
                meta[cur_key].append(value)
            continue
        if ":" not in raw:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if indent and cur_key:
            if not isinstance(meta.get(cur_key), dict):
                meta[cur_key] = {}
            container = meta[cur_key]
            if isinstance(container, dict):
                if value:
                    container[key] = value.strip("\"'")
                    cur_map_list = ""
                else:
                    container[key] = []
                    cur_map = cur_key
                    cur_map_list = key
            continue
        cur_map = ""
        cur_map_list = ""
        if value:
            meta[key] = value.strip("\"'")
            cur_key = ""
        else:
            meta[key] = []
            cur_key = key
    return meta, body


parse_frontmatter = _parse_frontmatter
