from __future__ import annotations

from pathlib import Path

from scripts.validate_marketplace_blueprints import (
    MarketplaceValidationOptions,
    validate_marketplace_source,
)


def _write_pack(
    root: Path,
    *,
    pack_id: str,
    nested: bool,
    unknown_tool: bool = False,
) -> None:
    pack = root / pack_id
    experts = pack / "experts"
    experts.mkdir(parents=True)
    pack.joinpath("AGENT.md").write_text(
        f"""---
id: {pack_id}
version: 1.0.0
title: {pack_id}
description: Test pack
blueprint:
  format: agent-blueprint-v1
root_expert: main
---
Root prompt.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "main.md").write_text(
        """---
id: main
title: Main
tier: 1
children:
  - data
---
Main prompt.
""",
        encoding="utf-8",
    )
    data_tool = "missing_tool" if unknown_tool else "hdf5_list_datasets"
    children = "\nchildren:\n  - format" if nested else ""
    experts.joinpath("data.md").write_text(
        f"""---
id: data
title: Data
tier: 2
parent_id: main{children}
tools:
  - {data_tool}
---
Data prompt.
""",
        encoding="utf-8",
    )
    if nested:
        experts.joinpath("format.md").write_text(
            """---
id: format
title: Format
tier: 3
parent_id: data
tools:
  - hdf5_analyze_dataset
---
Format prompt.
""",
            encoding="utf-8",
        )


def _write_mcp_descriptor(root: Path, *, pack_id: str, self_contained: bool) -> None:
    tools = root / pack_id / "tools"
    tools.mkdir(parents=True)
    if self_contained:
        tools.joinpath("calculator.md").write_text(
            """---
id: calculator
name: Calculator MCP
transport: stdio
install:
  method: uvx
  package: clio-calculator-mcp
runtime:
  args:
    - serve
tools:
  - calculator_add
---
Calculator descriptor.
""",
            encoding="utf-8",
        )
        return
    tools.joinpath("bare.md").write_text(
        """---
id: bare
name: Bare MCP
transport: stdio
command: bare-mcp
tools:
  - bare_tool
---
Bare descriptor.
""",
        encoding="utf-8",
    )


def test_marketplace_preflight_fails_when_complex_count_is_too_low(tmp_path: Path) -> None:
    _write_pack(tmp_path, pack_id="shallow", nested=False)
    _write_pack(tmp_path, pack_id="complex", nested=True)

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(require_complex_count=2),
    )

    assert result["ok"] is False
    assert result["blueprint_count"] == 2
    assert result["complex_blueprints"] == ["complex"]
    assert result["complex_blueprint_count"] == 1
    assert result["validation_errors"] == [
        "complex blueprint count below requirement: 1/2"
    ]


def test_marketplace_preflight_reports_pack_validation_errors(tmp_path: Path) -> None:
    _write_pack(tmp_path, pack_id="broken", nested=True, unknown_tool=True)

    result = validate_marketplace_source(tmp_path)

    assert result["ok"] is False
    assert result["blueprint_count"] == 1
    assert result["complex_blueprints"] == []
    assert any(
        "broken: data: unknown tool reference: missing_tool" == error
        for error in result["validation_errors"]
    )
    broken = result["blueprints"][0]
    assert broken["enabled"] is False
    assert broken["metrics"]["expert_count"] == 3


def test_marketplace_preflight_counts_included_expert_subtrees(tmp_path: Path) -> None:
    pack = tmp_path / "seismic"
    experts = pack / "experts"
    module = pack / "modules" / "ndp-collector" / "experts"
    experts.mkdir(parents=True)
    module.mkdir(parents=True)
    pack.joinpath("AGENT.md").write_text(
        """---
id: seismic
version: 1.0.0
title: Seismic
description: Test pack
blueprint:
  format: agent-blueprint-v1
root_expert: main
includes:
  - modules/ndp-collector/experts
---
Root prompt.
""",
        encoding="utf-8",
    )
    experts.joinpath("main.md").write_text(
        """---
id: main
title: Main
tier: 1
---
Main prompt.
""",
        encoding="utf-8",
    )
    experts.joinpath("data.md").write_text(
        """---
id: data
title: Data
tier: 2
parent_id: main
---
Data prompt.
""",
        encoding="utf-8",
    )
    module.joinpath("ndp_catalog.md").write_text(
        """---
id: ndp_catalog
title: NDP Catalog
tier: 3
parent_id: data
---
Catalog prompt.
""",
        encoding="utf-8",
    )

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(require_complex_count=1),
    )

    assert result["ok"] is True
    assert result["complex_blueprints"] == ["seismic"]
    row = result["blueprints"][0]
    assert row["metrics"]["expert_count"] == 3
    assert row["metrics"]["edge_count"] == 2
    assert row["metrics"]["max_levels"] == 3


def test_marketplace_preflight_can_exclude_seismic_from_complex_count(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path, pack_id="seismic-waveform-review", nested=True)
    _write_pack(tmp_path, pack_id="domain-a", nested=True)
    _write_pack(tmp_path, pack_id="domain-b", nested=True)

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(
            require_complex_count=2,
            exclude_complex_ids=("seismic-waveform-review",),
        ),
    )

    assert result["ok"] is True
    assert result["complex_blueprints"] == ["domain-a", "domain-b"]


def test_marketplace_preflight_counts_self_contained_mcp_descriptors(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path, pack_id="bare-mcp-pack", nested=False)
    _write_mcp_descriptor(tmp_path, pack_id="bare-mcp-pack", self_contained=False)
    _write_pack(tmp_path, pack_id="portable-mcp-pack", nested=False)
    _write_mcp_descriptor(tmp_path, pack_id="portable-mcp-pack", self_contained=True)

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(
            require_mcp_descriptor_count=2,
            require_self_contained_mcp_count=1,
        ),
    )

    assert result["ok"] is True
    assert result["mcp_descriptor_count"] == 2
    assert result["self_contained_mcp_descriptor_count"] == 1
    rows = {row["id"]: row for row in result["blueprints"]}
    assert rows["bare-mcp-pack"]["mcp_descriptor_count"] == 1
    assert rows["bare-mcp-pack"]["self_contained_mcp_descriptor_count"] == 0
    assert rows["portable-mcp-pack"]["mcp_descriptor_count"] == 1
    assert rows["portable-mcp-pack"]["self_contained_mcp_descriptor_count"] == 1


def test_marketplace_preflight_can_require_self_contained_mcp_descriptors(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path, pack_id="bare-mcp-pack", nested=False)
    _write_mcp_descriptor(tmp_path, pack_id="bare-mcp-pack", self_contained=False)

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(
            require_mcp_descriptor_count=1,
            require_self_contained_mcp_count=1,
        ),
    )

    assert result["ok"] is False
    assert result["mcp_descriptor_count"] == 1
    assert result["self_contained_mcp_descriptor_count"] == 0
    assert result["validation_errors"] == [
        "self-contained MCP descriptor count below requirement: 0/1"
    ]


def test_marketplace_preflight_does_not_count_invalid_pack_local_mcp_descriptor(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path, pack_id="local-mcp-pack", nested=False)
    tools = tmp_path / "local-mcp-pack" / "tools"
    tools.mkdir(parents=True)
    tools.joinpath("calculator.md").write_text(
        """---
id: calculator
name: Calculator MCP
transport: stdio
install:
  method: pack-local
  path: mcp/missing_server.py
tools:
  - calculator_add
---
Calculator descriptor.
""",
        encoding="utf-8",
    )

    result = validate_marketplace_source(
        tmp_path,
        options=MarketplaceValidationOptions(
            require_mcp_descriptor_count=1,
            require_self_contained_mcp_count=1,
        ),
    )

    assert result["ok"] is False
    assert result["mcp_descriptor_count"] == 1
    assert result["self_contained_mcp_descriptor_count"] == 0
    assert any(
        "local-mcp-pack: calculator: pack-local MCP launch path not found: mcp/missing_server.py"
        == error
        for error in result["validation_errors"]
    )
    assert "self-contained MCP descriptor count below requirement: 0/1" in result[
        "validation_errors"
    ]
