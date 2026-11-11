// Tauri config for the Linux deb and rpm: one runtime archive instead of the exploded
// runtime, and the maintainer scripts that unpack and remove it. Shared by
// clio-bundles.yml and desktop-bundle-smoke.yml, so qualification builds ship the
// release layout. Arrays are appended, not replaced: Tauri adds its own WebKit depends.
"use strict";

const path = require("node:path");

const SCRIPTS = path.join(__dirname);

function linuxPackageBundle(bundle, variant) {
  const out = { ...bundle, linux: { ...(bundle.linux ?? {}) } };
  for (const format of ["deb", "rpm"]) {
    const current = out.linux[format] ?? {};
    out.linux[format] = {
      ...current,
      // Lite packages carry the scripts too: they remove a runtime a bundled package
      // unpacked when the lite app replaces it.
      postInstallScript: path.join(SCRIPTS, "postinst"),
      preRemoveScript: path.join(SCRIPTS, "prerm"),
      postRemoveScript: path.join(SCRIPTS, "postrm"),
    };
    if (variant === "bundled") {
      const depends = current.depends ?? [];
      out.linux[format].depends = depends.includes("zstd") ? depends : [...depends, "zstd"];
    }
  }
  if (variant === "bundled") {
    out.resources = ["gact-runtime.tar.zst", "gact-runtime.pack.json", "gact-runtime.unpack.sh"];
    // The archive is already zstd-19, so a compressed rpm payload saves nothing. It
    // also never finishes: Tauri's rpm crate (0.15) re-hashes the whole remaining
    // buffer on every partial compressor write, quadratic in the 727 MB file (gzip
    // takes 32 KiB per write; the v0.9.6-beta.1 legs hit the 120 min job timeout).
    // An uncompressed payload is consumed in one write.
    out.linux.rpm.compression = { type: "none" };
  }
  return out;
}

module.exports = { linuxPackageBundle };
