# Aurora and transcript integration into develop, 12 October 2026

This integration brings the Aurora fixes, transcript and Work refinements,
end-user guides, and APPL-CORE onboarding guidance into `develop`. The separate
native task-system rewrite remains excluded. No merge to `main`, release,
website deployment, or Aurora infrastructure rerun is part of this work.

## Integration tracking

| Repository | Review and merge target | Included work |
| --- | --- | --- |
| clio-agent | [PR 1724](https://github.com/iowarp/clio-agent/pull/1724), `develop` | Aurora core changes, wait presentation, user guides, pinned UI and marketplace, ordinary OPAL prompts |
| gact-tui | [PR 599](https://github.com/iowarp/gact-tui/pull/599), `develop` | Transcript continuity, persisted disclosures, current todos, task links, connection switching, Aurora attention and SPOTTER UI |
| clio-agent-marketplace | [PR 97](https://github.com/iowarp/clio-agent-marketplace/pull/97), merged to `develop` | Finish onboarding and deterministic verification before reporting readiness |

Aurora ancestry included in the integration:

- Core `qual/aurora-fixes`: `df69876dc9d1ed56d80ddfbba607c74729544b1f`.
- UI `qual/aurora-fixes`: `27d856f0cb5dcf1577538bdc6112857f1ae991cd`.

Excluded reviews: core [PR 1704](https://github.com/iowarp/clio-agent/pull/1704),
UI [PR 590](https://github.com/iowarp/gact-tui/pull/590), and marketplace
[PR 95](https://github.com/iowarp/clio-agent-marketplace/pull/95).
The older task infrastructure already present in develop is retained.

## What changes for users

| Area | Behavior and purpose | Implementation owners |
| --- | --- | --- |
| Intel GPU execution | Detect Intel Data Center GPUs and offer native XPU vLLM variants on compatible Linux hosts. Use the facility's vLLM/PyTorch environment via host setup. The attention variant requires host-accessible Flowcept settings. | `infrastructure/probe.py`, `native_vllm.py`, `model_runtimes.py` |
| Model parsing | Derive Granite 4.2 parser defaults from its actual chat template. | `infrastructure/server_parameter_defaults.py` |
| Attention | Improve reasoning/content attribution, including a prompt that already opens thinking and separators stripped from the rendered transcript. Expose direct selection actions for later use and combined attention sets. | `gact/attention/selection.py`, `service.py`; UI `attention-lookup-panel.tsx` |
| SPOTTER | Arming a reviewer after activity has occurred requests review of that existing conversation. After a restart, initialize the host agent from the watched session before waking the reviewer. Show a compact review entry. | `spotter_arm_review.py`, `spotter_watcher.py`; UI `subagent-card.tsx` |
| Transcript | Keep reminders and results inside activity, preserve operation continuity across question exchanges, and omit completion footers for empty boundaries and ongoing updates. Keep adjacent text paragraphs 8 pixels apart, separate text from activity groups by 20 pixels, and expanded reasoning from tools by 12 pixels. | UI conversation grouping, injection and footer components |
| Work | Distinguish todos from asynchronous tasks, emphasize current todos, remember disclosure choices, and link delegated work to its details. Wait receipts identify any/all, selected tasks and pending outcomes. | UI Work/evidence components; `task_wait_presentation.py` |
| APPL-CORE | The initial merge required a generated loader and views. The follow-up below removes that restriction: investigate the actual read-only source, adapt the analysis, and substantiate findings. An ended child turn remains insufficient evidence of a result. | Marketplace onboarding and audit skills |

End-user coverage of the transcript and Work changes is in the website guides for
[following work](../../site/src/content/docs/docs/following-work.mdx),
[questions](../../site/src/content/docs/docs/questions.mdx),
[injections](../../site/src/content/docs/docs/injections.mdx), and
[workspace panels](../../site/src/content/docs/docs/workspace-panels.mdx).

Those guides include retained interface captures and examples. Aurora's changes
are described in this integration record. The unillustrated additions to the
model, evidence and work guides were withdrawn after the user clarified that
the documentation request meant a direct explanation of the integration.
Fresh Aurora attention and SPOTTER captures remain evidence for the owning
environment to produce.

## OPAL 1 requests and qualification boundary

The canonical input file, `docs/qualification/beta3/opal-live-inputs.json`, now
contains ordinary user requests:

1. Explore this data.
2. Does the nickel treatment hurt growth in this experiment?
3. Show me the growth curves that support your answer. Can I select a plant to inspect its measurements?

Choose APPL-CORE in the product rather than instructing the model how to run its
implementation. A fresh qualification must start without the earlier experiment
card, loader, audits or generated views. A new session alone is insufficient:
dataset artifacts are stored under the workspace's Agent state. Keep the raw
export and existing evidence intact, and use an isolated workspace state for
fresh onboarding.

The source must remain read-only even when an operator has a writable copy.
Do not repair its data or metadata to make a demo pass. Example schemas,
`loader.py`, and the card helper are optional starting points, not a required
analysis pipeline. A different export version, missing manifest entry, unavailable
helper, or irregular table requires investigation and adaptation where data is
accessible. It does not justify inventing meaning or refusing the entire export.
Derived outputs belong outside the source. Scientific conclusions still need
traceable observations, defensible calculations, and explicit uncertainty.

This corrects the onboarding requirement introduced by marketplace PR 97.
[Marketplace PR 98](https://github.com/iowarp/clio-agent-marketplace/pull/98)
updates the shared data-semantics guidance, APPL-CORE skills, and behavioral
evaluation together. The evaluator accepts alternative tool/code paths and
requires independent read-only and unchanged-source evidence. It no longer
rewards refusing an export solely because its version differs from an example.
The marketplace follow-up merged to `develop` at
`295308495af6e98bd43eb737e51db6f8855798ea` after all four CI jobs passed. The
clio-agent follow-up pins that revision. This is not evidence of a fresh OPAL run.

Submit one request, inspect the actual response, running work and questions,
then decide whether the next request is appropriate. Do not advance the chain
with an unattended runner.

**No fresh OPAL model turn was submitted in this integration pass.** A local
service was prepared with isolated configuration, data and state and an OPAL
workspace. It was stopped at the user's request. The local machine does not
have the Aurora vLLM/GPU infrastructure. Neither a local build nor a different
provider would qualify that stack. No Codex run substitutes for the requested
vLLM qualification, and no Aurora service was changed or rerun.

## Validation evidence

Local UI lint, ownership/presentation guards, type checking, web/offline builds
and a Windows native build were performed. Local pytest, Vitest and Playwright
suites remained stopped under the user's earlier instruction. CI results are
attached to the review links above; they are distinct from rendered review and
live scientific acceptance.

Initial UI CI exposed obsolete expectations for injection placement, completed
todo visibility, result summaries and empty completion boundaries. Coverage was
updated to open the intended disclosures while preserving checks for exact
content, causal order, navigation and duplicate avoidance. Real gaps found in
that review were repaired: accessible disclosure names, automatic/requested
summary provenance, and completion footers on ongoing updates.

Rendered spacing review used the real transcript components with a labeled
simulated fixture, in light and dark desktop views and narrow phone layouts.
Captures and their manifest are retained under
`D:/Libraries/Videos/clio_recordings/2026-10-12-transcript-spacing/`.
That review also reproduced an evidence-placement defect: the toolbar could
measure before the transcript ref was attached and never subscribe to layout
changes. Measuring after mount fixes initial docking; manual review confirmed
that a collapsed Work panel returns with its disclosure state after closing a
side tab. These are browser interaction checks, not scientific model evidence.
The browser click trace also exposed a floating scroll control appearing over a
disclosure press in a transcript that already fit the viewport. The control now
requires actual overflow. Manual pointer-down/up review confirmed the tool
result opens and the unnecessary scroll control stays absent.

The integration does not establish fresh GPU inference, live attention capture,
SPOTTER findings, successful OPAL onboarding, or a treatment effect.

## Marketplace locking on shared filesystems

See [the scoped lock audit](marketplace-locking-shared-filesystems-2026-10-12.md).
The local startup warning did not identify a holder, and the installation later
completed. Calling it evidence of another process was too specific: competing
threads can also encounter the nonblocking lock. This observation provides no
NFS or cross-node acceptance evidence.
