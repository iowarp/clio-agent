"""The showcase scripts: the Wordle reference, the hidden-law scorer and the study audit."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

SHOWCASE = Path(__file__).resolve().parents[2] / "scripts" / "showcase"
sys.path.insert(0, str(SHOWCASE))

import audit_study  # noqa: E402
import hidden_law_score  # noqa: E402
import wordle_reference  # noqa: E402
from hidden_law_bench.law import draw_law  # noqa: E402
from wordle_demo.game import Bench, feedback  # noqa: E402

SECRET = "showcase-test-secret"


def test_the_vectorized_feedback_matches_the_games_rule() -> None:
    words = Bench(SECRET).words
    rng = random.Random(1)
    sample = rng.sample(words, 200)
    encoded = wordle_reference._encode(sample)
    for guess in rng.sample(words, 20):
        codes = wordle_reference.patterns(wordle_reference._encode([guess])[0], encoded)
        expected = [wordle_reference._code(feedback(answer, guess)) for answer in sample]
        assert codes.tolist() == expected


def test_the_reference_solves_games_and_reads_the_studys_own() -> None:
    bench = Bench(SECRET)
    player = wordle_reference.Player(bench.words)
    games = [player.play(bench, seed) for seed in (11, 12)]
    assert all(game["status"] == "solved" and game["turns"] <= 6 for game in games)
    snapshot = {
        "calls": [
            {"session": "s1", "tool": "wordle_new_game", "input": {"seed": 4},
             "result_text": '{"game_id": "g1-s4", "seed": 4}', "message_id": "m"},
            {"session": "s1", "tool": "wordle_guess", "input": {},
             "result_text": '{"game_id": "g1-s4", "word": "abaky", "status": "playing"}',
             "message_id": "m"},
            {"session": "s1", "tool": "wordle_guess", "input": {},
             "result_text": '{"game_id": "g1-s4", "word": "kazuv", "status": "solved"}',
             "message_id": "m"},
        ]
    }  # fmt: skip
    [played] = wordle_reference.study_games(snapshot)
    assert played == {
        "game_id": "g1-s4", "seed": 4, "branch": "s1",
        "guesses": ["abaky", "kazuv"], "status": "solved", "turns": 2,
    }  # fmt: skip
    assert wordle_reference.summarize([played])["mean_guesses_solved"] == 2


def _true_formula(secret: str) -> str:
    law = draw_law(secret)
    second = {
        "bob_mass_kg": "(bob_mass_kg/0.5)",
        "release_angle_deg": "(release_angle_deg/20.0)",
        "ambient_temp_c": "((ambient_temp_c+273.15)/293.15)",
    }[law.second]
    return (
        f"T = {law.scale_s!r}*L^{law.length_exponent!r}*{second}^({law.second_exponent!r})"
        f"*where({law.regime} > {law.threshold!r}, ({law.regime}/{law.threshold!r})"
        f"^{law.regime_exponent!r}, 1)"
    )


def test_the_hidden_law_scores_zero_and_the_textbook_law_does_not() -> None:
    law = draw_law(SECRET)
    exact = hidden_law_score.score(hidden_law_score.Formula(_true_formula(SECRET)), SECRET)
    assert exact["rms_error_noise_units"] < 1e-6
    assert exact["textbook_rms_error_noise_units"] > 3
    assert exact["law_gives_decoy_no_effect"] is True
    assert exact["law_thresholds_for_regime_input"] == [pytest.approx(law.threshold)]
    textbook = hidden_law_score.score(hidden_law_score.Formula("2*pi*sqrt(L/g)"), SECRET)
    assert textbook["rms_error_noise_units"] == textbook["textbook_rms_error_noise_units"]
    ten = hidden_law_score.score(hidden_law_score.Formula("20*pi*sqrt(L/g)"), SECRET, "ten_cycles")
    assert ten["rms_error_noise_units"] == pytest.approx(textbook["rms_error_noise_units"])
    for unsafe in ("__import__('os')", "L.__class__", "open('x')", "unknown_input * 2"):
        with pytest.raises((ValueError, SyntaxError)):
            hidden_law_score.Formula(unsafe)


def test_the_scorer_finds_the_formula_behind_the_conclusion() -> None:
    entries = [
        {"id": "h1", "type": "hypothesis", "fields": {"formula": "2*L^0.6"}, "links": []},
        {"id": "ev", "type": "evidence", "fields": {}, "links": [{"relation": "supports", "to": "h1"}]},
        {"id": "c1", "type": "conclusion", "links": [],
         "fields": {"claims": [{"text": "T = 2 L^0.6", "cites": ["ev"]}]}},
    ]  # fmt: skip
    formula, text = hidden_law_score.law_from_snapshot({"science_log": {"entries": entries}})
    assert formula == "2*L^0.6" and text == "T = 2 L^0.6"


def test_the_audit_reports_sources_analyses_and_decisions() -> None:
    lineage = [
        {"reference": "call:b1/call_a", "call_id": "call_a", "session": "c1", "tool": "measure"},
        {
            "reference": "call:f1/call_old",
            "call_id": "call_old",
            "session": "f1",
            "tool": "measure",
        },
    ]
    snapshot = {
        "science_log": {
            "study": "q",
            "branches": [
                {"session_id": "root", "relation": "root"},
                {"session_id": "b1", "relation": "step", "parent_session_id": "root"},
                {"session_id": "c1", "relation": "delegated", "parent_session_id": "b1"},
                {"session_id": "f1", "relation": "fork", "parent_session_id": "root",
                 "fork_of_session_id": "b1"},
            ],
            "entries": [
                {"id": "q", "type": "question", "branch": "root", "fields": {"statement": "Why?"}},
                {"id": "o1", "type": "observation", "branch": "b1",
                 "fields": {"lineage": lineage[:1], "measurements": [{"conditions": {"seed": 1}}]}},
                {"id": "o2", "type": "observation", "branch": "f1", "fields": {"lineage": lineage[1:]}},
                {"id": "e1", "type": "evidence", "branch": "b1",
                 "fields": {"interpretation": "mean 3.2 vs 3.9", "analysis_call": "call:b1/call_fit",
                            "seed": 7}},
                {"id": "k", "type": "conclusion", "branch": "root",
                 "fields": {"claims": [{"text": "A beats B by 0.7", "cites": ["e1"],
                                        "analysis_call": "call_fit"},
                                       {"text": "No effect of mass", "cites": ["o1"]}]}},
            ],
        },
        "calls": [
            {"session": "b1", "call_id": "call_old", "tool": "measure", "message_id": "m"},
            {"session": "b1", "call_id": "call_g", "tool": "wordle_guess", "message_id": "m"},
            {"session": "f1", "call_id": "call_h", "tool": "wordle_guess", "message_id": "m",
             "is_error": True},
            {"session": "b1", "call_id": "call_fit", "tool": "shell_bash", "message_id": "m",
             "input": {"command": "python fit.py"}},
        ],
        "transforms": {
            "call_fit": {"instrument": {"tool": "shell_bash", "cmd": "python fit.py",
                                        "script_hash": "abc123"},
                         "used": [{"path": "m.parquet", "sha256": "def456"}],
                         "environment": {"tier": "lockfile-hash"}},
        },
    }  # fmt: skip
    report = audit_study.audit(snapshot, "guess")
    first, second = report["observations"]
    assert first["cites"][0]["in_branch_tree"] is True and first["condition_keys"] == ["seed"]
    assert second["cites"][0]["in_forker_history"] is True
    assert report["observations_citing_calls"] == 2
    assert report["quantitative_claims"] == 2
    assert report["quantitative_claims_citing_analysis"] == 2
    fit = report["claims"][1]["analysis"]
    assert fit["script_sha256"] == "abc123" and fit["seeds"] == ["7"]
    assert fit["inputs"] == [{"path": "m.parquet", "sha256": "def456"}]
    decisions = report["decisions"]
    assert decisions["calls"] == 2 and decisions["rejected"] == 1
    assert decisions["by_branch"] == {"b1": 1, "f1": 1} and decisions["all_model_tool_calls"]
    assert decisions["shell_commands"] == [{"session": "b1", "command": "python fit.py"}]
    assert "Study audit: Why?" in audit_study.markdown(report)
