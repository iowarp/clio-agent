# HDF5 deep references: `@run.h5::/entry/data`

Status: implemented (Oct 30): clio-agent `gact/context_reference_hdf5.py` and gact-tui `composer-hdf5-references.ts`.

## Goal

The `@` mention points at an object inside an HDF5 file, not just the file, so
the user can say exactly which group or dataset they mean and the agent receives
that precision.

## User flow

1. Type `@run`: the picker shows `run.h5`, as today.
2. Press **Tab** on an HDF5 file instead of Enter. The picker switches to the
   file's contents: groups and datasets, each with kind and shape
   ("dataset · 16448×1030 int32"). Typing filters, and Tab enters a group.
   Backspace on an empty filter goes up a level.
3. Press **Enter** to insert a chip `run.h5 › /entry/data/data`. Clicking the chip
   opens the HDF5 viewer at that object (`Hdf5ViewerFrame` already takes
   `locator`).

## Syntax

`@run.h5::/entry/data`:
- `::` is the silx/NeXus file-plus-object URL convention.
- A single `:` clashes with Windows paths (`C:\...`).
- It leaves room for slices later: `@run.h5::/entry/data[0, :, 100:200]`. The
  h5grove `data` route already parses that `selection` syntax.

## Server (clio-agent)

- **Listing.** Reuse the h5grove route from #1910: `meta` gives one level of
  children with kind/shape/dtype, and `paths` gives a whole-file list for typed
  search. Both are contained by `gact/hdf5_view.py` (links and VDS never leave
  the workspace).
- **Reference.** No new kind: a `workspace_file` context reference with
  `navigation = {workspace_id, path, locator: "/entry/data"}`. clio-schemas'
  `ContextReferenceMessageBlock.navigation` is already a string map.
- **Admission.** Resolve the locator through `hdf5_view` (exists, contained,
  not an unfollowable link). A bad locator is a typed `context_ref_invalid`,
  never silently dropped.
- **Delivery to the agent.** HDF5 is never inlined (`workspace_path` mode). Add
  the locator and a small object summary (kind, shape, dtype, attributes such
  as `NX_class`, units) from the same `meta` call. The agent reads data with the
  hdf5 plugin tools (`get_by_path`, `read_partial_dataset`).

## UI (gact-tui)

- **Picker.** `composer-reference-controller.ts` accepts on Tab today. Add a
  drill-in state: Tab on a container (an HDF5 file or group) loads its children
  through the repository's `readWorkspaceHdf5`, and only when the server
  advertises `x_clio_hdf5_viewer`.
- **Chip and navigation.** The label shows `file › /path`. Navigation opens the
  file tab with the locator (`composer-reference-navigation.ts`).
- **Showcase.** Covers loading, empty group, refused link and missing locator.

## Prerequisite: don't hash huge files to pin a reference

Today, resolving an `@file` reference runs `sha256_file` over the whole file
(`gact/context_references.py:273`) to pin its revision, even though the picker
already supplied a stat revision (`context_reference_search.py:141`,
`stat:<mtime_ns>:<size>`). A 100 GB file is read in full on every mention.

Proposal:
- **Large files** (default 64 MiB; config
  `gact.context_references.hash_max_bytes`): pin by stat revision only. The
  revision check compares stat against stat, and the content hash is left out
  of the delivery record. The record says `revision_basis: "stat"` so the
  choice is visible, not a silent downgrade.
- **Small files:** keep sha256, but cache it by `(dev, inode, size, mtime_ns)`
  so re-mentioning an unchanged file does not re-read it.
- **Trade-off:** stat misses an in-place edit that keeps both size and mtime
  (rare: `touch -r`, some copy tools, coarse-mtime filesystems). That is the
  same signal file watchers and build tools rely on, and scientific HDF5 files
  that change are rewritten or appended, which moves both.
- **Rejected alternative:** a sampled hash (first and last MB plus size). It is
  cheaper than a full hash, but it still misses mid-file edits, and adds code
  over stat for little gain.

## Tests

- Server: locator admission (valid, missing, through an outside link →
  refused), summary in delivery, stat-only pinning above the threshold, and a
  stale stat → `context_ref_stale`.
- UI: Tab drill-in, filter, up a level, Enter inserts the chip, the chip opens
  the viewer at the locator, and no drill-in without the capability.
- Live: the five nexusformat beamline files, mentioning `/entry/data` in the
  APS file and the 67 MB Jungfrau pixel mask.
