#!/usr/bin/env python3
"""CLIO Polaris kit: drive one CLIO server through its REST API, one receipt per step.

Every service is deployed BY CLIO (``POST /v1/infrastructure/services/{id}/actions``);
this helper only sequences the calls, waits on CLIO's operations and records what
happened. ``DRY_RUN=1`` prints every REST call instead of sending it.

Runs on CLIO's own interpreter after stage-up, or on the node's system python3
(SLES ships 3.6) before it: standard library only, no 3.7+ syntax or APIs.

Usage: kit.py <command> [args]   (see ``main`` for the commands)
"""

import glob
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

DRY = os.environ.get("DRY_RUN", "0") == "1"
ROOT = os.environ.get("ROOT", ".")
EVIDENCE = os.environ.get("EVIDENCE", os.path.join(ROOT, "evidence"))
JOB_EVIDENCE = os.environ.get("JOB_EVIDENCE", os.path.join(EVIDENCE, "job-local"))
RUN_DIR = os.environ.get("RUN_DIR", os.path.join(ROOT, "run"))
HOST = socket.gethostname().split(".")[0]
JOB = os.environ.get("JOB_TAG", "nojob")
SERVE_FILE = os.path.join(RUN_DIR, "clio-serve.json")
# Loopback calls never go through the ALCF proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class KitError(RuntimeError):
    """A step failed; the message says what to inspect."""


