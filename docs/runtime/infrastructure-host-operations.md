# Infrastructure host operations

Host inspection is the source of OS, architecture, accelerators, container availability,
hostname and home-directory facts. A connected SSH transport does not establish that
every service on that host is healthy. Allocation and scheduler preparation remain the
operator's responsibility; CLIO inspects the prepared execution host.

## Native and container installation methods

Choose an installation method before configuring the service. Native and Containers
qualify independently against the selected computer. A missing Docker daemon must not
disable a native driver; an unsupported native driver must not hide a usable container
driver. The service chooser reports both methods before opening setup. Unavailable
options retain their driver-specific reasons. An installed service keeps its recorded
method until its runtime is removed; changing configuration does not migrate its data
or silently switch its installation method.

The current driver matrix is explicit:

| Service | Native managed installation | Container requirements |
| --- | --- | --- |
| vLLM | Linux NVIDIA CUDA or Intel XPU, with uv; XPU uses the site's vLLM build | Docker, Podman or Apptainer; accelerator-specific variants remain host-qualified |
| llama.cpp | Windows CPU/Vulkan/ROCm/CUDA; Linux source builds for CPU/Vulkan/ROCm/CUDA; macOS CPU/Apple Metal | Docker, Podman or Apptainer, independently qualified |
| Ollama | Windows/Linux/macOS pinned native binaries; CPU/Vulkan/ROCm/CUDA or Apple Metal as supported by the host | Docker, Podman or Apptainer, independently qualified |
| Model router | Linux/macOS with uv | None |
| SearXNG | Windows/Linux/macOS with uv, within the native driver's architecture constraints | None |
| CLIO Web Search | Windows/Linux/macOS gateway with a private native SearXNG backend; optional document processing uses a supplied GROBID service | Docker, Podman or Apptainer |
| Flowcept / HPE CMF | Linux x86-64 application with managed native databases or site database settings | Existing hybrid application/container database stack remains selectable |
| CLIO Relay | Released uv tool | None |
| Remote CLIO | Published bootstrap on a Linux SSH host | None |

CPU container availability is not the same as native Linux support: the image runs
inside the engine. CPU vLLM and Ollama therefore do not reject a Windows host solely
because its host OS is Windows. This does not enable unqualified GPU pass-through,
claim that every binary or image has been run on every host.

Native llama.cpp and Ollama install their runtimes before a model is chosen. Starting
llama.cpp requires one local GGUF or Hugging Face GGUF reference; Ollama prepares its
selected model before readiness succeeds. Model caches honor the host storage settings
or an explicit deployment cache. Removing the runtime retains model data, database
files, logs and evidence; deleting retained service data is a separate confirmed action.
Shared model caches outside the service directory are not deleted by that action.

AMD discovery uses the host GPU inventory, including Windows PCI vendor IDs and Linux
sysfs when ROCm utilities are absent. Windows AMD defaults to Vulkan and offers ROCm;
Linux AMD defaults to ROCm and also offers Vulkan. Linux source builds require a C++
compiler and the selected GPU development SDK. Installation checks the selected
llama.cpp backend's real device list; choosing a GPU variant does not silently fall
back to CPU. Context sizing reads the installed backend's device memory on Windows too.

Container-free monitoring uses `uv`, a C compiler and `make` for managed databases.
CLIO builds Valkey/PostgreSQL in its owned environment (bootstrapping missing parser
build tools there), and downloads a verified MongoDB build matching the supported
distribution. Unsupported managed MongoDB distributions need site databases. No root
installation or system package manager is used. Native database listeners bind to
loopback, use private credentials and stop with their owning application. Credentials
and database files survive switching to site databases and back.

For site databases, choose **Site-provided** and supply an absolute JSON file on the
execution host with mode `0600`. Flowcept requires `redis_url` and `mongodb_uri`; CMF
requires `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and
`POSTGRES_DB`, all string values. These secrets stay on that host and are excluded
from public manifests and lifecycle receipts. Site databases remain under site control.

Verification performs real operations: a Web Search query must return URLs; Flowcept
must retain a newly published task through its query API; CMF must retain an execution
and both input/output artifact edges through its push/pull APIs. A listening port alone
does not count as verification.

## Storage and terminal output

Structured host replies remove actual CSI/OSC terminal decoration before parsing.
This prevents SSH cursor-control output from becoming part of a storage root. JSON
escaped path content is preserved. Unexpected remaining control characters produce a
short actionable error before validating the individual storage locations.

Windows extended path prefixes remain available to filesystem operations. The client
displays ordinary drive/UNC paths. Desktop's managed local connection uses its native
directory dialog; remote hosts and tunneled connections use the selected server's
folder browser.

## Managed model downloads

Linux and Windows execution hosts support durable model acquisitions. The target
needs `uv`; local control uses the running CLIO Python executable, and remote control
uses the platform's Python. Downloading files does not start a model or require a
container runtime. Immutable revisions, free-space checks, hashes, partial-cache reuse
and the existing root registry remain authoritative.

Windows uses a byte-range control lock and queries process creation identity before
reusing an owner. Cancellation writes the existing durable marker; the verified worker
observes it and settles the cancelled receipt, preserving cached bytes. It never kills
an unrelated PID tree. Linux retains its process-group cancellation behavior.

## Provider and protected-execution health

Configuration source strings such as `env:CLIO_LM_MODEL` describe where the default
provider configuration came from. They do not establish an authentication failure.
Codex credentials alone are not a verified connection. A fresh successful live handshake
clears the warning; static model metadata cannot do so. The health projection reads the
existing handshake cache without making another network request. Provider setup exposes
the model refresh that performs that check.

Protected-execution setup returns its actual verdict to the client, which displays it
and refreshes the dedicated sandbox status and general health projection. Elevation
consent alone is not enforcement proof; the existing native conformance checks remain
required.

## Qualification

CI's installation-time helper provisioning can receive an explicit scoped
`CLIO_CODEX_RELEASE_TOKEN` from the workflow's read-only token, avoiding the anonymous
GitHub API limit on shared runners. Authorization applies only to the fixed release
metadata endpoint: metadata redirects fail, asset downloads receive no token, and the
receipt stores no token. Version, architecture, size and hash validation remain required.
No unrelated `GH_TOKEN`, provider account or global sign-in is reused.

The Windows process-query function also guards its own platform-specific body, so Linux
type checks do not resolve an unavailable Windows API. Four additional focused cases
cover metadata authorization/redirect containment, explicit installer token forwarding
and the real Windows process/lock path. Linux-targeted Mypy and scoped Pyright pass.

Nine focused backend cases pass individually with one worker. They cover decorated SSH
paths, invalid-path diagnostics, actual Windows locking/process identity with a test-only
launch seam, cancellation checkpoints, platform-aware API commands, handshake health,
hash verification, PID reuse and recorded-root retry/cancel behavior. This qualification
does not claim a fresh native UAC setup, live Ares model download or provider inference.
