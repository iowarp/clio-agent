# Windows repair qualification

These receipts supplement the develop baseline in
`../windows-startup-2026-10-28/`. Candidate branch:
`codex/windows-startup-acceptance`, clio-agent PR #1889 and gact-tui PR #697,
both targeting develop. Product repairs were explicitly authorized after the
baseline failed. No release was tagged or published.

Final product heads: clio-agent `8af46e42136360a3384c0a07e16040757ae44eb4`,
gact-tui `f5b0ae3c0cbbc64f1a543484a5ed96b68d8dbda6`.

| | Cold | Repeat | Limit |
|---|---|---|---|
| Source install | 167.829 s | 25.380 s | 180 s |
| Installed agent | 26.338 s | 17.573 s | 30 s |
| Bundled desktop | 38.9 s | 27.0 s | 60 s |
| NSIS package setup | 176.204 s, exit 0 | 19.161 s, exit 0 | supplementary |

All six matrix timings pass. `repair6-*` selects the final source and installed
agent; `repair-desktop-*` selects the final GUI, screenshots, real backend/package
validation and quit/cleanup receipts. `desktop-package-repaired-*` records silent
setup. The package repeat was measured after both GUI launches. Both document
checks succeeded before Quit; no app/driver/install descendants survived. The
cold agent's separately recorded forced native-service cleanup is described in
the main document. Build JSON records the final NSIS/runtime archive hashes.

Each `repairN-install-cold.json` selects another empty profile and cache set;
the matching log records phase output. The first `repair-install-cold.json`
is iteration 1. OS and Defender caches were not dropped. The documents retain
unsuccessful candidates and do not select a passing result by hiding failures.

The direct-MSI and Office receipts are separate diagnostics. The pinned official
MSI was downloaded and checksum verified on this machine. Direct extraction
retained the application and all bundled fonts; office-qualification.pdf is the
real HTML-to-PDF output. These timings are not substitutes for the acceptance
matrix. The startup-graph diagnostic ran against an already installed environment.

The focused test logs cover the changed installer, native extraction validation,
protection handling, worker ownership, public infrastructure imports, and startup
bytecode validity. They do not claim develop CI or release-wide tests pass.
