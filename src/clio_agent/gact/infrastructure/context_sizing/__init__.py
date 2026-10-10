"""Context sizing applied to CLIO-managed model servers.

* :mod:`.deployment` -- the value becomes the launch setting (vLLM
  ``--max-model-len``, llama.cpp ``--ctx-size``, Ollama ``num_ctx``);
* :mod:`.preview` -- the control's values for a deployment form, before deploying;
* :mod:`.target_probe` -- the standalone host-side read of the model and GPUs.

The engine-neutral core is :mod:`clio_agent.context_sizing`.
"""
