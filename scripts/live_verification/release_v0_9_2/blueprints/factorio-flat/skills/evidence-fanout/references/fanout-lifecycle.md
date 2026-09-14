# Evidence fan-out lifecycle

Define the evidence question, decision it informs, scope, time horizon, source
families, terminology already known, and exclusions. Split it into lanes that can
be investigated independently without duplicating the whole question. Useful
lanes may cover mechanisms, measurements, material/condition regimes, competing
methods, standards, datasets, or counterevidence; choose them from the question,
not a fixed template.

Launch independent `evidence_leaf` assignments together with
`spawn_agents_parallel`. Preserve every returned task id and collect them with
`wait_agent_tasks`. Read all settled packets before deciding whether the evidence
is mature. Follow new terminology, cited primary work, contradictions, missing
conditions, or method branches only when they could change the scientific
decision. A follow-up may be one leaf or another independent fan-out.

When the claim map and source audit are coherent, spawn one `evidence_critic`
with the entire draft, citations, uncertainties, and audit. The critic must make
fresh web checks. If it returns `RESEARCH_REQUIRED`, investigate each material
gap and submit the revised complete packet for another independent review. Do not
force extra rounds after `PASS`, and do not translate an incomplete critic result
into approval.

If a child pauses with `ask_user`, the runtime resumes that same task after the
scientist answers. Keep and re-wait its original task id. If a source lane fails
terminally, continue independent lanes where useful and carry the exact evidence
gap and impact into the critic packet and final result.
