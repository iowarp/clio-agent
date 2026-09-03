"""CLIO agent-test SUT.

Defines *how to run CLIO* for agent-test: set the provider/model (so the model
matrix works), activate an Agent Blueprint, send a prompt to the live gact
server, and normalize the resulting session trace into an agent-test ``Run``.

Tests stay declarative: they pass the runtime semantics (provider, model,
blueprint, prompt) and assert on the ``Run``. Everything about *driving* CLIO
lives here.

Runtime knobs (env):
  CLIO_GACT_URL          base URL of a running gact server (default :17960)
  CLIO_AGENTTEST_CELLS   "provider:model,provider:model" cells() override
The blueprint, marketplace source, workdir, and timeout are passed per call via
the ``agent.run({...})`` input dict.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
from agent_test import SUT, Run, ToolCall

DEFAULT_BASE_URL = os.environ.get("CLIO_GACT_URL", "http://127.0.0.1:17960").rstrip("/")

# SAFETY GUARDRAIL (deliberate, per Jaime): the matrix must never fan out across
# every model on every endpoint if the autonomous loop triggers --matrix. So the
# default matrix is hard-capped to ONE provider and AT MOST 2 models. Metis is
# the target precisely because it runs a small fixed set. Normal operation uses
# the first cell (gpt-oss-120b). Override deliberately with CLIO_AGENTTEST_CELLS.
RESTRICTED_CELLS = [
    ("argonne_metis", "gpt-oss-120b"),
    ("argonne_metis", "Llama-4-Maverick-17B-128E-Instruct"),
]


# Committed per-model LM bind profile — the reproducible "model DB" (tasks
# #33/#34), keyed by model id. This is the durable home for the LM settings that
# used to live in the throwaway /tmp grind shell (CTX=/TEMP=/sampling env): a run
# is now reproducible from committed config alone, no shell incantation to
# remember. Applied as DEFAULTS in bind(), so an explicit --override or a
# CLIO_AGENTTEST_* env still wins for ad-hoc experiments.
#
# Why these values (qwopus, a Qwen-family reasoning model): it MUST NOT run
# greedy (temp 0 collapses reasoning models into repetition); 0.6 is Qwen's
# recommended thinking-mode temperature. And it needs a real context window — the
# EarthScope pipeline accumulates ~50-60k tokens of reasoning + evidence, so a
# 32768 window truncates the model mid-thought exactly when it is reasoning
# through messy/unexpected data (which is the whole point of an agent vs a
# script). parallel=1 serializes the single-GPU box; turn_timeout_s drives the
# server's progress-aware watchdog for a slow-but-advancing reasoning run.
MODEL_PROFILES: dict[str, dict[str, Any]] = {
    # gemma4 on ALCF/Sophia (the generalization driver, #682). Non-reasoning, so
    # temp 0 is the right call for the routing-dominated pipeline (typed
    # next_expert decisions follow the grounding most consistently at temp 0 —
    # see the routing-temperature note). Remote inference: context_length is the
    # request window, not an LM Studio load size; gemma-4 exposes a large window.
    "google/gemma-4-31B-it": {
        "context_length": 262144,
        "temperature": 0.0,
        "turn_timeout_s": 1800.0,
    },
    "qwopus3.5-9b-v3": {
        "context_length": 65536,
        # 0.6 is Qwen's thinking-mode default, but the pipeline is dominated by
        # multi-step ROUTING decisions (orchestrators picking next_expert), where
        # lower temp follows the (already-thorough) grounding far more consistently
        # — the dominant remaining failure is orchestrators intermittently ignoring
        # their routing rules. Dropped to 0.4 for routing reliability; the
        # stop-sequences + parse re-sample guard against any low-temp repetition.
        # Revert toward 0.6 if reasoning experts start looping.
        "temperature": 0.4,
        "parallel": 1,
        "turn_timeout_s": 1900.0,
    },
    # Claude Code (OAuth/subscription) via the `claude -p` CLI. A fresh process is
    # spawned per LM call (~10-15s cold start, #715), so a full multi-turn EarthScope
    # run needs a generous, progress-aware turn budget to reach completion. temp 0 for
    # the routing-dominated pipeline (same rationale as the other non-reasoning cells).
    "haiku": {
        "temperature": 0.0,
        "turn_timeout_s": 3600.0,
    },
}


def _cells_from_env() -> list[tuple[str, str]] | None:
    """Optional explicit cell list from config, e.g.
    ``CLIO_AGENTTEST_CELLS="argonne_metis:gpt-oss-120b"``.
    Returns None when unset so ``cells()`` uses the restricted default."""
    raw = os.environ.get("CLIO_AGENTTEST_CELLS", "").strip()
    if not raw:
        return None
    cells: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        provider, _, model = chunk.partition(":")
        cells.append((provider.strip(), model.strip()))
    return cells or None


class ClioAgent(SUT):
    """Drives a live CLIO gact server through one blueprint turn."""

    name = "clio"

    def __init__(self) -> None:
        self._base_url = DEFAULT_BASE_URL
        self._provider = ""
        self._model = ""
        self._overrides: dict[str, Any] = {}

    # --- agent-test SUT contract -------------------------------------------

    def cells(self) -> list[tuple[str, str]]:
        """The matrix is intentionally restricted (blast-radius guardrail): a
        single provider (Metis) and at most 2 models, so an accidental
        ``--matrix`` cannot sweep every model on every endpoint. The first cell
        (gpt-oss-120b) is normal operation. Override only on purpose with
        ``CLIO_AGENTTEST_CELLS``; the runner still pins one with
        ``--provider/--model``."""
        explicit = _cells_from_env()
        if explicit is not None:
            return explicit
        return list(RESTRICTED_CELLS)

    def available(self, provider: str, model: str) -> bool:
        """A cell is runnable when the gact server is reachable and the
        provider is usable. agent-test treats False as 'skip (blank)', not a
        failure, so unconfigured cells (no API key, no Globus token) drop out of
        ``--matrix`` cleanly instead of erroring at bind time.

        Usability = the provider's ``is_authenticated`` from the registry, plus a
        live Globus-token check for argonne (whose token isn't reflected there)."""
        try:
            with httpx.Client(base_url=self._base_url, timeout=5.0) as http:
                # A 503 here just means no LM is wired yet (bind fixes that);
                # only a transport failure means the server is unreachable.
                http.get("/v1/health")
                rows = http.get("/v1/providers").json().get("providers", [])
        except Exception:
            return False
        row = next((r for r in rows if r.get("id") == provider), None)
        if row is None:
            return False
        if str(row.get("metadata", {}).get("provider_kind") or "").startswith("argonne"):
            try:
                from clio_agent.providers import argonne_auth

                return bool(argonne_auth.tokens_exist())
            except Exception:
                return False
        return bool(row.get("is_authenticated"))

    def bind(
        self, provider: str, model: str, overrides: dict[str, Any] | None = None
    ) -> "ClioAgent":
        """Configure the live LM for this cell. This is the model-iteration
        seam: provider/model come from the matrix, not the test body."""
        self._provider, self._model, self._overrides = provider, model, dict(overrides or {})
        with httpx.Client(base_url=self._base_url, timeout=120.0) as http:
            http.get("/v1/health")  # reachability; 503 == "no LM yet", which we are about to fix
            rows = http.get("/v1/providers").json().get("providers", [])
            row = next((r for r in rows if r.get("id") == provider), None)
            if row is None:
                raise RuntimeError(f"unknown provider cell: {provider!r}")
            kind = str(row.get("metadata", {}).get("provider_kind") or provider)
            # Sweep hooks (env fallbacks) so the temperature-exploration grind can
            # vary temperature / point at a non-default LM Studio host without
            # editing code per run. Explicit per-cell overrides still win.
            env_temp = os.environ.get("CLIO_AGENTTEST_TEMPERATURE", "")
            env_api_base = os.environ.get("CLIO_AGENTTEST_API_BASE", "")
            api_base = str(
                self._overrides.get("api_base") or env_api_base or row.get("api_base") or ""
            )
            model_id = model or str(row.get("default_model") or "")
            # Committed per-model defaults (the reproducible model DB). Precedence
            # for every knob below: explicit --override > CLIO_AGENTTEST_* env >
            # this profile > hardcoded fallback.
            profile = MODEL_PROFILES.get(model_id, {})
            # CLEAN SLATE before handing off to clio: eject EVERY loaded instance of
            # this model from LM Studio first, so a prior crashed/duplicate/wrong-config
            # instance (e.g. a stale parallel=4 load) can never co-reside with the one
            # clio is about to load and collapse the GPU. This is the TEST's job, not
            # clio core's — clio's production load path must not aggressively unload
            # user/other instances; the harness owns its own hygiene. lm_studio only.
            if kind == "lm_studio":
                self._eject_lm_studio_model(api_base, model_id)
                # Then LOAD at the profile's context/parallel via the native REST
                # API instead of relying on clio's bind / LM Studio's JIT default
                # (the 8192-context, parallel-4 trap that starves a multi-stage
                # pipeline and collapses a single GPU). The harness owns LM Studio
                # hygiene; this guarantees a reproducible load config per cell.
                self._load_lm_studio_model(api_base, model_id, profile)
            payload = {
                "provider": kind,
                "api_base": api_base,
                "model": model_id,
                "api_key": os.environ.get("CLIO_LM_API_KEY", "x"),
                "temperature": float(
                    self._overrides.get(
                        "temperature",
                        float(env_temp) if env_temp else profile.get("temperature", 0.0),
                    )
                ),
                "max_tokens": int(
                    self._overrides.get(
                        "max_tokens", int(os.environ.get("CLIO_AGENTTEST_MAX_TOKENS") or 32000)
                    )
                ),
                # LM Studio load-size + concurrency. Omitting context_length leaves
                # the server with no managed load -> LM Studio JIT-loads its 8192
                # default (the "8192 trap"), too small for a multi-stage pipeline's
                # accumulated context. Pass a real window (e.g. 65536) so the bind
                # loads/reuses an instance at that size. parallel=1 serializes a
                # single-GPU box. 0 = unset (no-op for non-lm_studio providers).
                "context_length": int(
                    self._overrides.get(
                        "context_length",
                        int(
                            os.environ.get("CLIO_AGENTTEST_CONTEXT_LENGTH")
                            or profile.get("context_length", 0)
                        ),
                    )
                ),
                "parallel": int(
                    self._overrides.get(
                        "parallel",
                        int(
                            os.environ.get("CLIO_AGENTTEST_PARALLEL") or profile.get("parallel", 0)
                        ),
                    )
                ),
                # Drive the SERVER's per-turn no-progress watchdog on the SAME
                # channel we configure the LM, so it cannot silently disagree with
                # our client-side watchdog (CLIO_AGENTTEST_NO_PROGRESS_S). Without
                # this the server defaults to 900s and kills a slow-but-progressing
                # reasoning-model pipeline even though the SUT waits longer. 0 =
                # leave the server on its own conf/default.
                "turn_timeout_s": float(
                    self._overrides.get(
                        "turn_timeout_s",
                        float(
                            os.environ.get("CLIO_AGENTTEST_NO_PROGRESS_S")
                            or profile.get("turn_timeout_s", 0)
                        ),
                    )
                ),
            }
            # Sampling surface (env fallbacks for the sampling-exploration grind).
            # Greedy decoding (temp 0) makes Qwen-family reasoning models degenerate
            # into repetition loops; this lets a grind force Qwen's recommended
            # thinking-mode sampling (temp 0.6 / top_p 0.95 / top_k 20) per run.
            # Only sent when set, so omitted -> the model's own default applies.
            for _key, _env, _cast in (
                ("top_p", "CLIO_AGENTTEST_TOP_P", float),
                ("top_k", "CLIO_AGENTTEST_TOP_K", int),
                ("min_p", "CLIO_AGENTTEST_MIN_P", float),
                ("presence_penalty", "CLIO_AGENTTEST_PRESENCE_PENALTY", float),
            ):
                _val = self._overrides.get(_key, os.environ.get(_env, "") or profile.get(_key, ""))
                if _val not in ("", None):
                    payload[_key] = _cast(_val)
            if "system_prompt" in self._overrides:
                payload["system_prompt"] = self._overrides["system_prompt"]
            http.put("/v1/providers/lm", json=payload, timeout=180.0).raise_for_status()
            self._wait_lm_ready(http, timeout_s=float(self._overrides.get("bind_timeout_s", 120.0)))
        return self

    def _eject_lm_studio_model(self, api_base: str, model: str) -> list[str]:
        """Unload EVERY loaded LM Studio instance of ``model`` (clean slate).

        Best-effort, never raises: queries the LM Studio native REST
        (``GET /api/v1/models``), finds all loaded instances whose key/id matches
        ``model``, and unloads each (``POST /api/v1/models/unload``). Run BEFORE
        clio binds (and at teardown) so a stale/duplicate/wrong-config instance
        (e.g. a crashed parallel=4 load) can never co-reside with the fresh one and
        OOM the GPU. The harness owns this hygiene; clio core stays untouched.
        """
        from urllib.parse import urlsplit, urlunsplit

        if not api_base or not model:
            return []
        parts = urlsplit(api_base.rstrip("/"))
        root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        headers = {"Content-Type": "application/json"}
        token = (
            os.environ.get("LM_STUDIO_API_TOKEN", "").strip()
            or os.environ.get("LM_API_TOKEN", "").strip()
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        ejected: list[str] = []
        try:
            with httpx.Client(timeout=30.0) as h:
                resp = h.get(f"{root}/api/v1/models", headers=headers)
                if resp.status_code >= 400:
                    return []
                for item in resp.json().get("models") or []:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "")
                    for inst in item.get("loaded_instances") or []:
                        if not isinstance(inst, dict):
                            continue
                        iid = str(inst.get("id") or "")
                        if not iid:
                            continue
                        # match exact key/id OR the "<model>:<n>" duplicate form
                        if model in {key, iid} or iid.startswith(f"{model}:") or key == model:
                            try:
                                u = h.post(
                                    f"{root}/api/v1/models/unload",
                                    headers=headers,
                                    json={"instance_id": iid},
                                )
                                if u.status_code < 400:
                                    ejected.append(iid)
                            except Exception:
                                pass
        except Exception:
            return ejected
        if ejected:
            print(f"[clean-slate] ejected LM Studio instances of {model!r}: {ejected}")
        return ejected

    def _load_lm_studio_model(self, api_base: str, model: str, profile: dict[str, Any]) -> bool:
        """Load ``model`` into LM Studio at the profile's context/parallel via the
        native REST load API (``POST /api/v1/models/load``), so the grind never
        depends on LM Studio's JIT default (the 8192-context / parallel-4 trap).

        Best-effort, never raises. Run AFTER ``_eject_lm_studio_model`` so the
        freshly-loaded instance is the only one. flash_attention + KV-cache GPU
        offload are kept on (the validated qwopus stack). lm_studio only."""
        from urllib.parse import urlsplit, urlunsplit

        if not api_base or not model:
            return False
        parts = urlsplit(api_base.rstrip("/"))
        root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        cfg = {
            "model": model,
            "context_length": int(profile.get("context_length") or 0) or 65536,
            "parallel": int(profile.get("parallel") or 0) or 1,
            "flash_attention": True,
            "offload_kv_cache_to_gpu": True,
        }
        headers = {"Content-Type": "application/json"}
        token = (
            os.environ.get("LM_STUDIO_API_TOKEN", "").strip()
            or os.environ.get("LM_API_TOKEN", "").strip()
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=300.0) as h:
                r = h.post(f"{root}/api/v1/models/load", headers=headers, json=cfg)
                if r.status_code < 400:
                    print(
                        f"[clean-slate] loaded {model!r} at "
                        f"ctx={cfg['context_length']} parallel={cfg['parallel']}"
                    )
                    return True
                print(f"[clean-slate] load {model!r} failed: {r.status_code} {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001 - load hygiene is best-effort
            print(f"[clean-slate] load {model!r} error: {exc}")
        return False

    def _wait_lm_ready(self, http: httpx.Client, *, timeout_s: float) -> None:
        """LM wiring is async after PUT; await readiness via the server-side
        ``GET /v1/providers/lm/wait`` long-poll instead of a hand-rolled poll loop,
        so the first turn does not race a 503 ('no ClioAgent wired')."""
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            window = max(1.0, min(30.0, deadline - time.monotonic()))
            try:
                info = http.get(
                    "/v1/providers/lm/wait",
                    params={"timeout": window},
                    timeout=window + 10.0,
                ).json()
            except Exception as exc:  # transient transport hiccup — retry within deadline
                last = f"wait error: {exc}"
                time.sleep(1.0)
                continue
            state = str(info.get("state") or "")
            last = state or str(info.get("status_message") or "")
            if state == "ready":
                return
            if state == "error":
                raise RuntimeError(f"LM provider failed to configure: {info.get('error') or info}")
            # idle / still configuring after the long-poll window -> loop again
        raise TimeoutError(f"LM provider not ready in {timeout_s:g}s (last state: {last!r})")

    def invoke(self, input: Any) -> Run:
        """Run one blueprint turn and normalize the trace into a Run.

        input: {"task": str, "blueprint_id": str, "marketplace_source"?: str,
                "workdir"?: str, "timeout_s"?: float, "trace_path"?: str}
        """
        spec = input if isinstance(input, dict) else {"task": str(input)}
        prompt = str(spec.get("task") or spec.get("prompt") or "")
        blueprint_id = str(spec.get("blueprint_id") or "")
        # ``workdir`` is MANDATORY: it becomes the session's workspace root, and
        # the agent writes its real deliverables (staged CSVs, rendered PNGs)
        # there. Defaulting it to ``Path.cwd()`` silently turned the repo root
        # into the scratch workspace, so every live run deposited artifacts into
        # the source tree (and got band-aided away with per-filename gitignores).
        # Require an explicit, isolated dir so that can never recur — tests pass
        # a pytest ``tmp_path``.
        workdir = str(spec.get("workdir") or "").strip()
        if not workdir:
            raise ValueError(
                "ClioAgent.invoke requires an explicit 'workdir' in the run spec "
                "(the isolated workspace root for this run). Pass e.g. "
                "agent.run({..., 'workdir': str(tmp_path)}). Refusing to default "
                "to the current working directory, which leaks deliverables into "
                "the repo."
            )
        timeout_s = float(spec.get("timeout_s", 600.0))

        with httpx.Client(base_url=self._base_url, timeout=200.0) as http:
            workspace_id = self._ensure_workspace(http, Path(workdir))
            if spec.get("marketplace_source"):
                http.post(
                    "/v1/agent-blueprints/install",
                    json={
                        "source": spec["marketplace_source"],
                        "scope": "workspace",
                        "workspace_id": workspace_id,
                    },
                    timeout=180.0,
                ).raise_for_status()
            session_id = self._create_session(http, workspace_id, blueprint_id)
            # Multi-turn: a "turns" list runs several prompts on the SAME session
            # (progressive exploration — #687). Single "task" is the one-turn case.
            turns = spec.get("turns")
            prompts = [str(t) for t in turns] if turns else [prompt]
            turn_runs: list[Run] = []
            seen_ids: set[str] = set()
            seen_artifact_ids: set[str] = set()
            assistant: dict[str, Any] = {}
            for turn_prompt in prompts:
                assistant = self._post_turn(http, session_id, turn_prompt, timeout_s)
                snapshot = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
                if turns:
                    fresh = [m for m in snapshot if str(m.get("id")) not in seen_ids]
                    # Per-turn artifacts: the registry versions NEW since the prior
                    # turn (diff by artifact_id) — sourced from the registry wire.
                    reg_items = self._registry_artifacts(http, session_id)
                    turn_items = [
                        it for it in reg_items if it["artifact_id"] not in seen_artifact_ids
                    ]
                    seen_artifact_ids.update(it["artifact_id"] for it in reg_items)
                    turn_runs.append(
                        self._to_run(
                            assistant,
                            fresh,
                            [],
                            {},
                            session_id,
                            blueprint_id,
                            None,
                            artifacts=self._existing_paths(turn_items),
                        )
                    )
                seen_ids.update(str(m.get("id")) for m in snapshot)
            messages = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
            all_sessions = http.get("/v1/sessions").json()["sessions"]
            children = [r for r in all_sessions if r.get("parent_session_id") == session_id]
            # Full descendant tree (children of children, ...): child experts run
            # as REAL child sessions that spawn their own declared children in
            # turn, so tool activity a matcher needs (e.g. geo_*/ndp_*/plot_*) can
            # fire two or more levels down, not only in a direct child. One
            # /v1/sessions listing was already fetched above; walk it once here.
            descendants = self._descendant_sessions(all_sessions, session_id)
            descendant_payloads: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
            for row in descendants:
                desc_id = str(row.get("id") or "")
                if not desc_id:
                    continue
                resp = http.get(f"/v1/sessions/{desc_id}/messages")
                desc_messages = resp.json().get("messages", []) if resp.status_code == 200 else []
                descendant_payloads.append((row, desc_messages))
            active = http.get(f"/v1/sessions/{session_id}/agent-blueprint").json()
            run_artifacts = self._existing_paths(self._registry_artifacts(http, session_id))

        trace_path = self._resolve_trace_path(spec)
        run = self._to_run(
            assistant,
            messages,
            children,
            active,
            session_id,
            blueprint_id,
            trace_path,
            artifacts=run_artifacts,
        )
        self._fold_descendants(run, descendant_payloads)
        # All descendants (not just direct children), tree order — child_session_ids
        # above stays direct-only (unchanged contract); this is the full tree the
        # fold above walked.
        run.extra["descendant_session_ids"] = [
            str(row.get("id") or "") for row, _ in descendant_payloads
        ]
        # Per-turn sub-runs (multi-turn case): each carries that turn's own
        # tool_calls / steps / artifacts so a test can assert turn-by-turn
        # (e.g. turn 1 discovers but does not plot; later turns reuse state).
        if turn_runs:
            run.extra["turn_runs"] = [tr.to_dict() for tr in turn_runs]
        if trace_path:
            # Dump the FULL descendant tree (not just direct children) so a
            # trace-driven review can see the whole spawn tree post-hoc — same
            # child_session record schema _dump_trace already writes.
            self._dump_trace(
                trace_path, run, messages, [row for row, _ in descendant_payloads], active
            )
        return run

    def _resolve_trace_path(self, spec: dict[str, Any]) -> Path | None:
        """SUT-owned bootup semantic: where trace logs go.

        Convention: traces land in
        ``<case_dir>/runs/<label>-<provider>-<model>.jsonl`` so per-cell matrix
        runs never collide (different models on the same provider get distinct
        files). An explicit ``trace_path`` wins; if neither is given, no trace is
        written.
        """
        if spec.get("trace_path"):
            return Path(str(spec["trace_path"]))
        case_dir = spec.get("case_dir")
        if not case_dir:
            return None
        label = str(spec.get("run_label") or "run")
        cell = "-".join(part for part in (self._provider, self._model) if part) or "cell"
        cell = cell.replace("/", "_").replace(":", "_")
        return Path(str(case_dir)) / "runs" / f"{label}-{cell}.jsonl"

    # --- recipe helpers -----------------------------------------------------

    def _ensure_workspace(self, http: httpx.Client, root: Path) -> str:
        target = str(root.expanduser().resolve())
        for row in http.get("/v1/workspaces").json().get("workspaces", []):
            if str(row.get("root_path") or "") == target:
                return str(row.get("id") or "")
        created = http.post(
            "/v1/workspaces",
            json={
                "name": "agent-test",
                "root_path": target,
                "storage_root": str(Path(target) / ".clio"),
            },
        )
        created.raise_for_status()
        return str(created.json().get("id") or "")

    def _create_session(self, http: httpx.Client, workspace_id: str, blueprint_id: str) -> str:
        created = http.post(
            "/v1/sessions", json={"title": "agent-test", "workspace_id": workspace_id}
        )
        created.raise_for_status()
        session_id = created.json()["id"]
        if blueprint_id:
            http.post(
                f"/v1/sessions/{session_id}/agent-blueprint", json={"blueprint_id": blueprint_id}
            ).raise_for_status()
        return session_id

    def _post_turn(
        self, http: httpx.Client, session_id: str, prompt: str, timeout_s: float
    ) -> dict[str, Any]:
        ack = http.post(
            f"/v1/sessions/{session_id}/messages",
            json={"parts": [{"type": "text", "text": prompt}]},
        )
        ack.raise_for_status()
        user_id = ack.json()["message_id"]
        # Progress-based wait: NO per-session/per-experiment wall. Keep waiting as
        # long as the turn is still producing (the message stream keeps changing);
        # abort only after a generous NO-PROGRESS window (a genuinely stuck turn).
        # Individual hung calls are bounded by per-call timeouts elsewhere. A 15-min
        # run that is producing should complete. ``timeout_s`` (if >0) is an optional
        # hard ceiling, OFF by default; ``no_progress_s`` is the real watchdog.
        # The SERVER owns no-progress detection. Under agent-driven routing the
        # experts run NESTED in this one parent turn (no child sessions, and the
        # nested expert LM calls -- which run in executors -- emit neither a
        # /messages delta nor a session SSE event the client can see). So the
        # client has NO visibility into intra-turn progress and MUST NOT run its
        # own progress watchdog: doing so just kills a slow-but-healthy reasoning
        # run that the server knows is still generating (server-side
        # ``CLIO_GACT_TURN_TIMEOUT_S`` IS progress-aware -- it treats an in-flight
        # LM call as progress and returns a terminal ``provider_timeout`` message
        # only when the model truly wedges). The client therefore just waits for
        # the server's terminal message; ``unresponsive_s`` only guards against the
        # server itself going dark (poll failing), and ``timeout_s`` is an optional
        # absolute hard ceiling (OFF by default).
        unresponsive_s = float(
            self._overrides.get(
                "no_progress_s", float(os.environ.get("CLIO_AGENTTEST_NO_PROGRESS_S") or 900.0)
            )
        )
        hard_cap_s = float(timeout_s) if timeout_s and timeout_s > 0 else 0.0
        start = time.monotonic()
        last_ok = start
        while True:
            try:
                messages = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
                last_ok = time.monotonic()  # server is alive and answering
            except Exception:
                messages = None
            if messages is not None:
                for index, message in enumerate(messages):
                    if message.get("id") == user_id:
                        if index > 0 and messages[index - 1].get("role") == "assistant":
                            assistant = messages[index - 1]
                            if (
                                str(assistant.get("stop_reason") or "")
                                or assistant.get("error_info") is not None
                            ):
                                return assistant
                        break
            now = time.monotonic()
            if now - last_ok > unresponsive_s:
                raise TimeoutError(f"gact server unresponsive for {unresponsive_s:g}s")
            if hard_cap_s and now - start > hard_cap_s:
                raise TimeoutError(f"assistant turn exceeded hard cap {hard_cap_s:g}s")
            time.sleep(1.0)

    # --- normalization ------------------------------------------------------

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        parts = message.get("parts") or message.get("content") or []
        if isinstance(parts, str):
            return parts
        out = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                out.append(str(part.get("text") or ""))
        return "\n".join(t for t in out if t)

    @staticmethod
    def _extract_messages(
        messages: list[dict],
    ) -> tuple[list[ToolCall], list[list[str]], float, list[dict]]:
        """Extract tool calls, routing steps, cost, and structured outputs from
        ONE session's assistant messages (oldest-first traversal).

        Shared by ``_to_run`` (the root/main session) and ``invoke``'s descendant
        fold: every session in the tree — root, child, or grandchild — carries
        assistant messages in the same shape, so the same extraction applies
        uniformly to each.

        Routing (steps) has TWO sources, in call order per message:

        * The CURRENT routing surface (react-main spawn architecture, #948 +
          composer wave): the assistant CALLS ``spawn_agent_task`` /
          ``spawn_agents_parallel`` to route work. Verified live: metadata has NO
          ``expert_handoffs`` key; routing decisions ARE the spawn tool calls. Each
          ``spawn_agent_task`` call becomes its own one-agent step; each
          ``spawn_agents_parallel`` call becomes ONE step listing every agent it
          fanned out to (they ran together, not in sequence).
        * ``expert_handoffs`` metadata — the legacy delegation surface. Kept for
          back-compat (an older runtime/replay trace might still carry it), even
          though the live server no longer emits it.
        """
        import json as _json

        tool_calls: list[ToolCall] = []
        steps: list[list[str]] = []
        cost = 0.0
        structured: list[dict] = []
        for message in reversed(messages):  # oldest-first
            if message.get("role") != "assistant":
                continue
            cost += float(message.get("cost_usd") or 0.0)
            meta = message.get("metadata") or {}
            for tool in meta.get("tools_called") or []:
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name") or "")
                args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
                result = tool.get("result")
                if isinstance(result, str):
                    try:
                        result = _json.loads(result)
                    except _json.JSONDecodeError:
                        pass
                tool_calls.append(ToolCall(name=name, args=args, output=result))
                if name == "spawn_agent_task":
                    agent_id = args.get("agent")
                    if isinstance(agent_id, str) and agent_id.strip():
                        steps.append([agent_id])
                elif name == "spawn_agents_parallel":
                    spawns = args.get("spawns")
                    batch = [
                        str(spec.get("agent"))
                        for spec in (spawns if isinstance(spawns, list) else [])
                        if isinstance(spec, dict)
                        and isinstance(spec.get("agent"), str)
                        and spec.get("agent").strip()
                    ]
                    if batch:
                        steps.append(batch)
            handoffs = [
                str(h.get("agent_id") or h.get("to") or h.get("delegate_to") or "")
                for h in (meta.get("expert_handoffs") or [])
                if isinstance(h, dict)
            ]
            handoffs = [h for h in handoffs if h]
            if handoffs:
                steps.append(handoffs)
            runtime = meta.get("agent_runtime") or {}
            if isinstance(runtime.get("structured_outputs"), dict):
                structured.append(runtime["structured_outputs"])
        return tool_calls, steps, cost, structured

    @staticmethod
    def _descendant_sessions(
        all_sessions: list[dict[str, Any]], root_id: str
    ) -> list[dict[str, Any]]:
        """Every descendant of ``root_id`` (children, grandchildren, ...), in
        depth-first tree order, from the ONE ``/v1/sessions`` listing the caller
        already fetched.

        Child experts run as REAL child sessions (#948), and children spawn their
        own DECLARED children in turn (verified live: ``geo_geocode`` ran in a
        child session, ``ndp_search_datasets`` in a GRANDCHILD) — so a run's tool
        activity is scattered across the whole descendant tree, not just the
        direct children. The ``visited`` guard makes this robust to a
        malformed/cyclic ``parent_session_id`` edge (should never happen, but a
        normalization bug must never hang the harness in a traversal loop).
        """
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in all_sessions:
            parent = str(row.get("parent_session_id") or "")
            if parent:
                by_parent.setdefault(parent, []).append(row)
        ordered: list[dict[str, Any]] = []
        visited: set[str] = {root_id}

        def _walk(parent_id: str) -> None:
            for row in by_parent.get(parent_id, []):
                rid = str(row.get("id") or "")
                if not rid or rid in visited:
                    continue
                visited.add(rid)
                ordered.append(row)
                _walk(rid)

        _walk(root_id)
        return ordered

    def _fold_descendants(
        self,
        run: Run,
        descendant_payloads: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    ) -> None:
        """Fold every descendant session's tool calls / routing steps / cost INTO
        ``run`` in place — main's own tool_calls/steps stay first, descendants are
        appended in tree order. ``run`` is a frozen dataclass, but its ``tool_calls``
        / ``steps`` lists and ``usage`` dict are mutable containers: mutating their
        CONTENTS (extend/update) is legal, only reassigning the attribute itself
        would raise."""
        for _row, desc_messages in descendant_payloads:
            desc_tool_calls, desc_steps, desc_cost, _structured = self._extract_messages(
                desc_messages
            )
            run.tool_calls.extend(desc_tool_calls)
            run.steps.extend(desc_steps)
            run.usage["cost_usd"] = float(run.usage.get("cost_usd", 0.0)) + desc_cost
        run.usage["steps"] = sum(len(turn) for turn in run.steps)

    def _to_run(
        self,
        assistant: dict,
        messages: list[dict],
        children: list[dict],
        active: dict,
        session_id: str,
        blueprint_id: str,
        trace_path: Path | None = None,
        *,
        artifacts: list[str] | None = None,
    ) -> Run:
        tool_calls, steps, cost, structured = self._extract_messages(messages)
        active_id = str(active.get("active_agent_blueprint_id") or "")
        error_info = assistant.get("error_info")
        workflow_state = self._extract_workflow_state(messages)
        return Run(
            output=self._message_text(assistant),
            steps=steps,
            tool_calls=tool_calls,
            usage={"cost_usd": cost, "steps": sum(len(t) for t in steps)},
            error=str(error_info) if error_info else None,
            extra={
                "session_id": session_id,
                "blueprint_id": blueprint_id,
                "active_agent_blueprint_id": active_id,
                "blueprint_activated": active_id == blueprint_id,
                "child_session_ids": [str(c.get("id")) for c in children],
                "stop_reason": str(assistant.get("stop_reason") or ""),
                "provider": self._provider,
                "model": self._model,
                "workflow_state": workflow_state,
                "structured_outputs": structured,
                # Registry-sourced (S7 #973): the produced artifacts come from the
                # artifact-registry wire, not a tool-output path scrape.
                "artifacts": list(artifacts or []),
                "trace_path": str(trace_path) if trace_path else "",
            },
        )

    @staticmethod
    def _extract_workflow_state(messages: list[dict]) -> dict[str, Any]:
        """Parse the merged typed `workflow_state` the runtime embeds in message
        text (e.g. 'Retained typed workflow state: {"workflow_state": {...}}').
        Returns the deepest-merged state seen across messages."""
        import json as _json

        merged: dict[str, Any] = {}
        for message in messages:
            # Scan ALL parts (any type), plus metadata — the merged typed state
            # is embedded in non-"text" parts the user-facing extractor skips.
            chunks = []
            parts = message.get("parts") or message.get("content") or []
            if isinstance(parts, list):
                for p in parts:
                    if isinstance(p, dict) and p.get("text"):
                        chunks.append(str(p["text"]))
            meta = message.get("metadata") or {}
            if meta:
                chunks.append(_json.dumps(meta, default=str))
            text = "\n".join(chunks)
            idx = 0
            while True:
                hit = text.find('{"workflow_state"', idx)
                if hit < 0:
                    break
                depth, end = 0, -1
                for i in range(hit, len(text)):
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end < 0:
                    break
                try:
                    obj = _json.loads(text[hit:end])
                    ws = obj.get("workflow_state")
                    if isinstance(ws, dict):
                        merged.update(ws)
                except _json.JSONDecodeError:
                    pass
                idx = end
        return merged

    def _registry_artifacts(self, http: httpx.Client, session_id: str) -> list[dict[str, Any]]:
        """The session's REGISTERED artifact versions, queried from the wire (S7 #973).

        Replaces the deleted tool-output path scraper: instead of guessing artifact
        paths out of tool-call outputs, this queries the artifact registry route
        ``GET /v1/sessions/{sid}/artifacts?include_children=true`` — the designation
        truth — so every benchmark run LIVE-TESTS the artifact contract (the registry
        actually recorded what the agent produced, with typed kind/custody/sha256).
        ``include_children`` unions the delegates' workspaces so an orchestrator run
        sees its children's outputs. Returns one flat item per immutable version.
        """
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):  # bounded pagination (never an unbounded dump)
            params: dict[str, Any] = {"include_children": True, "limit": 200}
            if cursor:
                params["before"] = cursor
            resp = http.get(f"/v1/sessions/{session_id}/artifacts", params=params)
            if resp.status_code != 200:
                break
            body = resp.json()
            for record in body.get("artifacts") or []:
                for version in record.get("versions") or []:
                    items.append(
                        {
                            "artifact_id": str(version.get("artifact_id") or ""),
                            "name": str(record.get("name") or ""),
                            "path": str(version.get("path") or ""),
                            "kind": str(version.get("kind") or ""),
                            "sha256": version.get("sha256"),
                            "custody": str(version.get("custody") or ""),
                        }
                    )
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return items

    @staticmethod
    def _existing_paths(items: list[dict[str, Any]]) -> list[str]:
        """The on-disk artifact paths from registry items (deduped, order-preserved)."""
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            path = str(item.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            if Path(path).is_file():
                result.append(path)
        return result

    def _dump_trace(
        self, path: str | Path, run: Run, messages: list, children: list, active: dict
    ) -> None:
        import json

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"record": "run", "run": run.to_dict()}, default=str) + "\n")
            fh.write(
                json.dumps({"record": "agent_blueprint", "active": active}, default=str) + "\n"
            )
            for child in children:
                fh.write(
                    json.dumps({"record": "child_session", "child": child}, default=str) + "\n"
                )
            for message in messages:
                fh.write(json.dumps({"record": "message", "message": message}, default=str) + "\n")
