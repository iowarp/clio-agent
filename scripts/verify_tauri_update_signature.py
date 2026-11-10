"""Verify a Tauri updater ``.sig`` against the brand's updater public key.

Every installed CLIO Desktop checks an update's minisign signature with the
``plugins.updater.pubkey`` compiled into it. A release that notarizes and
staples after Tauri signs must prove the published ``.sig`` still matches the
final payload bytes, or every client would refuse the update.

    python scripts/verify_tauri_update_signature.py \
        --config branding/clio/tauri.clio.conf.json payload.app.tar.gz payload.app.tar.gz.sig

Requires ``cryptography`` (``uv run --with cryptography``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _minisign_lines(encoded: str) -> list[str]:
    """Tauri stores minisign key and signature files base64-encoded once more."""
    return base64.b64decode(encoded.strip()).decode("utf-8").splitlines()


def public_key(encoded: str) -> tuple[bytes, Ed25519PublicKey]:
    """Return a minisign public key's id and Ed25519 key."""
    raw = base64.b64decode(_minisign_lines(encoded)[1])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ValueError("not a minisign Ed25519 public key")
    return raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:])


def verify(payload: bytes, signature: str, key: str) -> str:
    """Verify payload and trusted comment; return the trusted comment."""
    key_id, verifier = public_key(key)
    lines = _minisign_lines(signature)
    if len(lines) < 4 or not lines[2].startswith("trusted comment: "):
        raise ValueError("malformed minisign signature")
    raw = base64.b64decode(lines[1])
    if len(raw) != 74:
        raise ValueError("malformed minisign signature")
    algorithm, signed_by, value = raw[:2], raw[2:10], raw[10:]
    if signed_by != key_id:
        raise ValueError(
            f"signed by key {signed_by[::-1].hex().upper()}, expected {key_id[::-1].hex().upper()}"
        )
    if algorithm == b"ED":
        message = hashlib.blake2b(payload, digest_size=64).digest()
    elif algorithm == b"Ed":
        message = payload
    else:
        raise ValueError(f"unsupported minisign algorithm {algorithm!r}")
    trusted = lines[2].removeprefix("trusted comment: ")
    try:
        verifier.verify(value, message)
        verifier.verify(base64.b64decode(lines[3]), value + trusted.encode("utf-8"))
    except InvalidSignature as error:
        raise ValueError("signature does not match these bytes") from error
    return trusted


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True, type=Path, help="Tauri config with the pubkey")
    parser.add_argument("payload", type=Path)
    parser.add_argument("signature", type=Path)
    args = parser.parse_args(argv)
    key = json.loads(args.config.read_text(encoding="utf-8"))["plugins"]["updater"]["pubkey"]
    try:
        trusted = verify(args.payload.read_bytes(), args.signature.read_text("utf-8"), key)
    except ValueError as error:
        print(f"::error::{args.payload.name}: {error}", file=sys.stderr)
        return 1
    print(f"{args.payload.name}: updater signature valid ({trusted})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
