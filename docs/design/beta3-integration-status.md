# Beta 3 implementation and qualification

This is an implementation checkpoint, **not release approval**. Publication is
separate from the October 5 Delta qualification with the owner.

## Branches and preserved work

- Agent: `codex/beta3-integration`, based on `develop` plus the existing smooth
  vector branding commit `c34ea657`. Merge commit `bcecf675` preserves PR #1490
  and ports attention declaration into the current typed DSPy request boundary.
- UI: `external/gact-tui`, branch `codex/beta3-integration`; `9976580f` reconciles
  develop, `9c8c00bb` preserves PR #502 and resolves transcript integration.
- Schemas: `codex/beta3-integration`, shared contracts commit
  `7be60ee655287859124c4b3ea5e130ef976dc26d`, based on existing `main` (there is no
  remote `develop`; final PR base is awaiting clarification). Agent and UI pin
  that exact revision. Version `0.6.0b1` is not published to PyPI.
- Marketplace: `external/clio-agent-marketplace`, branch
  `codex/beta3-integration`, based on `main`; no beta-3 changes yet.

## Completed checks at this checkpoint

- Agent attention, typed LM trace, and route guard tests: **66 passed**.
  Both synchronous/asynchronous typed requests and keyword/positional requests
  preserve caller configuration, declaration context, and response identifiers.
- Schemas: **762 passed**; canonical JSON export verification and focused
  Pyright passed. Generated TypeScript fixtures include the new contracts.
- UI attention: **59 passed**; merged attention typecheck passed.
- Storage, infrastructure, model plans and route guard: **48 passed** including
  inventory, path inheritance, and freezing legacy deployment paths. Focused
  Pyright and Ruff checks passed.
- Infrastructure UI, route behavior, state isolation, transcript state:
  **48 passed**, plus the new storage form regression passed (in a 17-test run
  with the route suite). Typecheck, focused lint, and production build passed.
  Vite reports CSS Custom Highlight optimizer warnings and existing large bundles;
  the build succeeds, but attention needs its separate rendered qualification.
- Real isolated-backend browser review: storage form, overrides, target-side
  folder browsing, saving locations, inherited defaults, phone navigation,
  click-open info tooltip, and desktop light/dark palettes.
  Evidence is under `docs/qualification/beta3/`.
- Read-only production storage probe through OpenSSH to homelab:
  `/` free 5,316,571,136 bytes, not writable, fails a 20 GiB requirement;
  `/data` free 2,820,260,794,368 bytes, writable, passes. Reproduce with:
  `uv run python scripts/qualification/beta3_host_storage.py homelab / /data --required-gib 20`.
  This is filesystem inspection, not a completed service deployment test.

## Work in progress

Host-bound persistent storage settings and browsing now have HTTP contracts and
UI controls. Models store their resolved paths in deployment configuration;
older model receipts are frozen before defaults change. The six Infrastructure
sections and retained management state are being connected to full workflows.

## Required work still outstanding

1. Complete connected storage adapters, trusted OAuth setup, source picker,
   transfers, immutable working-copy baselines and conflict review. CLIO-owned
   Google/Globus client registrations and redirect URLs have been requested;
   never include secrets in this document or the transcript.
2. Complete guided model download, native vLLM, versioned service definitions,
   independent Flowcept/CMF deployment and write/readback verification, pinned
   attention probe activation, receipts, ownership-safe lifecycle controls.
3. Implement marketplace audit #1627, including scoped identities, safe reload,
   drafts/publish, pin preservation, events, and deletion distinctions.
4. Finish full-transcript stable attention selections, shared editable numerical
   profiles, bidirectional lookup, SPOTTER tools and findings. Replace the old
   ordinal tool-block bridge with stable call identities. No fabricated image
   patch attribution; no SSH fetching inside the attention reader.
5. Complete browser review of every changed control, empty/error states,
   keyboard, light/dark, narrow layouts and long transcripts. Complete packaged
   install/update-channel checks and exact dependency qualification.
6. Real homelab deployment/download/provenance/reconnect/cleanup on an owned
   `/data` directory; external bounded Ares allocation and SSH-hop checks.
   Neither has been completed at this checkpoint.
7. Prepare ordered Delta setup, then together run fresh instrumented inference,
   token mapping, both OPAL/SPOTTER demos and evidence retention through shutdown.

Do not label unavailable, skipped, simulated or inspection-only checks as passed
live deployment or inference acceptance. Do not publish beta 3 from this checkpoint.