def env(name, default=""):
    return os.environ.get(name, default)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def say(message):
    sys.stderr.write("[kit %s] %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


def write_json(path, data):
    """Atomic write; receipts are never partially written."""
    if DRY and not os.path.isdir(os.path.dirname(path) or "."):
        print("DRY-RUN would write %s: %s" % (path, json.dumps(data)[:400]))
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def prior(step, dry_default):
    """A previous step's receipt from this job (a stand-in under DRY_RUN)."""
    found = read_json(os.path.join(JOB_EVIDENCE, step + ".json"))
    if found:
        return found
    return dict(dry_default) if DRY else {}


def receipt(step, status, **details):
    """One JSON receipt per step under the job evidence directory."""
    data = {
        "step": step,
        "status": status,
        "host": HOST,
        "job": JOB,
        "at": now(),
        "dry_run": DRY,
    }
    data.update(details)
    write_json(os.path.join(JOB_EVIDENCE, step + ".json"), data)
    say("receipt %s: %s" % (step, status))
    return data


# --------------------------------------------------------------------------- REST


def base():
    serve = read_json(SERVE_FILE, {}) or {}
    return "http://127.0.0.1:%s" % (serve.get("port") or env("CLIO_PORT", "8100"))


class RestError(KitError):
    def __init__(self, method, path, status, payload):
        self.status = status
        self.payload = payload
        KitError.__init__(
            self, "%s %s -> HTTP %s: %s" % (method, path, status, json.dumps(payload)[:800])
        )


def call(method, path, body=None, timeout=120, dry=None, allow=()):
    """One REST call. Under DRY_RUN prints it and returns ``dry`` (a stand-in reply)."""
    url = base() + path
    if DRY:
        print("DRY-RUN %s %s %s" % (method, url, json.dumps(body) if body is not None else ""))
        return {} if dry is None else dry
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.time()
    try:
        response = _OPENER.open(request, timeout=timeout)
        status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        raise KitError(
            "%s %s: CLIO not reachable at %s (%s)" % (method, path, base(), exc)
        ) from exc
    try:
        payload = json.loads(raw.decode() or "{}")
    except ValueError:
        payload = {"raw": raw.decode(errors="replace")[:2000]}
    _rest_log(method, path, status, time.time() - started)
    if status >= 400 and status not in allow:
        raise RestError(method, path, status, payload)
    return payload


def _rest_log(method, path, status, seconds):
    try:
        os.makedirs(JOB_EVIDENCE, exist_ok=True)
        with open(os.path.join(JOB_EVIDENCE, "rest.jsonl"), "a") as handle:
            handle.write(
                json.dumps(
                    {
                        "at": now(),
                        "method": method,
                        "path": path,
                        "status": status,
                        "ms": int(seconds * 1000),
                    }
                )
                + "\n"
            )
    except OSError:
        pass


def healthy():
    try:
        call("GET", "/v1/health", timeout=10, allow=(503,), dry={"healthy": True})
        return True
    except KitError:
        return False


# ------------------------------------------------------------- CLIO serve lifecycle


def _proc_start(pid):
    """Kernel start time of ``pid`` (field 22 of /proc/<pid>/stat), or None."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _owned_serve():
    """The recorded CLIO serve process if it is still the same process, else None."""
    serve = read_json(SERVE_FILE)
    if not serve:
        return None
    pid = int(serve.get("pid") or 0)
    if not pid or _proc_start(pid) != serve.get("starttime"):
        return None
    if "serve" not in _proc_cmdline(pid) or str(serve.get("port")) not in _proc_cmdline(pid):
        return None
    return serve


def _bind(port, reuse):
    sock = socket.socket()
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return sock.getsockname()[1]
    except OSError:
        return None
    finally:
        sock.close()


def _free_port(preferred):
    """The preferred loopback port, else any free one.

    SO_REUSEADDR (as uvicorn binds) lets the preferred port be taken while the
    previous CLIO's connections sit in TIME_WAIT, so a restart keeps its port and
    the drivers' receipts, which pin the endpoint, still resume. A port another
    process really holds is retried for 90 s before falling back.
    """
    deadline = time.time() + (0 if DRY else 90)
    while True:
        if _bind(preferred, reuse=True) is not None:
            return preferred
        if time.time() >= deadline:
            break
        time.sleep(3)
    port = _bind(0, reuse=False)
    if port is not None:
        return port
    raise KitError("no free loopback port")


def _wait_agent_ready(log_path, timeout):
    """'agent ready' in the serve log, or tool hooks installed in /v1/health."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(log_path, errors="replace") as handle:
                if "agent ready" in handle.read():
                    return "log"
        except OSError:
            pass
        try:
            health = call("GET", "/v1/health", timeout=15, allow=(503,))
            if health.get("tool_hooks_installed") is True:
                return "health"
        except KitError:
            pass
        time.sleep(5)
    return ""


def serve_start():
    """Start CLIO on loopback (or reuse the recorded one), wait until its agent is ready."""
    owned = _owned_serve()
    if owned and healthy():
        return receipt("02-serve", "ok", reused=True, **owned)
    clio_bin = os.path.join(env("CLIO_SRC"), ".venv", "bin", "clio-agent")
    port = int(env("CLIO_PORT", "8100"))
    if DRY:
        print("DRY-RUN start: %s serve --host 127.0.0.1 --port %s" % (clio_bin, port))
        return receipt("02-serve", "dry_run", port=port)
    if not os.access(clio_bin, os.X_OK):
        raise KitError("CLIO is not installed at %s; run 01_stage_up.sh" % clio_bin)
    chosen = _free_port(port)
    if chosen != port:
        say(
            "port %s is busy; CLIO uses %s (resumed OPAL receipts pin the endpoint)"
            % (port, chosen)
        )
    logs = os.path.join(ROOT, "logs")
    os.makedirs(logs, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    log_path = os.path.join(logs, "clio-serve-%s-%s-%s.log" % (HOST, JOB, time.strftime("%H%M%S")))
    workspace = os.path.join(ROOT, "workspace-default")
    os.makedirs(workspace, exist_ok=True)
    command = [clio_bin, "serve", "--host", "127.0.0.1", "--port", str(chosen)]
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    serve = {
        "pid": process.pid,
        "pgid": process.pid,
        "starttime": _proc_start(process.pid),
        "port": chosen,
        "log": log_path,
        "command": command,
        "started_at": now(),
        "connector_source": env("CLIO_VLLM_ATTN_CONNECTOR", "(CLIO pin)"),
    }
    write_json(SERVE_FILE, serve)
    say("CLIO pid %s port %s log %s" % (process.pid, chosen, log_path))
    deadline = time.time() + 600
    while time.time() < deadline and not healthy():
        if process.poll() is not None:
            raise KitError("CLIO exited with %s; see %s" % (process.returncode, log_path))
        time.sleep(3)
    if not healthy():
        raise KitError("CLIO did not answer /v1/health in 10 min; see %s" % log_path)
    # A POST during boot can hit a marketplace or target lock (Delta c43): wait for the agent.
    ready = _wait_agent_ready(log_path, int(env("AGENT_READY_TIMEOUT_S", "1200")))
    serve["agent_ready"] = ready or "timeout"
    write_json(SERVE_FILE, serve)
    health = call("GET", "/v1/health", allow=(503,))
    write_json(os.path.join(JOB_EVIDENCE, "health.json"), health)
    status = "ok" if ready else "degraded"
    return receipt("02-serve", status, reused=False, overall=health.get("overall_status"), **serve)


def serve_stop(step="serve-stop"):
    """Stop only the recorded CLIO process group: TERM, then KILL after 60 s."""
    owned = _owned_serve()
    if DRY:
        print("DRY-RUN stop the recorded CLIO process group (%s)" % SERVE_FILE)
        return receipt(step, "dry_run")
    if not owned:
        return receipt(step, "skipped", reason="no live CLIO process recorded for this host")
    pgid = int(owned["pgid"])
    forced = False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(1)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        forced = True
    os.replace(SERVE_FILE, SERVE_FILE + ".stopped")
    return receipt(
        step,
        "ok",
        pid=owned["pid"],
        forced=forced,
        note="forced: SIGKILL after 60 s, graceful settlement unknown" if forced else "",
    )


# ------------------------------------------------------------------ operations


def wait_op(op, tag, timeout):
    """Follow a CLIO infrastructure operation; keep its progress and log as evidence."""
    op_id = op.get("id", "dry-op")
    progress = os.path.join(JOB_EVIDENCE, "ops", tag + ".jsonl")
    deadline = time.time() + timeout
    row = op
    while True:
        row = call(
            "GET",
            "/v1/infrastructure/operations/" + op_id,
            dry={"id": op_id, "state": "succeeded", "progress": "dry"},
        )
        if not DRY:
            os.makedirs(os.path.dirname(progress), exist_ok=True)
            with open(progress, "a") as handle:
                handle.write(
                    json.dumps(
                        {"t": now(), "state": row.get("state"), "progress": row.get("progress")}
                    )
                    + "\n"
                )
        if row.get("state") not in ("queued", "running"):
            break
        if time.time() > deadline:
            raise KitError(
                "operation %s (%s) still %s after %ss; inspect GET "
                "/v1/infrastructure/operations/%s" % (op_id, tag, row.get("state"), timeout, op_id)
            )
        time.sleep(5)
    if row.get("state") != "succeeded" and not DRY:
        lines = call("GET", "/v1/infrastructure/operations/%s/log?limit=5000" % op_id, allow=(404,))
        write_json(os.path.join(JOB_EVIDENCE, "ops", tag + "-log.json"), lines)
    return row


def catalog():
    return call(
        "GET", "/v1/infrastructure/catalog?target_id=local", timeout=300, dry=_dry_catalog()
    )


def _dry_catalog():
    services = os.path.join(ROOT, "state", "data", "services", HOST)
    return {
        "facts": {"accelerator": "nvidia", "hostname": HOST},
        "services": [
            {
                "id": "flowcept",
                "state": "running",
                "connection_url": "http://127.0.0.1:8008",
                "configuration": {"flowcept_settings": services + "/flowcept/settings.yaml"},
            },
            {
                "id": "vllm",
                "state": "running",
                "connection_url": "http://127.0.0.1:41234",
                "configuration": {
                    "model": "<model>",
                    "storage.captures": services + "/vllm/evidence",
                    "param.tensor_parallel_size": env("VLLM_TP", "1"),
                },
            },
        ],
    }


def service_row(cat, service_id):
    return next((row for row in cat.get("services", []) if row.get("id") == service_id), {})


def service_record(service_id):
    """The durable record (its installed variant) from the inventory."""
    inventory = call("GET", "/v1/infrastructure/inventory", dry={"services": []})
    return next(
        (
            row
            for row in inventory.get("services", [])
            if row.get("service_id") == service_id and row.get("target_id") == "local"
        ),
        {},
    )


def action(service_id, verb, variant, configuration, tag, timeout):
    body = {
        "target_id": "local",
        "action": verb,
        "variant_id": variant,
        "configuration": configuration,
    }
    write_json(os.path.join(JOB_EVIDENCE, "ops", "%s-%s-request.json" % (tag, verb)), body)
    op = call(
        "POST",
        "/v1/infrastructure/services/%s/actions" % service_id,
        body,
        dry={"id": "dry-%s-%s" % (service_id, verb)},
    )
    row = wait_op(op, "%s-%s" % (tag, verb), timeout)
    say("%s %s: %s %s" % (service_id, verb, row.get("state"), (row.get("error") or "")[:300]))
    return row


def ensure_service(service_id, variant, configuration, tag, timeout, matches=None):
    """Running with this variant and configuration, through CLIO's lifecycle actions.

    Reuse a matching running deployment; else start the installed one; else install
    (CLIO's reuse preflight skips work already done) and start. A running deployment
    that does not match is stopped first.
    """
    matches = matches or (lambda row: True)
    row = service_row(catalog(), service_id)
    record = service_record(service_id)
    if DRY:  # show the full first-deployment path
        row, record = dict(row, state="not_installed"), {}
    same = record.get("variant_id") == variant and matches(row)
    steps = []
    if row.get("state") == "running" and same:
        return row, ["reused running deployment"]
    if row.get("state") == "running" and not same:
        stopped = action(service_id, "stop", record.get("variant_id") or variant, {}, tag, 600)
        steps.append("stop: " + str(stopped.get("state")))
    if same and record.get("state") not in (None, "", "not_installed"):
        started = action(service_id, "start", variant, configuration, tag, timeout)
        steps.append("start: " + str(started.get("state")))
        if started.get("state") == "succeeded":
            return service_row(catalog(), service_id), steps
    installed = action(service_id, "install", variant, configuration, tag, timeout)
    steps.append("install: " + str(installed.get("state")))
    if installed.get("state") != "succeeded":
        raise KitError(
            "%s install failed: %s (evidence %s/ops/%s-install*)"
            % (service_id, installed.get("error"), JOB_EVIDENCE, tag)
        )
    started = action(service_id, "start", variant, configuration, tag, timeout)
    steps.append("start: " + str(started.get("state")))
    if started.get("state") != "succeeded":
        raise KitError(
            "%s start failed: %s (evidence %s/ops/%s-start*)"
            % (service_id, started.get("error"), JOB_EVIDENCE, tag)
        )
    return service_row(catalog(), service_id), steps


# ---------------------------------------------------------------------- steps


def step_storage():
    """Models in the shared HF hub cache; temporary space node-local (directive 23)."""
    temporary = os.path.join(env("NODE_TMP", "/tmp"), "clio-tmp")
    body = {
        "root": "",
        "models": env("HF_HUB_CACHE"),
        "service_data": "",
        "captures": "",
        "temporary": temporary,
    }
    if not DRY:
        os.makedirs(temporary, exist_ok=True)
        os.makedirs(body["models"], exist_ok=True)
    before = call("GET", "/v1/infrastructure/targets/local/storage", dry={})
    after = call("PUT", "/v1/infrastructure/targets/local/storage", body, dry={"effective": body})
    return receipt(
        "03-storage",
        "ok",
        requested=body,
        before=before.get("effective"),
        effective=after.get("effective"),
    )


def _snapshot_complete(path):
    """config.json plus every weight shard the index names (symlinks resolved)."""
    if not os.path.isfile(os.path.join(path, "config.json")):
        return False, "config.json missing"
    index = read_json(os.path.join(path, "model.safetensors.index.json"))
    shards = sorted(set(index["weight_map"].values())) if index else []
    if not shards:
        shards = [os.path.basename(p) for p in glob.glob(os.path.join(path, "*.safetensors"))]
    missing = [
        s
        for s in shards
        if not os.path.isfile(os.path.join(path, s)) or os.path.getsize(os.path.join(path, s)) == 0
    ]
    if not shards or missing:
        return False, "missing shards: %s" % (missing[:5] or "none listed")
    size = sum(os.path.getsize(os.path.join(path, s)) for s in shards)
    return True, "%d shards, %.1f GB" % (len(shards), size / 1e9)


def step_model():
    """The model directory vLLM serves: the hub snapshot, else a CLIO download."""
    repo, revision = env("MODEL_REPO"), env("MODEL_REVISION")
    candidates = []
    if env("MODEL_PATH"):
        candidates.append(env("MODEL_PATH"))
    hub = env("HF_HUB_CACHE")
    candidates.append(
        os.path.join(hub, "models--" + repo.replace("/", "--"), "snapshots", revision)
    )
    candidates.append(os.path.join(hub, repo.replace("/", "--") + "--" + revision))
    listing = call(
        "GET", "/v1/infrastructure/targets/local/models", timeout=300, dry={"models": []}
    )
    write_json(os.path.join(JOB_EVIDENCE, "models-inventory.json"), listing)
    for path in candidates:
        ok, why = _snapshot_complete(path) if not DRY else (False, "dry")
        if ok:
            listed = any(row.get("destination") == path for row in listing.get("models", []))
            return receipt(
                "04-model", "ok", source="existing", path=path, detail=why, listed_by_clio=listed
            )
    if DRY:
        call(
            "POST",
            "/v1/infrastructure/targets/local/models",
            {"repository": repo, "revision": revision},
        )
        return receipt("04-model", "dry_run", path=candidates[-1])
    say(
        "downloading %s@%s through CLIO (8B 17.6 GB / 30B 58.5 GB; ~5-60 min via the proxy)"
        % (repo, revision[:12])
    )
    row = call(
        "POST",
        "/v1/infrastructure/targets/local/models",
        {"repository": repo, "revision": revision},
        timeout=300,
    )
    job = row.get("id")
    deadline = time.time() + int(env("MODEL_TIMEOUT_S", "14400"))
    while time.time() < deadline:
        listing = call("GET", "/v1/infrastructure/targets/local/models", timeout=300)
        current = next((r for r in listing.get("models", []) if r.get("id") == job), row)
        state = current.get("state")
        say(
            "model %s: %s %s/%s bytes"
            % (job, state, current.get("bytes_done"), current.get("bytes_total"))
        )
        if state == "ready":
            ok, why = _snapshot_complete(current["destination"])
            if not ok:
                raise KitError("CLIO reports the model ready but " + why)
            return receipt(
                "04-model",
                "ok",
                source="clio_download",
                path=current["destination"],
                acquisition=current,
                detail=why,
            )
        if state in ("failed", "cancelled", "interrupted", "stale", "removed"):
            raise KitError(
                "model download %s: %s %s"
                % (state, current.get("error_code"), current.get("error"))
            )
        time.sleep(30)
    raise KitError("model download still running; rerun 02_services_up.sh to keep waiting")


def step_flowcept():
    variant = env("FLOWCEPT_VARIANT", "native")
    configuration = {
        "port": env("FLOWCEPT_PORT", "8008"),
        "redis_port": env("FLOWCEPT_REDIS_PORT", "16379"),
        "mongo_port": env("FLOWCEPT_MONGO_PORT", "37017"),
    }
    if variant == "native":
        configuration["database_mode"] = "managed"
    else:
        configuration["container_runtime"] = "apptainer"
    row, steps = ensure_service("flowcept", variant, configuration, "flowcept", 3600)
    settings = (row.get("configuration") or {}).get("flowcept_settings", "")
    if not settings or (not DRY and not os.path.isfile(settings)):
        raise KitError("Flowcept is up but its settings file is missing: %r" % settings)
    return receipt(
        "05-flowcept",
        "ok",
        variant=variant,
        steps=steps,
        url=row.get("connection_url"),
        settings_path=settings,
        state=row.get("state"),
    )


def step_vllm():
    flowcept = prior(
        "05-flowcept",
        {"settings_path": "<dry:flowcept settings.yaml>", "url": "http://127.0.0.1:8008"},
    )
    model = prior("04-model", {"path": "<dry:model snapshot>"}).get("path")
    if not model or not flowcept.get("settings_path"):
        raise KitError("run the model and Flowcept steps first (04-model, 05-flowcept receipts)")
    tp = env("VLLM_TP", "1")
    configuration = {
        "model": model,
        "model_revision": env("MODEL_REVISION"),
        "flowcept_settings": flowcept["settings_path"],
        "param.tensor_parallel_size": tp,
        "param.dtype": "bfloat16",
        "param.gpu_memory_utilization": env("VLLM_GPU_UTIL", "0.9"),
        "param.max_num_seqs": env("VLLM_MAX_NUM_SEQS", "1"),
    }
    if env("VLLM_MAX_MODEL_LEN"):
        configuration["param.max_model_len"] = env("VLLM_MAX_MODEL_LEN")
    variant = env("VLLM_VARIANT", "native-cuda-attention")
    try:  # what Fit to GPU will choose for this model and TP (evidence only)
        sizing = call(
            "POST",
            "/v1/infrastructure/services/vllm/context-sizing",
            {"target_id": "local", "variant_id": variant, "configuration": configuration},
            timeout=300,
        )
        write_json(os.path.join(JOB_EVIDENCE, "vllm-context-sizing.json"), sizing)
    except KitError as exc:
        say("context-sizing preview unavailable: %s" % exc)

    def matches(row):
        held = row.get("configuration") or {}
        return (
            held.get("model") == model
            and held.get("param.tensor_parallel_size", "1") == tp
            and held.get("flowcept_settings") == flowcept["settings_path"]
        )

    row, steps = ensure_service("vllm", variant, configuration, "vllm", 5400, matches)
    held = row.get("configuration") or {}
    return receipt(
        "06-vllm",
        "ok",
        variant=variant,
        steps=steps,
        url=row.get("connection_url"),
        model=model,
        tensor_parallel_size=tp,
        captures=held.get("storage.captures"),
        service_directory=held.get("storage.service_directory"),
        effective_parameters=row.get("effective_parameters"),
        connector=env("CLIO_VLLM_ATTN_CONNECTOR", "(CLIO pin)"),
    )


def step_provenance():
    """Flowcept as CLIO's collector with attention capture; a selection needs a restart."""
    flowcept = prior(
        "05-flowcept",
        {"settings_path": "<dry:flowcept settings.yaml>", "url": "http://127.0.0.1:8008"},
    )
    vllm = prior(
        "06-vllm",
        {
            "captures": "<dry:vllm evidence dir>",
            "model": "<dry:model snapshot>",
            "url": "http://127.0.0.1:41234",
        },
    )
    if not vllm.get("captures"):
        raise KitError("06-vllm receipt has no captures directory")
    body = {
        "service_id": "flowcept",
        "label": "Flowcept (%s)" % HOST,
        "url": flowcept.get("url") or "http://127.0.0.1:%s" % env("FLOWCEPT_PORT", "8008"),
        "settings_path": flowcept["settings_path"],
        "attention_files_dir": vllm["captures"],
        "capture_attention": True,
    }
    row = call(
        "POST",
        "/v1/infrastructure/provenance/connections",
        body,
        dry={"id": "dry-conn", "active": False, "selected": False},
    )
    cid = row["id"]
    verified = call(
        "POST", "/v1/infrastructure/provenance/connections/%s/verify" % cid, dry={"verified": True}
    )
    if not verified.get("verified"):
        raise KitError("Flowcept verify did not write+read back: %s" % json.dumps(verified)[:600])
    restarted = False
    if not row.get("active"):
        use = call(
            "POST",
            "/v1/infrastructure/provenance/connections/%s/use" % cid,
            dry={"restart_required": True},
        )
        if use.get("restart_required"):
            say("provenance selection needs a CLIO restart; restarting the owned CLIO")
            serve_stop("07-serve-restart-stop")
            serve_start()
            restarted = True
    listing = call(
        "GET",
        "/v1/infrastructure/provenance/connections",
        dry={"connections": [{"id": cid, "active": True}]},
    )
    current = next((c for c in listing.get("connections", []) if c.get("id") == cid), {})
    providers = call("GET", "/v1/provenance/providers", dry={"default_provider": "flowcept"})
    if current.get("active") and providers.get("default_provider") != "flowcept" and not restarted:
        # A connection activated by an older CLIO left queries on the native journal; using it
        # again makes Flowcept the query source SPOTTER reads (needs one more restart).
        say(
            "provenance queries go to %r; selecting Flowcept again"
            % providers.get("default_provider")
        )
        call(
            "POST",
            "/v1/infrastructure/provenance/connections/%s/use" % cid,
            dry={"restart_required": True},
        )
        serve_stop("07-serve-requery-stop")
        serve_start()
        restarted = True
        providers = call("GET", "/v1/provenance/providers", dry={"default_provider": "flowcept"})
    write_json(os.path.join(JOB_EVIDENCE, "provenance-providers.json"), providers)
    if not current.get("active"):
        raise KitError("provenance connection %s is not active after restart" % cid)
    if providers.get("default_provider") != "flowcept":
        raise KitError(
            "CLIO queries %r, not Flowcept, after using the connection"
            % providers.get("default_provider")
        )
    # vLLM is supervised outside CLIO and should survive the restart; give the
    # restarted CLIO a moment to observe it before deciding to start it again.
    state = ""
    for _ in range(24):
        state = service_row(catalog(), "vllm").get("state")
        if state == "running":
            break
        time.sleep(5)
    if state != "running":
        say("vLLM is %s after the CLIO restart; starting it again through CLIO" % state)
        step_vllm()
    return receipt(
        "07-provenance",
        "ok",
        connection_id=cid,
        restarted=restarted,
        verification=verified.get("verification"),
        vllm_state=state,
        query_default=providers.get("default_provider"),
    )


def step_bind():
    """'Use in Models': save the managed vLLM as a model server (its key stays server-side)."""
    vllm = prior(
        "06-vllm",
        {
            "captures": "<dry:vllm evidence dir>",
            "model": "<dry:model snapshot>",
            "url": "http://127.0.0.1:41234",
        },
    )
    url = service_row(catalog(), "vllm").get("connection_url") or vllm.get("url")
    if not url:
        raise KitError("vLLM has no connection URL in the catalog")
    address = url.rstrip("/") + ("" if url.rstrip("/").endswith("/v1") else "/v1")
    dry_saved = {
        "id": "vllm",
        "credential_ref": "managed-server:local:vllm",
        "check": {"reachable": True, "models": [vllm.get("model")]},
    }
    servers = call("GET", "/v1/providers/servers", dry={"servers": []}).get("servers", [])
    held = next(
        (
            s
            for s in servers
            if s.get("preset_id") == "vllm" and (s.get("address") or "").rstrip("/") == address
        ),
        None,
    )
    if held:  # rerun: recheck the saved server instead of saving it twice
        saved = call("POST", "/v1/providers/servers/%s/check" % held["id"], dry=dry_saved)
    else:
        saved = call(
            "POST",
            "/v1/providers/servers",
            {"address": address, "preset_id": "vllm"},
            dry=dry_saved,
        )
    write_json(os.path.join(JOB_EVIDENCE, "bind-server.json"), saved)
    check = saved.get("check") or {}
    models = check.get("models") or []
    if not check.get("reachable") or not models:
        raise KitError(
            "the saved vLLM server is not reachable or lists no model: %s" % json.dumps(check)[:600]
        )
    if not saved.get("credential_ref"):
        say("WARNING: the saved server carries no managed key reference; requests will 401")
    model_id = next((m for m in models if m == vllm.get("model")), models[0])
    provider_models = call(
        "GET", "/v1/providers/%s/models" % saved.get("id", "vllm"), timeout=300, dry={}
    )
    write_json(os.path.join(JOB_EVIDENCE, "provider-models.json"), provider_models)
    return receipt(
        "08-bind",
        "ok",
        server_id=saved.get("id"),
        address=address,
        model={"provider_id": saved.get("id", "vllm"), "model_id": model_id},
        credential_ref=saved.get("credential_ref"),
    )


def _session_model():
    bound = prior("08-bind", {"model": {"provider_id": "vllm", "model_id": "<dry:model>"}})
    if not bound.get("model"):
        raise KitError("no bound model (08-bind receipt); run 02_services_up.sh")
    return bound["model"]


def _workspace(name, root):
    if not DRY:
        os.makedirs(root, exist_ok=True)
    listing = call("GET", "/v1/workspaces", dry={"workspaces": []})
    held = next(
        (
            w
            for w in listing.get("workspaces", [])
            if w.get("root_path") == root and w.get("name") == name
        ),
        None,
    )
    if held:
        return held
    return call(
        "POST", "/v1/workspaces", {"name": name, "root_path": root}, dry={"id": "ws_dry_" + name}
    )


def _session(workspace_id, title, agent_ref, approval):
    body = {
        "workspace_id": workspace_id,
        "title": title,
        "approval_mode": approval,
        "model": _session_model(),
    }
    if agent_ref:
        body["agent"] = {"id": agent_ref}
    return call("POST", "/v1/sessions", body, dry={"id": "sess_dry_" + uuid.uuid4().hex[:8]})


def _post(session_id, text, approval):
    key = str(uuid.uuid4())
    body = {
        "client_message_id": key,
        "idempotency_key": key,
        "delivery": "start",
        "model": _session_model(),
        "parts": [{"type": "text", "text": text}],
        "behavior": {"execution_mode": "execute", "confirmation_policy": approval},
    }
    return call(
        "POST",
        "/v1/sessions/%s/messages" % session_id,
        body,
        dry={"message_id": "msg_user_dry", "state": "started"},
    )


def _settled(session_id, user_message_id, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages = call(
            "GET",
            "/v1/sessions/%s/messages" % session_id,
            dry={
                "messages": [
                    {"id": user_message_id, "role": "user", "turn_id": "t"},
                    {
                        "id": "msg_asst_dry",
                        "role": "assistant",
                        "turn_id": "t",
                        "stop_reason": "end_turn",
                        "parts": [{"id": "part_dry", "type": "text", "text": "dry"}],
                    },
                ]
            },
        )
        rows = messages.get("messages", [])
        user = next((r for r in rows if r.get("id") == user_message_id), None)
        turn = (user or {}).get("turn_id")
        replies = [
            r
            for r in rows
            if turn
            and r.get("turn_id") == turn
            and r.get("role") == "assistant"
            and (r.get("stop_reason") or r.get("error_info"))
        ]
        if len(replies) > 1:
            raise KitError(
                "turn %s has %d completed assistant messages; inspect it in CLIO"
                % (turn, len(replies))
            )
        if replies:
            return replies[0]
        state = call("GET", "/v1/sessions/" + session_id, dry={"status": "idle"})
        if state.get("status") in ("waiting_user", "waiting_permission", "error", "cancelled"):
            raise KitError("session %s is %s; open it in CLIO" % (session_id, state.get("status")))
        time.sleep(3)
    raise KitError("no settled reply in %s s (session %s)" % (timeout, session_id))


def _safetensors_header(path):
    with open(path, "rb") as handle:
        (length,) = struct.unpack("<Q", handle.read(8))
        header = json.loads(handle.read(min(length, 1 << 22)).decode())
    meta = header.pop("__metadata__", {})
    return {
        "metadata": meta,
        "tensors": {
            k: {"shape": v.get("shape"), "dtype": v.get("dtype")} for k, v in header.items()
        },
    }


def step_smoke():
    """ONE attention turn on the TP deployment: the gate before any OPAL work."""
    vllm = prior(
        "06-vllm",
        {
            "captures": "<dry:vllm evidence dir>",
            "model": "<dry:model snapshot>",
            "url": "http://127.0.0.1:41234",
        },
    )
    workspace = _workspace("attention-smoke", os.path.join(ROOT, "workspace-smoke"))
    session = _session(
        workspace["id"], "Polaris attention smoke (TP=%s)" % env("VLLM_TP", "1"), "", "auto-edits"
    )
    prompt = env(
        "SMOKE_PROMPT",
        "In three sentences, what does a dose-response curve show, "
        "and why can its peak sit at an intermediate dose?",
    )
    started = time.time()
    ack = _post(session["id"], prompt, "auto-edits")
    reply = _settled(session["id"], ack["message_id"], int(env("SMOKE_TIMEOUT_S", "1800")))
    seconds = round(time.time() - started, 1)
    text = next((p for p in reply.get("parts", []) if p.get("type") == "text"), {})
    availability = call(
        "GET",
        "/v1/sessions/%s/attention/availability" % session["id"],
        dry={"enabled": True, "messages": {reply.get("id"): True}},
    )
    attention = call(
        "POST",
        "/v1/sessions/%s/messages/%s/attention" % (session["id"], reply["id"]),
        {"part_id": text.get("id", ""), "field": "text"},
        timeout=600,
        dry={"available": True},
    )
    write_json(os.path.join(JOB_EVIDENCE, "smoke-attention.json"), attention)
    # The per-item view (default profile attn-analytics, Bogdan's formula) on the answer's
    # first sentence: what OPAL's chain export reads for each answer item.
    answer = (text.get("text") or "").strip()
    item = answer.split(".")[0][:200] or answer[:200]
    items = call(
        "POST",
        "/v1/sessions/%s/messages/%s/attention/items" % (session["id"], reply["id"]),
        {"items": [{"text": item}]},
        timeout=600,
        dry={"available": True},
    )
    write_json(os.path.join(JOB_EVIDENCE, "smoke-attention-items.json"), items)
    write_json(os.path.join(JOB_EVIDENCE, "smoke-reply.json"), reply)
    captures = []
    if vllm.get("captures") and not DRY:
        files = sorted(
            glob.glob(os.path.join(vllm["captures"], "**", "*.safetensors"), recursive=True),
            key=os.path.getmtime,
        )[-4:]
        for path in files:
            row = {"path": path, "bytes": os.path.getsize(path)}
            try:
                row.update(_safetensors_header(path))
            except (OSError, ValueError, struct.error) as exc:
                row["header_error"] = str(exc)
            captures.append(row)
    available = attention.get("available") is True and items.get("available") is True
    details = {
        "session_id": session["id"],
        "workspace_id": workspace["id"],
        "assistant_message_id": reply.get("id"),
        "stop_reason": reply.get("stop_reason"),
        "error_info": reply.get("error_info"),
        "turn_seconds": seconds,
        "answer_chars": len(text.get("text") or ""),
        "thinking_separated": any(p.get("type") == "thinking" for p in reply.get("parts", [])),
        "availability": availability,
        "attention_available": available,
        "attention_reason": attention.get("reason"),
        "attention_message": attention.get("message"),
        "items_available": items.get("available"),
        "items_reason": items.get("reason"),
        "items_profile": (items.get("profile") or {}).get("name"),
        "response_id": attention.get("response_id"),
        "capture_sha256": attention.get("capture_sha256"),
        "lm_call_id": attention.get("lm_call_id"),
        "recent_capture_files": captures,
        "tensor_parallel_size": env("VLLM_TP", "1"),
    }
    status = "ok" if available and reply.get("stop_reason") == "end_turn" else "failed"
    receipt("09-attention-smoke", status, **details)
    if status != "ok":
        raise KitError(
            "attention smoke failed: available=%s reason=%s stop=%s (see %s)"
            % (
                attention.get("available"),
                attention.get("reason"),
                reply.get("stop_reason"),
                JOB_EVIDENCE,
            )
        )


def _agent_ref(agent_id):
    agents = call(
        "GET", "/v1/agents", dry={"agents": [{"id": agent_id, "ref": "clio/" + agent_id}]}
    )
    write_json(os.path.join(JOB_EVIDENCE, "agents.json"), agents)
    row = next((a for a in agents.get("agents", []) if a.get("id") == agent_id), None)
    if not row:
        raise KitError(
            "agent %r is not installed; GET /v1/agents lists %s"
            % (agent_id, [a.get("id") for a in agents.get("agents", [])])
        )
    return row["ref"]


def step_opal_setup():
    """OPAL workspace + read-only source + two fresh APPL-CORE sessions with SPOTTER."""
    export = env("OPAL_EXPORT")
    if not DRY and not os.path.isdir(export):
        raise KitError("OPAL export %s is missing; run 04_stage_opal_data.sh" % export)
    sessions_file = os.path.join(EVIDENCE, "opal", "opal-sessions.json")
    held = read_json(sessions_file, {}) or {}
    workspace = _workspace("opal-polaris", os.path.join(ROOT, "workspace-opal"))
    sources = call("GET", "/v1/workspaces/%s/sources" % workspace["id"], dry={"sources": []})
    # CLIO stores the resolved root, so compare resolved paths (Eagle paths hold symlinks).
    real_export = os.path.realpath(export)
    source = next(
        (
            s
            for s in sources.get("sources", [])
            if real_export in (s.get("root"), (s.get("source") or {}).get("root"))
        ),
        None,
    )
    if source is None:
        source = call(
            "POST",
            "/v1/workspaces/%s/sources" % workspace["id"],
            {
                "provider": "local",
                "root": export,
                "mode": "read_only",
                "label": "OPAL export (staged from the public Drive folder, read-only)",
            },
            dry={"id": "src_dry"},
        )
    linked = None
    if env("OPAL_LINK_SOURCE", "1") == "1":  # the virtual read-only folder (no copy)
        try:
            linked = call(
                "POST",
                "/v1/workspaces/%s/sources/%s/link" % (workspace["id"], source["id"]),
                timeout=300,
                dry={"indexing_operation": "dry"},
            )
            linked = {"indexing_operation": linked.get("indexing_operation")}
        except KitError as exc:
            linked = {"error": str(exc)}
            say(
                "linking the OPAL source failed (continuing, the agent can still reach it): %s"
                % exc
            )
    refs = {
        "chain": _agent_ref(env("OPAL_CHAIN_AGENT", "appl-core")),
        "broad": _agent_ref(env("OPAL_AGENT", "appl-core")),
    }
    spotter = call("GET", "/v1/spotter/availability", dry={"available": True})
    if not spotter.get("available") and env("ALLOW_SPOTTER_UNAVAILABLE", "0") != "1":
        raise KitError(
            "SPOTTER unavailable: %s %s (remedy: %s)"
            % (spotter.get("reason"), spotter.get("message"), spotter.get("remedy"))
        )
    conditions = env("OPAL_CONDITIONS", "A B").split()
    kinds = ["chain_" + c for c in conditions] + ["broad"]
    refs.update({k: refs["chain"] for k in kinds if k.startswith("chain_")})
    titles = {
        k: "OPAL chain %s condition %s (Polaris)" % (env("OPAL_CHAIN_ID"), k[6:])
        for k in kinds
        if k.startswith("chain_")
    }
    titles["broad"] = "OPAL broad SPOTTER investigation (Polaris)"
    for kind in kinds:
        sid = held.get(kind)
        if sid:
            try:
                call("GET", "/v1/sessions/" + sid, dry={"id": sid})
                continue
            except RestError as exc:
                if exc.status != 404:
                    raise
        held[kind] = _session(workspace["id"], titles[kind], refs[kind], "spotter-ai")["id"]
    held.update(
        {
            "workspace_id": workspace["id"],
            "source_id": source.get("id"),
            "link": linked,
            "model": _session_model(),
            "endpoint": base(),
            "agents": refs,
        }
    )
    for kind in kinds:
        held["spotter_" + kind] = call(
            "GET", "/v1/spotter/availability?session_id=" + held[kind], dry={"available": True}
        )
    write_json(sessions_file, held)
    return receipt("10-opal-setup", "ok", **held)


def step_opal_report(kind):
    held = read_json(os.path.join(EVIDENCE, "opal", "opal-sessions.json")) or {}
    sid = held.get(kind) or ("sess_dry_" + kind if DRY else "")
    if not sid:
        raise KitError("no %s session recorded; run opal-setup" % kind)
    availability = call("GET", "/v1/sessions/%s/attention/availability" % sid, dry={})
    messages = call("GET", "/v1/sessions/%s/messages" % sid, dry={"messages": []}).get(
        "messages", []
    )
    answers = [m for m in messages if m.get("role") == "assistant"]
    flags = availability.get("messages") or {}
    return receipt(
        "12-opal-%s-report" % kind,
        "ok",
        session_id=sid,
        assistant_messages=len(answers),
        attention_available=sum(1 for v in flags.values() if v),
        attention_enabled=availability.get("enabled"),
        attention_reason=availability.get("reason"),
        spotter=call("GET", "/v1/spotter/availability?session_id=" + sid, dry={}),
    )


def step_teardown_services():
    """Stop every running CLIO-managed service on this target, model servers first."""
    inventory = call(
        "GET",
        "/v1/infrastructure/inventory",
        dry={
            "services": [
                {
                    "service_id": "vllm",
                    "variant_id": "native-cuda-attention",
                    "target_id": "local",
                    "state": "running",
                }
            ]
        },
    )
    order = {"vllm": 0, "flowcept": 2, "cmf": 2}
    running = sorted(
        (
            r
            for r in inventory.get("services", [])
            if r.get("target_id") == "local" and r.get("state") == "running"
        ),
        key=lambda r: order.get(r.get("service_id"), 1),
    )
    results = []
    for row in running:
        try:
            done = action(
                row["service_id"],
                "stop",
                row["variant_id"],
                {},
                "teardown-" + row["service_id"],
                600,
            )
            results.append(
                {
                    "service": row["service_id"],
                    "state": done.get("state"),
                    "error": done.get("error"),
                }
            )
        except KitError as exc:
            results.append({"service": row["service_id"], "state": "error", "error": str(exc)})
    status = "ok" if all(r["state"] == "succeeded" for r in results) else "partial"
    return receipt("13-teardown-services", status, stopped=results)


def step_status():
    serve = _owned_serve() or {}
    out = {"serve": serve, "healthy": healthy()}
    if out["healthy"]:
        cat = catalog()
        out["services"] = {r["id"]: r.get("state") for r in cat.get("services", [])}
    print(json.dumps(out, indent=2))


def cmd_receipt(argv):
    """receipt STEP STATUS [key=value ...] (values that parse as JSON stay typed)."""
    details = {}
    for item in argv[2:]:
        key, _, value = item.partition("=")
        try:
            details[key] = json.loads(value)
        except ValueError:
            details[key] = value
    receipt(argv[0], argv[1], **details)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_opal(manifest_path, dest):
    """Download the OPAL tabular core by Drive id, verify sha256, then make it read-only."""
    manifest = read_json(manifest_path)
    if not manifest:
        raise KitError("cannot read %s" % manifest_path)
    rows, bad, fetched = manifest["rows"], [], 0
    for row in rows:
        path = os.path.join(dest, row["path"])
        if (
            os.path.isfile(path)
            and os.path.getsize(path) == row["bytes"]
            and (DRY or file_sha256(path) == row["sha256"])
        ):
            continue
        url = (
            "https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t"
            % (row["drive_id"])
        )
        if DRY:
            print("DRY-RUN curl -fL %s -o %s" % (url, path))
            continue
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o755)
        tmp = path + ".part"
        result = subprocess.call(
            ["curl", "-fsSL", "--retry", "4", "--retry-delay", "5", "-o", tmp, url]
        )
        if result != 0 or not os.path.isfile(tmp) or file_sha256(tmp) != row["sha256"]:
            bad.append(row["path"])
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        if os.path.exists(path):
            os.chmod(path, 0o644)
        os.replace(tmp, path)
        fetched += 1
    if not DRY:
        for top, _dirs, files in os.walk(dest):
            for name in files:
                os.chmod(os.path.join(top, name), 0o444)
        for top, _dirs, _files in os.walk(dest, topdown=False):
            os.chmod(top, 0o555)
    total = sum(r["bytes"] for r in rows)
    status = "ok" if not bad else "failed"
    receipt(
        "11-opal-data",
        status,
        dest=dest,
        files=len(rows),
        fetched=fetched,
        failed=bad,
        bytes=total,
        manifest=manifest_path,
        read_only=not DRY,
    )
    if bad:
        raise KitError("%d OPAL files failed download or sha256: %s" % (len(bad), bad[:5]))


COMMANDS = {
    "serve-start": lambda a: serve_start(),
    "serve-stop": lambda a: serve_stop(),
    "serve-restart": lambda a: (serve_stop(), serve_start()),
    "health": lambda a: sys.exit(0 if healthy() else 1),
    "storage": lambda a: step_storage(),
    "model": lambda a: step_model(),
    "flowcept": lambda a: step_flowcept(),
    "vllm": lambda a: step_vllm(),
    "provenance": lambda a: step_provenance(),
    "bind": lambda a: step_bind(),
    "smoke": lambda a: step_smoke(),
    "opal-setup": lambda a: step_opal_setup(),
    "opal-report": lambda a: step_opal_report(a[0]),
    "stage-opal": lambda a: stage_opal(a[0], a[1]),
    "teardown-services": lambda a: step_teardown_services(),
    "status": lambda a: step_status(),
    "receipt": cmd_receipt,
    "session-id": lambda a: print(
        (read_json(os.path.join(EVIDENCE, "opal", "opal-sessions.json")) or {}).get(
            a[0], "sess_dry_" + a[0] if DRY else ""
        )
    ),
    "endpoint": lambda a: print(base()),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.stderr.write("usage: kit.py {%s} [args]\n" % ",".join(sorted(COMMANDS)))
        sys.exit(2)
    name = sys.argv[1]
    try:
        COMMANDS[name](sys.argv[2:])
    except KitError as exc:
        say("FAILED %s: %s" % (name, exc))
        if name not in ("receipt", "health", "status", "session-id", "endpoint"):
            receipt("failed-" + name, "failed", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
