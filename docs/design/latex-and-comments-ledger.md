# LaTeX builds ledger

Rows follow [latex-and-document-comments-plan.md](sdk/latex-and-document-comments-plan.md) as
amended by [latex-and-document-comments-addendum-a.md](sdk/latex-and-document-comments-addendum-a.md).
Where the addendum and the plan disagree, the addendum wins. Status values are the ones
[acceptance-ledger.md](sdk/acceptance-ledger.md) defines (`passed (live)`, `tests only`, `static`,
`done`, `open`, `blocked`), plus `local phase` for a row the plan assigns to the local pass.

Decisions use the prefix `L`, scenarios keep the plan's ids.

**What moved.** Workstreams 1 and 5, the comments bullet of workstream 6, and scenarios L8–L14,
L16–L18 belong to the comments plan. They are qualified there as K1–K24
([comments-ledger.md](comments-ledger.md)). This ledger covers workstreams 2, 3, 4 and 6, the
`source-map` resolver, and scenarios L1–L7, L15, L19 and L20.

**Branch.** `claude/busy-turing-r40z0q` in clio-agent, gact-tui and clio-agent-marketplace. The
person chose to keep working on the comments branch, so the resolver plugs straight into its
registry (`gact/documents/anchors.py`). The plan's own branch name, `claude/latex-document-comments`,
is not used.

**Base.** The head of `claude/tool-calling-long-horizon`, merged into this branch at `0b5c1a0b`.
From here on the branch takes its base by rebasing.

## Phase 0

| ID | Decision | Evidence | Status |
| --- | --- | --- | --- |
| L1 | Tectonic 0.17.0, the latest stable release (Homebrew's formula API reports `stable: 0.17.0`; `tectonic@0.18.0` has no release). Assets and SHA-256, hashed from the downloaded release assets, since the GitHub API is not reachable from this session to cross-check a published digest: `x86_64-unknown-linux-musl.tar.gz` `8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7`; `aarch64-unknown-linux-musl.tar.gz` `b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8`; `x86_64-apple-darwin.tar.gz` `7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80`; `aarch64-apple-darwin.tar.gz` `a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667`; `x86_64-pc-windows-msvc.zip` `f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f`. The Linux archive holds the single executable `tectonic` (26,401,904 bytes). | Release downloads `https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/<asset>` returned 200; `sha256sum` | done |
| L2 | The pinned bundle | `relay.fullyjustified.net/default_bundle_v33.tar` is reachable. Which bundle 0.17.0 uses by default, and the versioned URL to pin, have to be read from the running binary | blocked: running the downloaded binary was not permitted in this session |
| L3 | `--synctex` output on Linux | needs the binary to run | blocked: as L2 |
| L4 | IEEE and ACM sample builds, packages fetched, cache size, `llncs` and `elsarticle` | needs the binary to run | blocked: as L2 |
| L5 | biber | needs the binary and a biber build to run | blocked: as L2 |
| L6 | Network for builds. The sandbox's `NetPolicy` is binary, `allow_record` or `deny` (`runtime/sandbox.py:73-75`). Per-host decisions exist only at the egress proxy (`runtime/net_chokepoint.py` `set_egress_gate`, `_gate_allows`), wired to workspace `host_pattern` policies. So a build child runs `NET_ALLOW_RECORD` behind a build-scoped egress gate that admits only the pinned bundle host. When the gate refuses a fetch, Tectonic's log names the missing file and the build reports `missing_package`. | Code reading; the refused and admitted proofs need the binary | open |
| L7 | SyncTeX parser: a small parser in clio-agent. The `.synctex.gz` format is gzip-compressed line-oriented text (a preamble with `Input:<tag>:<path>` lines, then per-page `{`…`}` blocks of box records `[`, `(`, `h`, `v`, `x`, `k`, `g`, `$` with tag, line, x, y, width, height and depth in scaled points). Reading page boxes, the input tags and their line ranges needs no dependency. The maintained Python options are unmaintained or tied to a TeX distribution's `synctex` CLI, which a Tectonic install does not ship. | Format as documented in the SyncTeX parser sources; the round trip on a real build needs the binary | open |
