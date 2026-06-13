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

    def bind(self, provider: str, model: str, overrides: dict[str, Any] | None = None) -> "ClioAgent":
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
            # CLEAN SLATE before handing off to clio: eject EVERY loaded instance of
            # this model from LM Studio first, so a prior crashed/duplicate/wrong-config
            # instance (e.g. a stale parallel=4 load) can never co-reside with the one
            # clio is about to load and collapse the GPU. This is the TEST's job, not
            # clio core's — clio's production load path must not aggressively unload
            # user/other instances; the harness owns its own hygiene. lm_studio only.
            if kind == "lm_studio":
                self._eject_lm_studio_model(api_base, model_id)
            payload = {
                "provider": kind,
                "api_base": api_base,
                "model": model_id,
                "api_key": os.environ.get("CLIO_LM_API_KEY", "x"),
                "temperature": float(
                    self._overrides.get("temperature", float(env_temp) if env_temp else 0.0)
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
                        int(os.environ.get("CLIO_AGENTTEST_CONTEXT_LENGTH") or 0),
                    )
                ),
                "parallel": int(
                    self._overrides.get(
                        "parallel", int(os.environ.get("CLIO_AGENTTEST_PARALLEL") or 0)
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
                        float(os.environ.get("CLIO_AGENTTEST_NO_PROGRESS_S") or 0),
                    )
                ),
            }
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
                    json={"source": spec["marketplace_source"], "scope": "workspace", "workspace_id": workspace_id},
                    timeout=180.0,
                ).raise_for_status()
            session_id = self._create_session(http, workspace_id, blueprint_id)
            assistant = self._post_turn(http, session_id, prompt, timeout_s)
            messages = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
            children = [r for r in http.get("/v1/sessions").json()["sessions"]
                        if r.get("parent_session_id") == session_id]
            active = http.get(f"/v1/sessions/{session_id}/agent-blueprint").json()

        trace_path = self._resolve_trace_path(spec)
        run = self._to_run(assistant, messages, children, active, session_id, blueprint_id, trace_path)
        if trace_path:
            self._dump_trace(trace_path, run, messages, children, active)
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
        created = http.post("/v1/workspaces", json={
            "name": "agent-test", "root_path": target, "storage_root": str(Path(target) / ".clio")})
        created.raise_for_status()
        return str(created.json().get("id") or "")

    def _create_session(self, http: httpx.Client, workspace_id: str, blueprint_id: str) -> str:
        created = http.post("/v1/sessions", json={"title": "agent-test", "workspace_id": workspace_id})
        created.raise_for_status()
        session_id = created.json()["id"]
        if blueprint_id:
            http.post(f"/v1/sessions/{session_id}/agent-blueprint",
                      json={"blueprint_id": blueprint_id}).raise_for_status()
        return session_id

    def _post_turn(self, http: httpx.Client, session_id: str, prompt: str, timeout_s: float) -> dict[str, Any]:
        ack = http.post(f"/v1/sessions/{session_id}/messages",
                        json={"parts": [{"type": "text", "text": prompt}]})
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
                            if str(assistant.get("stop_reason") or "") or assistant.get("error_info") is not None:
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

    def _to_run(self, assistant: dict, messages: list[dict], children: list[dict],
                active: dict, session_id: str, blueprint_id: str, trace_path: Path | None = None) -> Run:
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
            for tool in (meta.get("tools_called") or []):
                if not isinstance(tool, dict):
                    continue
                result = tool.get("result")
                if isinstance(result, str):
                    try:
                        result = _json.loads(result)
                    except _json.JSONDecodeError:
                        pass
                tool_calls.append(ToolCall(
                    name=str(tool.get("name") or ""),
                    args=tool.get("args") if isinstance(tool.get("args"), dict) else {},
                    output=result,
                ))
            handoffs = [str(h.get("agent_id") or h.get("to") or h.get("delegate_to") or "")
                        for h in (meta.get("expert_handoffs") or []) if isinstance(h, dict)]
            handoffs = [h for h in handoffs if h]
            if handoffs:
                steps.append(handoffs)
            runtime = meta.get("agent_runtime") or {}
            if isinstance(runtime.get("structured_outputs"), dict):
                structured.append(runtime["structured_outputs"])
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
                "artifacts": self._artifacts(tool_calls),
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

    @staticmethod
    def _artifacts(tool_calls: list[ToolCall]) -> list[str]:
        """Collect produced artifact paths from tool outputs (e.g. the rendered
        map's ``output_path``), keeping only ones that exist on disk."""
        paths: list[str] = []
        for call in tool_calls:
            out = call.output
            if not isinstance(out, dict):
                continue
            for key in ("output_path", "artifact", "path", "png", "plot_path", "map_artifact"):
                value = out.get(key)
                if isinstance(value, str) and value:
                    paths.append(value)
        seen: set[str] = set()
        result: list[str] = []
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            if Path(p).is_file():
                result.append(p)
        return result

    def _dump_trace(self, path: str, run: Run, messages: list, children: list, active: dict) -> None:
        import json

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"record": "run", "run": run.to_dict()}, default=str) + "\n")
            fh.write(json.dumps({"record": "agent_blueprint", "active": active}, default=str) + "\n")
            for child in children:
                fh.write(json.dumps({"record": "child_session", "child": child}, default=str) + "\n")
            for message in messages:
                fh.write(json.dumps({"record": "message", "message": message}, default=str) + "\n")
