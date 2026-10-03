"""Stage the exact component fragments returned by CLIO's load_skill resolver."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main() -> None:
    """Render both catalog fixtures through the production JSON fragment resolver."""
    resolver_path = (
        Path(__file__).resolve().parents[2] / "src/clio_agent/gact/agents/skill_json_fragment.py"
    )
    spec = importlib.util.spec_from_file_location("clio_skill_fragment", resolver_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load skill resolver: {resolver_path}")
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    source = Path(sys.argv[1])
    result: dict[str, dict[str, str]] = {}
    for slug, relative in (
        ("clio-workspace", "clio-workspace/v1/catalog.json"),
        ("basic", "basic/catalog.json"),
    ):
        raw = (source / relative).read_text(encoding="utf-8")
        for name in json.loads(raw)["components"]:
            escaped_name = name.replace("~", "~0").replace("/", "~1")
            fragment = f"/components/{escaped_name}"
            result[name] = {
                "call": f'load_skill("a2ui-catalog-{slug}", file="catalog.json#{fragment}")',
                "content": resolver.resolve_json_pointer_fragment("catalog.json", raw, fragment),
            }
    target = Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
