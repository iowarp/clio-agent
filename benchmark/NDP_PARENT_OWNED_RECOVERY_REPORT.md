# NDP Parent-Owned Recovery Semantics

## Why this exists

The earlier seismic benchmark reached SAC statistics and a PNG artifact by letting
the `ndp_catalog` child expert recover from failed NDP staging with a hardcoded
EarthScope waveform fetch. That made the artifact real, but the hierarchy was
wrong: the NDP child silently crossed from catalog/staging into unrelated
acquisition and format recovery.

## Corrected behavior

The `ndp_catalog` expert now owns only NDP discovery, metadata inspection,
resource ranking, and bounded NDP staging.

When staging fails, it returns:

- the dataset identifier and resource index attempted;
- the structured tool error from `ndp_stage_resource`;
- a compact human-readable staging note;
- `metadata.staging.status = blocked`;
- recommended parent actions such as broadening catalog search, trying another
  provider, delegating to a utility download expert, asking the user, or stopping
  without downstream analysis.

It does not call `sac_fetch_earthscope_waveform`, `sac_inspect_archive`, shell
download tools, or unrelated dataset recovery paths.

## What this fixes

- Failed NDP staging is no longer hidden behind a child-local fallback.
- Parent experts and the orchestrator receive explicit evidence to decide the
  next step.
- Tests now fail if the NDP child fetches EarthScope SAC data after an NDP
  staging blocker.
- The built-in and marketplace NDP expert prompts both state that recovery is
  parent-owned.

## Remaining benchmark work

The full end-to-end seismic PNG workflow still needs a generic parent/orchestrator
recovery implementation. That recovery should consume the blocked NDP child
result, choose an explicit next delegation, and only then proceed to SAC analysis
and visualization if it obtains a real local waveform file.

