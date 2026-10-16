# Native installation and storage recovery

## Older Linux document hosts (#1782)

LibreOffice 26.2.6's official x86-64 DEB and RPM binaries import GLIBC 2.34
and GLIBCXX 3.4.29 symbols. Changing the archive format does not remove those
requirements. CLIO retains the pinned current renderer and, on older x86-64
glibc hosts, prepares a private, checksum-pinned Ubuntu 22.04 C/C++ runtime.
The Office launcher invokes its ELF loader explicitly. It does not install
system packages, replace host libraries, require containers, or export a
different `LD_LIBRARY_PATH` into CLIO or its other tools.

Compatibility runtimes have a package-derived directory identity. Updates are
published beside older versions so an active renderer can keep using its own
libraries. Package copyright and license notices remain in that directory.
Modern Linux hosts retain their native loader; forcing an older libc onto their
newer graphics libraries would create another ABI mismatch. Other architectures
retain their existing pinned renderer packages.

The private launcher honors LibreOffice's normal profile-initialization restart
code 81, with a three-launch ceiling. It propagates crashes and other exit codes.
The renderer is probed before publication and before warm reuse. Network and
integrity failures permit at most three fresh download attempts under one
600-second budget; each attempt must match the existing pinned SHA-256.
Permanent HTTP failures, size limits and deadline exhaustion remain explicit
errors. Failed downloads are removed; diagnostics include the actual checksum,
byte count and resolved source URL. Linux still
needs LibreOffice's ordinary host graphics/font/NSS libraries. Qualification on
Rocky Linux 8 (glibc 2.28) includes cold preparation, warm reuse, and real DOCX
and PPTX conversions to PDF; the unmodified launcher fails on that same host.

## Full managed conversation storage (#1783)

CTE's file-tier capacity is a configured ceiling, independent of free filesystem
space. The old default seeded 10% of free disk (2–50 GiB) once and never extended
an allocated tier. A full tier could therefore reject conversation writes while
the host still had ample disk space.

CLIO's async write adapter now preserves the native return code. A completed
placement/allocation refusal (12 or 13) may trigger one capacity recovery check
within the existing bounded retry. Unknown errors retain the bounded retry;
pending or timed-out writes are not reissued by capacity recovery.

For CLIO's generated default disk-only configuration, recovery:

1. Serializes across clients with an interprocess lock and checks the configuration
   fingerprint. Explicit external configurations and `arc.cte.file_capacity` /
   `CLIO_ARC_CTE_FILE_CAPACITY` remain operator choices.
2. Measures the existing blob size and refreshes native target statistics. Only
   additional growth counts. Under `max_bw`, each candidate must individually
   have room; aggregate free space across undersized targets is insufficient.
3. Adds an independent file target when every configured target lacks room.
   Existing targets and records are never resized, removed or replayed. New
   targets retain at least 1 GiB or 10% free filesystem space, whichever is larger.
   Growth is bounded to eight targets, 50 GiB per addition, and 500 GiB overall.
4. Journals the exact target, storage index, stable native pool ID and resulting
   configuration before registering the target. A failed or interrupted addition
   resumes the same plan. An edited configuration stops recovery.
5. Retries the identical refused blob. A partial batch does not rewrite successful
   records. Restart reads the same target paths and IDs from the durable config.

New generated configs record ownership in `cte-capacity.json`. An older config
without that journal is eligible only when it matches the original generated
single-file seed shape, owned paths and 2–50 GiB seed range. A custom topology is
not automatically enrolled. Set `CLIO_ARC_CTE_AUTO_EXPAND=false` (or
`arc.cte.auto_expand=false`) to retain a fixed managed capacity. Editing a
journaled config also opts it out until an operator reconciles it.

This requires the Core capacity/target-query and stable async registration
bindings in the [Core 2.3.2 maintenance patch](https://github.com/iowarp/clio-core/pull/1292).
Published Core 2.3.1 does not contain those bindings. The Agent change must ship
with an API-capable Core wheel and updated dependency/bundle pins; a source PR
alone does not update an installed beta.

## Tool execution versus recording

A command can finish successfully while recording its completion fails. CLIO
retains the observed result in its live transcript and publishes the terminal
tool event even when durable trace recording raises. That event distinguishes a
recording failure from execution success, clears the active-call latch, and lets
the original recording error propagate. It does not rerun the command or claim
the result was saved durably. Provenance receives the original call start time.

For historical turns whose terminal result was never recorded, the transcript
shows the execution outcome as Unknown once the turn ends. Captured output is
retained; CLIO neither fabricates a completion time nor keeps incrementing a
running timer. The live API checks current turn ownership as well as recorded
stop reasons, so a restart cannot leave a partial message looking active just
because its final state was never saved. An actual recorded result takes
precedence over that uncertainty.

The screenshots in #1783 establish lost persistence and a stale Running row.
They do not establish the affected host's exact placement failure or repeated
command execution. Isolated native tests reproduce capacity exhaustion with code
12 despite free disk and prove automatic recovery plus byte-identical restart
readback. Other placement/health failures remain explicit errors.
