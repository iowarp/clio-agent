# Local vLLM CPU qualification (2026-09-04)

This qualification used the official vLLM 0.28.0 x86 CPU release wheel on WSL2
Ubuntu and the host's AVX2 CPU. The [vLLM CPU installation guide](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/)
documents Linux with AVX2 as limited-feature support and release CPU wheels from
0.17.0 onward.

The model was `Qwen/Qwen2.5-0.5B-Instruct` at immutable revision
`7ae557604adf67be50417f59c2c2f167def9a775`, served only as
`clio-local-qualification`. Its checkpoint was 0.92 GiB. The D-owned root was
`D:\Libraries\Documents\projects\clio_develop_workspace\vllm-qualification\20260904`.
At completion it contained 70,909 files and 7,294,761,493 logical bytes (6.79
GiB), including 999,683,322 model-cache bytes. Logical bytes include uv
cache/environment duplication and are not allocated-disk measurements.

The pinned wheel installation was:

```sh
uv pip install --python /mnt/d/Libraries/Documents/projects/clio_develop_workspace/vllm-qualification/20260904/venv/bin/python \
  https://github.com/vllm-project/vllm/releases/download/v0.28.0/vllm-0.28.0+cpu-cp38-abi3-manylinux_2_34_x86_64.whl \
  --torch-backend cpu
```

The successful server used `float32`, eager execution, a 512-token context, one
sequence, and a 1 GiB CPU KV cache:

```sh
VLLM_CPU_KVCACHE_SPACE=1 \
TMPDIR=/tmp/clio-vllm-20260904 \
/mnt/d/Libraries/Documents/projects/clio_develop_workspace/vllm-qualification/20260904/venv/bin/vllm serve \
  Qwen/Qwen2.5-0.5B-Instruct \
  --revision 7ae557604adf67be50417f59c2c2f167def9a775 \
  --served-model-name clio-local-qualification \
  --host 127.0.0.1 --port 18080 --dtype float32 \
  --max-model-len 512 --max-num-seqs 1 --enforce-eager
```

All environments, wheels, models, Hugging Face data, compilation data, and logs
were placed on D:. vLLM's ZeroMQ engine requires AF_UNIX sockets, which Windows
DrvFS does not support, so only the transient IPC directory used WSL's Linux
`/tmp`. It had a task ownership marker and was removed after server shutdown.

The real Agent factory smoke was:

```powershell
uv run --no-sync python -B scripts/qualify_vllm_local.py `
  --output D:\Libraries\Documents\projects\clio_develop_workspace\vllm-qualification\20260904\vllm-agent-smoke.json
```

Both `/v1/chat/completions` requests returned HTTP 200 and `['OK']`. The default
`max_tokens=0` call omitted `max_tokens` from the provider configuration and took
6.061 seconds. The explicit `max_tokens=16` call preserved the value and took
0.529 seconds. Each Agent LM recorded one history entry. Exact evidence is in
`vllm-agent-smoke.json` under the D-owned qualification root.

Two failed starts are retained as negative evidence. Passing `--device cpu` to
vLLM 0.28.0 is invalid because that option now expects integer device IDs; the
CPU wheel selects its platform automatically. A long D-owned `TMPDIR` exceeded
ZeroMQ's 107-character Unix-socket limit, while short `/mnt/d` storage rejected
AF_UNIX with `Operation not supported`. Neither failure downloaded another model,
changed drivers, touched Docker, or installed global WSL packages.

Initial imports from the D: 9P/DrvFS environment took several minutes before
model handling began. The model download itself took 40.752 seconds and loading
the 0.92 GiB checkpoint took 5.26 seconds. These local timings are operational
evidence, not HPC performance results.
