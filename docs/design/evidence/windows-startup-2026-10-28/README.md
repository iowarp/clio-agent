# Windows acceptance evidence, 2026-10-28

The matrix and interpretation are in
[Result on Windows](../../install-and-startup-acceptance.md#result-on-windows-2026-10-28).
Cold source install, agent and desktop exceed their budgets. Fresh NSIS installation
also failed its Office check. These files do not establish release readiness.

- `install-*-aligned.json` and timestamped logs are the successful source installs
  with matching HOME/USERPROFILE/CODEX_HOME. The `diagnostic-install-*` files are
  earlier failed profile-isolation attempts; retain their timings separately.
  The installer's reported TUI elapsed time runs until collection of the background
  process, rather than recording its actual completion time. It is not a separate
  compiler-duration measurement and must not be added to the total.
- `mxc-original-profile.json` and `mxc-aligned-profile.json` retain the exact real
  probe comparison, including all seven witnesses in the successful run.
- `reference-store-summary.json` identifies the locally generated store, workload
  and absolute environment paths. `diagnostic-reference-store-isolated.log` is the
  successful generator's full log, including transient socket/quarantine/recovery
  failures. `diagnostic-reference-store.log` is the earlier failed generation.
  Never copy the generated store to reproduce these runs.
- `agent-*.json` are the original functional measurement script's results.
  `agent-*-processes.json` separately track immutable owned process identities,
  including the cold run's surviving SearXNG environment installer that required
  forced cleanup. A graceful server exit is not a complete-descendant guarantee.
- `desktop-package-managed-cold*` retain the fresh NSIS install failure and its
  full Office timeout. `desktop-package-managed-repeat*` retain successful repeat
  setup. `desktop-build.json` identifies the corrected unsigned managed CLIO build.
- `desktop-cold.json` and `desktop-repeat.json` retain the original launch timers,
  readiness/908-conversation validation, rail population, memory and quit evidence.
  PNGs capture actual WebDriver states. Composer screenshots precede the 10 s
  settle, when the rail filled. No model provider was configured or prompt sent.
- The first desktop run initially selected an old, stale boot-log path. Its result
  says `boot_log_current: false`; that old transcript was excluded. The app's real
  Tauri AppLog directory was queried live, recorded in
  `desktop-cold-resolved-log.json`, and its actual log retained separately as
  `desktop-cold-native-boot.log`. Repeat used automatic live AppLog discovery.
  The directory differs from both the caller's .NET known folder and the isolated
  LOCALAPPDATA value. No launch timing was rewritten to correct log collection.
- `desktop-*-cleanup.json` confirm no measured desktop or driver survivors and no
  forced application termination. Driver utilities are retired after measured quit.
- `desktop-neutral-diagnostic*` are excluded from acceptance: an earlier local
  build had neutral native connect-mode configuration. Its window received the
  real Quit command; its verified Node harness was then forcibly stopped and its
  driver utilities retired. The managed build regenerated the CLIO native backend
  descriptor and ran the full branded Tauri build before packaging.
- Machine/storage and Defender snapshots record context. Process CPU samples do
  not assign an exclusive wall-clock duration to Defender scanning.

The generated reference store and benchmark profiles remain at their original D:
paths while qualification is still open. Raw process records contain temporary
synthetic probe commands; desktop bearer-token arguments are redacted.
Text evidence uses Git line endings; trailing spaces on blank installer-output
lines were removed. Captured timestamps and timing values are unchanged.
