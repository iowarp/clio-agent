"""Per-server-type dialect adapters (model-capabilities brief Part 6 / P4b).

Each submodule is a handful of HTTP reads mapped onto
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities` /
:class:`~clio_agent.providers.capabilities.records.EndpointCapabilities` /
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities`
fields, plus that dialect's own invalidation fingerprint (brief 5.6). This is
the ONLY place server-SOFTWARE knowledge (which fields llama.cpp's ``/props``
happens to expose, how Ollama spells a Modelfile parameter) is allowed to
live in code -- never a fact about any one model.

Submodules:

* :mod:`.llama_cpp` -- single-model ``/props`` + ``/v1/models``, and router-mode
  ``/models`` (``architecture.input_modalities`` + the ``status.args`` parser).
* :mod:`.vllm` -- ``/v1/models`` (``max_model_len``, ``root``) + ``GET /version``.
* :mod:`.ollama` -- the official ``ollama`` client's ``show``/``ps``, plus
  ``GET /api/version`` (no client method covers that one).
* :mod:`.lm_studio` -- ``GET /api/v1/models`` with a ``GET /api/v0/models``
  fallback.
* :mod:`.openrouter` -- ``GET /api/v1/models`` (model + deployment fields,
  ``supported_parameters`` -> ``route_params`` directly).
* :mod:`.litellm_proxy` -- ``/v1/models`` + ``/v1/model/info``, narrowing a
  multi-deployment alias to the intersection of capabilities / minimum limits.
* :mod:`.cloud` -- NVIDIA NIM / Azure / Bedrock / Vertex / Gemini / OpenAI /
  Anthropic: "no restriction" deployment defaults (brief Part 4.2).
* :mod:`.alcf` -- best-effort per-job vLLM reads layered on the existing
  ``/jobs`` discovery (:class:`clio_agent.providers.handshake.argonne.
  ArgonneHandshake`).
* :mod:`.probes` -- the active tools/vision/reasoning/parameter probes (brief
  5.3), run only during discovery/refresh for local and self-hosted endpoints.
"""

from __future__ import annotations

__all__: list[str] = []
