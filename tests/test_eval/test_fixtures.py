"""Fixture generation: determinism, answer keys that match the files, and plugin format."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.eval.tool_calling.fixtures import generate
from scripts.eval.tool_calling.fixtures.common import hash_tree
from scripts.eval.tool_calling.fixtures.seed_bugs import CATEGORIES


def _generate(out: Path, repo: Path, seed: int = 3) -> dict[str, Any]:
    return generate.generate(
        out,
        seed=seed,
        base="HEAD",
        repo=repo,
        tree_path="src/pkg",
        p3_package="tools",
        p4_subsystems=("alpha", "beta", "gamma"),
        h4_scale=360,
    )


@pytest.fixture(scope="module")
def generated(mini_repo: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("fixtures") / "out"
    return out, _generate(out, mini_repo)


@pytest.mark.timeout(240)  # two full generations, each running two fixture test suites
def test_same_seed_same_manifest(mini_repo: Path, tmp_path: Path, generated) -> None:
    _, first = generated
    second = _generate(tmp_path / "again", mini_repo)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    on_disk = json.loads((tmp_path / "again" / "manifest.json").read_text())
    assert on_disk == second


def test_other_seed_changes_answers(mini_repo: Path, tmp_path: Path, generated) -> None:
    _, first = generated
    other = _generate(tmp_path / "other", mini_repo, seed=4)
    assert other["tasks"]["P2"]["answer"] != first["tasks"]["P2"]["answer"]
    assert other["tasks"]["P5"]["answer"]["ids"] != first["tasks"]["P5"]["answer"]["ids"]


def test_manifest_hashes_every_file_but_git(generated) -> None:
    out, manifest = generated
    assert manifest["files"] == {k: v for k, v in hash_tree(out).items() if k != "manifest.json"}
    assert not any(part == ".git" for key in manifest["files"] for part in key.split("/"))
    assert manifest["base_commit"] and len(manifest["base_commit"]) == 40


def test_p1_answer_is_the_unique_function(generated) -> None:
    out, manifest = generated
    answer = manifest["tasks"]["P1"]["answer"]
    assert answer["function"] == "compute_checksum_total"
    definition = answer["definition"]
    line = (out / "tree" / definition["file"]).read_text().splitlines()[definition["line"] - 1]
    assert line.startswith("def compute_checksum_total(")
    assert [(c["file"], c["line"]) for c in answer["callers"]] == [
        ("src/pkg/beta/users.py", 5),
        ("src/pkg/beta/users.py", 9),
        ("src/pkg/gamma/more.py", 5),
    ]


def test_p2_fact_is_deep_in_a_5000_line_module(generated) -> None:
    out, manifest = generated
    answer = manifest["tasks"]["P2"]["answer"]
    text = (out / "p2" / answer["file"]).read_text()
    lines = text.splitlines()
    assert len(lines) == 5000
    ast.parse(text)
    assert 4200 <= answer["line"] <= 4400
    assert lines[answer["line"] - 1] == f"{answer['name']} = {answer['value']}"
    assert sum(1 for x in lines if x.startswith(f"{answer['name']} =")) == 1


def test_p3_bugs_are_seeded_where_recorded(generated, mini_repo: Path) -> None:
    out, manifest = generated
    issues = manifest["tasks"]["P3"]["answer"]["issues"]
    assert [i["category"] for i in issues] == list(CATEGORIES)
    assert len({i["file"] for i in issues}) == 5
    for issue in issues:
        path = out / "p3" / issue["file"]
        source = path.read_text()
        ast.parse(source)
        lines = source.split("\n")
        assert "\n".join(lines[issue["line_start"] - 1 : issue["line_end"]]).startswith(
            issue["after"].split("\n")[0]
        )
        original = subprocess.run(
            ["git", "show", f"HEAD:src/pkg/{issue['file']}"],
            cwd=mini_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert original != source, issue
    by_category = {i["category"]: i for i in issues}
    assert "range(len(items) - 1)" in by_category["off-by-one"]["after"]
    assert by_category["inverted-condition"]["after"].strip() == 'if mode != "fast":'
    assert (
        by_category["wrong-default"]["after"].strip().startswith("def load(path, retries: int = 0)")
    )
    assert "handle = open(path)" in by_category["unclosed-file"]["after"]
    assert by_category["swallowed-exception"]["after"].split("\n")[-1].strip() == "pass"


def test_p4_checklist_names_real_modules(generated) -> None:
    out, manifest = generated
    answer = manifest["tasks"]["P4"]["answer"]
    alpha = answer["subsystems"]["alpha"]["facts"]
    assert {"kind": "entry_point", "text": "names compute_checksum_total",
            "match_any": ["compute_checksum_total"]} in alpha  # fmt: skip
    for sub in answer["subsystems"].values():
        for fact in sub["facts"]:
            if fact["kind"] == "module":
                assert (out / "tree" / sub["path"] / f"{fact['match_any'][0]}.py").is_file()


def test_records_answer_matches_server_data(generated) -> None:
    from scripts.eval.tool_calling.servers.records import generate_records

    _, manifest = generated
    for task, size in (("P5", 20), ("P6", 200), ("P7", 2000)):
        answer = manifest["tasks"][task]["answer"]
        rows = generate_records(size, manifest["seed"])
        assert answer["ids"] == [r["record_id"] for r in rows if r["reading"] < 0]
        assert answer["count"] == len(answer["ids"]) >= 2


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_h1_history_has_the_regression(generated) -> None:
    out, manifest = generated
    answer = manifest["tasks"]["H1"]["answer"]
    assert answer["verified"] == {"tests_pass_before_regression": True, "tests_pass_at_head": False}
    log = _git(out / "h1", "log", "--format=%H %s").splitlines()
    assert len(log) == 5
    assert log[1].startswith(answer["introducing_commit"])
    assert "European" in log[1]
    diff = _git(out / "h1", "show", answer["introducing_commit"], "--", "ledgerlite/parser.py")
    assert "def parse_amount" in diff
    assert manifest["tasks"]["H3"] == {"fixture": "h1", "answer": answer}


def test_h2_history_has_the_slowdown(generated) -> None:
    out, manifest = generated
    answer = manifest["tasks"]["H2"]["answer"]
    assert answer["verified"]["tests_pass_at_head"] is True
    reader = (out / "h2" / "recstream" / "reader.py").read_text()
    assert "def read_record_at" in reader and "CHUNK_RECORDS" not in reader
    shown = _git(out / "h2", "show", answer["introducing_commit"], "--stat")
    assert "recstream/reader.py" in shown


def test_h4_and_h5_share_the_export(generated) -> None:
    out, manifest = generated
    h4, h5 = manifest["tasks"]["H4"], manifest["tasks"]["H5"]
    assert h4 == h5 and h4["fixture"] == "h4-export"
    answer = h4["answer"]
    assert answer["scale"] == 360 and answer["run_folders"] == 25
    assert [a["id"] for a in answer["anomalies"]] == ["calibration", "unit", "duplicate", "clock"]
    exported = [k for k in manifest["files"] if k.startswith("h4-export/")]
    assert len(exported) == 140
    for finding in answer["anomalies"]:
        assert (out / "h4-export" / finding["path"] / "run.json").is_file()


def test_marketplace_is_a_valid_agent_plugins_marketplace(generated) -> None:
    from clio_agent.plugins.manifest import read_mcp_config
    from clio_agent.plugins.marketplace import read_marketplace

    out, manifest = generated
    root = out / "marketplace"
    contents = read_marketplace(root, trusted=True)
    assert contents.name == "clio-eval"
    assert sorted(r.id for r in contents.plugins) == sorted(manifest["marketplace"]["plugins"])
    assert not [d for d in contents.diagnostics if d.severity == "error"]
    for plugin, servers in manifest["marketplace"]["plugins"].items():
        raw = json.loads((root / "plugins" / plugin / "mcp.json").read_text())
        assert set(raw) == {"$schema", "mcpServers"}
        parsed, diagnostics = read_mcp_config(root / "plugins" / plugin, plugin)
        assert not diagnostics
        assert sorted(parsed) == sorted(servers)
        assert all(re.fullmatch(r"[a-z0-9-]+", name) for name in parsed)
        assert (root / "plugins" / plugin / "server" / "run.py").is_file()
