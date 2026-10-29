# Resources created by part D (cloud live check), 2026-10-26

## Left running on purpose

| Resource | Identity at the end | Notes |
|---|---|---|
| GACT server | 127.0.0.1:8787, pid 28024 (process group in `.cloud-run/agent.pid`) | `bash scripts/cloud/stack.sh stop` stops it. It runs fix 46897d8. |
| gact-tui dev UI | 127.0.0.1:5173, vite pid 28519 (`.cloud-run/ui.pid`) | stack.sh |
| clio-core daemon | `clio_run` pid 28386 on 9413 and 9416 | started by the GACT server |
| Flowcept, Docker variant (CLIO-managed) | supervisor pid 16828, Flowcept app pid 16841 on 127.0.0.1:8008; containers `4a714b0c5f69` `clio-flowcept-5e260e099892-mongo` (127.0.0.1:37017) and `99f992d18de4` `clio-flowcept-5e260e099892-redis` (127.0.0.1:16379) | To stop it through CLIO: `POST /v1/infrastructure/services/flowcept/actions {"action":"stop","variant_id":"managed","configuration":{"container_runtime":"docker"}}` |
| Flowcept provenance connection | `77762d7b-1d73-4ca3-ba89-5eb7c2d4eef6`, active | `~/.config/clio-agent/config.yaml`: `provenance.agentic.providers: [jsonl, flowcept]`, Flowcept `settings_path`, and `query_default: flowcept`. I set `query_default` myself; the file before that change is in my scratchpad. |
| Docker images | `redis@sha256:02f2cc48…`, `mongo@sha256:02a0cc79…` | used by the running Flowcept |
| Workspace | `ws_bbfeb32ee180` → `/home/user/clio-cloudlive-ws` (`greenhouse_trial.csv`, `greenhouse_trial_clean.csv`) | kept because the evidence sessions refer to it; it is disposable |
| Sessions | `sess_5f6eea93ae15` (disarmed and cancelled), `sess_5a093f1eed32` and `sess_ca1ea7ca60f0` (SPOTTER armed, watchers idle and yielded) | |

## Created and removed

| Resource | Removal |
|---|---|
| Flowcept native record (valkey 16379, mongod 37017, Flowcept 8008) | stopped and uninstalled through CLIO; no processes left (checked with `pgrep`). Its database files remain in the shared service directory by design. |
| dockerd registry mirror (`/etc/docker/daemon.json`) | reset with `{"registry-mirrors":[]}` and SIGHUP, then the file was deleted. `docker info` shows no mirrors. dockerd (pid 3821) was never stopped. |
| Valkey 8.1.3 shallow clone and reproduced archive (`/home/user/clio-cloudlive-src`) | removed. The checksum-verified tarball stays in the Flowcept service's own `cache/downloads/`. |
| Approver helper (`approver.py`, pid 10676) | had already exited; confirmed gone |
| UI screenshots `.cloud-run/shots/cloudlive*.png` | removed after the selected ones were copied to `screens/` as JPEG |
| SPOTTER wake loop in `sess_5f6eea93ae15` | stopped by PATCH `approval_mode: ask` and `POST /cancel` |
