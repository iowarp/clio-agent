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
      // The payload is the ~730 MB gact-runtime.tar.zst, already compressed. Tauri's rpm
      // bundler recompresses it with gzip on one thread by default, which turned the
      // bundled leg's `tauri build` from minutes (deb only) into well over an hour.
      if (format === "rpm") out.linux.rpm.compression = { type: "none" };
    }
  }
  if (variant === "bundled") {
    out.resources = ["gact-runtime.tar.zst", "gact-runtime.pack.json", "gact-runtime.unpack.sh"];
  }
  return out;
}

module.exports = { linuxPackageBundle };
