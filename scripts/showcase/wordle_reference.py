"""An information-maximizing Wordle player, outside every plugin, for checking a study.

It plays the same word list and the same game seeds offline, with the secret
(``CLIO_SECRET_WORDLE_SEED``), choosing each guess to maximize the expected
information over the words still consistent with the feedback. Like the agent, it
does not know which words are answers: every listed word is a candidate. Its numbers
are used only to check and publish a study's result; it never runs during a study.

Seeds come from ``--seeds`` or from a study snapshot's ``new_game`` calls, so the
reference plays exactly the games the study played. With a snapshot it also lists
the study's own games (seed, branch, guesses, status) beside its own, as facts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "external/clio-agent-marketplace/plugins/wordle-demo/impl/src"))

from wordle_demo.game import MAX_TURNS, Bench  # noqa: E402

_LETTERS = 26


def _encode(words: list[str]) -> np.ndarray:
    return np.array([[ord(c) - 97 for c in word] for word in words], dtype=np.int8)


def patterns(guess: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Feedback codes (base 3: 0 absent, 1 present, 2 correct) of ``guess`` against
    every row of ``candidates``, with the duplicate-letter rule, vectorized."""

    green = candidates == guess
    counts = np.zeros((len(candidates), _LETTERS), dtype=np.int8)
    rows = np.arange(len(candidates))
    for pos in range(5):
        np.add.at(counts, (rows[~green[:, pos]], candidates[~green[:, pos], pos]), 1)
    code = np.zeros(len(candidates), dtype=np.int32)
    for pos in range(5):
        letter = guess[pos]
        yellow = ~green[:, pos] & (counts[:, letter] > 0)
        counts[yellow, letter] -= 1
        code += np.where(green[:, pos], 2, np.where(yellow, 1, 0)) * 3**pos
    return code


def _code(marks: list[str]) -> int:
    return sum({"correct": 2, "present": 1}.get(m, 0) * 3**i for i, m in enumerate(marks))


class Player:
    def __init__(self, words: list[str]) -> None:
        self.words = words
        self.encoded = _encode(words)

    def best_guess(self, candidates: np.ndarray) -> int:
        """The listed word whose feedback splits ``candidates`` most evenly (entropy);
        among equals, one that could itself be the answer."""

        if len(candidates) <= 2:
            return int(candidates[0])
        if len(candidates) == len(self.words):
            return self._opening()
        return self._most_informative(candidates)

    @cache  # noqa: B019 - one player per process; the opening never changes
    def _opening(self) -> int:
        return self._most_informative(np.arange(len(self.words)))

    def _most_informative(self, candidates: np.ndarray) -> int:
        best, best_score = int(candidates[0]), -1.0
        pool = self.encoded[candidates]
        possible = set(candidates.tolist())
        for index in range(len(self.words)):
            counts = np.bincount(patterns(self.encoded[index], pool), minlength=243)
            probabilities = counts[counts > 0] / len(candidates)
            score = float(-(probabilities * np.log2(probabilities)).sum())
            score += 1e-6 * (index in possible)
            if score > best_score:
                best, best_score = index, score
        return best

    def play(self, bench: Bench, seed: int) -> dict[str, Any]:
        game = bench.new_game(seed)
        candidates = np.arange(len(self.words))
        while game.status == "playing":
            guess = self.words[self.best_guess(candidates)]
            bench.guess(game.id, guess)
            observed = _code(game.marks[-1])
            keep = patterns(_encode([guess])[0], self.encoded[candidates]) == observed
            candidates = candidates[keep]
        return {
            "seed": seed,
            "guesses": list(game.guesses),
            "turns": len(game.guesses),
            "status": game.status,
        }


def study_games(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The study's own games from a snapshot: seed, branch, guesses and final status."""

    games: dict[str, dict[str, Any]] = {}
    for call in data.get("calls") or []:
        tool = call["tool"]
        try:
            result = json.loads(call.get("result_text") or "{}")
        except ValueError:
            continue
        if not isinstance(result, dict) or call.get("is_error"):
            continue
        if tool.endswith("new_game") and result.get("game_id"):
            games[result["game_id"]] = {
                "game_id": result["game_id"],
                "seed": result.get("seed", (call.get("input") or {}).get("seed")),
                "branch": call["session"],
                "guesses": [],
                "status": "playing",
            }
        elif tool.endswith("guess") and result.get("game_id") in games:
            game = games[result["game_id"]]
            game["guesses"].append(result.get("word"))
            game["status"] = result.get("status", game["status"])
    for game in games.values():
        game["turns"] = len(game["guesses"])
    return list(games.values())


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    solved = [row["turns"] for row in rows if row["status"] == "solved"]
    return {
        "games": len(rows),
        "solved": len(solved),
        "failed": sum(row["status"] == "failed" for row in rows),
        "unfinished": sum(row["status"] == "playing" for row in rows),
        "mean_guesses_solved": round(sum(solved) / len(solved), 3) if solved else None,
        "max_turns": MAX_TURNS,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="", help="comma-separated game seeds")
    parser.add_argument("--snapshot", default="", help="a study snapshot JSON")
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args()
    secret = os.environ.get("CLIO_SECRET_WORDLE_SEED", "")
    bench = Bench(secret)
    player = Player(bench.words)
    data = json.loads(Path(args.snapshot).read_text()) if args.snapshot else {}
    played = study_games(data) if data else []
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()] or sorted(
        {int(g["seed"]) for g in played if g.get("seed") is not None}
    )
    reference = [player.play(bench, seed) for seed in seeds]
    by_branch: dict[str, list[dict[str, Any]]] = {}
    for game in played:
        by_branch.setdefault(game["branch"], []).append(game)
    report = {
        "reference": {"summary": summarize(reference), "games": reference},
        "study": {
            "summary": summarize(played),
            "by_branch": {b: summarize(g) for b, g in by_branch.items()},
            "games": played,
        },
        "list_version": bench.version,
        "entropy_of_uniform_list_bits": round(math.log2(len(bench.words)), 3),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"reference": report["reference"]["summary"], "study": report["study"]["summary"]}
        )
    )


if __name__ == "__main__":
    main()
