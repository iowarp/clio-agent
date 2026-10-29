# Prebuilt-only native installs (2026-10-26)

CLIO no longer compiles Valkey, PostgreSQL, m4/bison/flex or llama.cpp on the host. Native
installs fetch pinned upstream binaries; a host they cannot run on is refused and the container
variant is the path there. Matrix and requirements: [Managed infrastructure](https://clio.iowarp.ai/docs/managed-infrastructure/).

## CLIO's Flowcept install hook, run in official distribution images

`native_monitoring_hook.install()` (this branch) with `database_mode: managed`, the download cache
seeded with the pinned archives (verified by SHA-256 name), no compiler on the system:

```
=== opensuse/leap:15.6            (glibc 2.38; libopenssl1_1 installed)
INSTALLED in 6.4s; binaries: ['mongod', 'valkey-server']
valkey PING -> +PONG
mongod: db version v8.0.14
compilers on host: None None None

=== opensuse/leap:15.5            (glibc 2.31; libopenssl1_1, libsystemd0, libopenssl3 installed)
REFUSED after 1.2s: Valkey's prebuilt Linux build does not run on this host:
  .../valkey-server: /lib64/libc.so.6: version `GLIBC_2.33' not found ...
  CLIO does not compile services; use the container variant or site databases on this host.
```

Before this change the same install compiled Valkey with `make` (and PostgreSQL with
`configure && make`, after building m4, bison and flex) and failed outright without `cc`/`make`.

## Each pinned binary's `--version`, after installing the listed system libraries

| Image | glibc | Valkey 8.1.3 (jammy) | PostgreSQL 17.6 | llama.cpp b11206 CPU |
| --- | --- | --- | --- | --- |
| ubuntu:24.04 | 2.39 | runs | runs | runs |
| ubuntu:22.04 | 2.35 | runs | runs | runs |
| opensuse/leap:15.6 | 2.38 | runs | runs | runs |
| rockylinux:9 | 2.34 | runs | runs | `GLIBCXX_3.4.30` not found (GCC 11 libstdc++) |
| opensuse/leap:15.5 | 2.31 | `GLIBC_2.33` not found | `GLIBC_2.34` not found | `GLIBC_2.34` not found |
| rockylinux:8 | 2.28 | no `libssl.so.3` (and glibc too old) | same | same |

Not exercised here: the llama.cpp Vulkan and CUDA builds on a GPU host, and HPE Cray OS itself.
