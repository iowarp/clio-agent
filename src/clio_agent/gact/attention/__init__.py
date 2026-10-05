"""Attention view: which prompt regions a generated answer attended to.

Owner package for the SPOTTER-AI attention surface. Two halves:

* **write path** (:mod:`.declare`): on vLLM calls, CLIO renders the request with
  the model's own tokenizer + chat template, declares one token range per
  transcript section as ``kv_transfer_params.ranges`` (the vllm-attn-connector
  per-request channel), and records the labelled ranges on its own ``lm.call``
  provenance record next to ``response_id``;
* **read path** (:mod:`.service`): a typed query layer that finds the ``lm.call``
  that produced a transcript message, queries the connector's attention records
  from Flowcept by response id, and shapes per-section / per-token importance
  for the UI.

Every unavailable path returns a typed :class:`.reasons.AttentionUnavailable`;
there is no silent fallback. The storage contract the read path expects is
spelled out in :mod:`.contract`.
"""
