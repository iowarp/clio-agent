"""The Tauri updater signature check accepts only the brand key's exact bytes."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts.verify_tauri_update_signature import main, public_key, verify


def _wrap(lines: list[str]) -> str:
    return base64.b64encode(("\n".join(lines) + "\n").encode()).decode()


def _keypair(key_id: bytes = b"\x01" * 8) -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    encoded = base64.b64encode(b"Ed" + key_id + public).decode()
    return private, _wrap(["untrusted comment: minisign public key", encoded])


def _sign(
    private: Ed25519PrivateKey, payload: bytes, key_id: bytes = b"\x01" * 8, prehash: bool = True
) -> str:
    message = hashlib.blake2b(payload, digest_size=64).digest() if prehash else payload
    value = private.sign(message)
    trusted = "timestamp:1700000000\tfile:CLIO Desktop.app.tar.gz"
    global_signature = private.sign(value + trusted.encode())
    return _wrap(
        [
            "untrusted comment: signature from tauri secret key",
            base64.b64encode((b"ED" if prehash else b"Ed") + key_id + value).decode(),
            f"trusted comment: {trusted}",
            base64.b64encode(global_signature).decode(),
        ]
    )


@pytest.mark.parametrize("prehash", [True, False])
def test_accepts_signature_over_the_exact_payload(prehash: bool) -> None:
    private, public = _keypair()
    trusted = verify(b"stapled app", _sign(private, b"stapled app", prehash=prehash), public)
    assert trusted.startswith("timestamp:")


def test_rejects_bytes_changed_after_signing() -> None:
    private, public = _keypair()
    with pytest.raises(ValueError, match="does not match"):
        verify(b"stapled app", _sign(private, b"app before stapling"), public)


def test_rejects_another_key() -> None:
    _, public = _keypair()
    other, _ = _keypair()
    with pytest.raises(ValueError, match="does not match"):
        verify(b"payload", _sign(other, b"payload"), public)
    with pytest.raises(ValueError, match="signed by key"):
        verify(b"payload", _sign(other, b"payload", key_id=b"\x02" * 8), public)


def test_rejects_edited_trusted_comment() -> None:
    private, public = _keypair()
    lines = base64.b64decode(_sign(private, b"payload")).decode().splitlines()
    lines[2] = "trusted comment: timestamp:1\tfile:other"
    with pytest.raises(ValueError, match="does not match"):
        verify(b"payload", _wrap(lines), public)


def test_cli_reads_the_brand_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private, public = _keypair()
    config = tmp_path / "tauri.conf.json"
    config.write_text(json.dumps({"plugins": {"updater": {"pubkey": public}}}), encoding="utf-8")
    payload = tmp_path / "CLIO.app.tar.gz"
    payload.write_bytes(b"final bytes")
    signature = tmp_path / "CLIO.app.tar.gz.sig"
    signature.write_text(_sign(private, b"final bytes"), encoding="utf-8")
    assert main(["--config", str(config), str(payload), str(signature)]) == 0
    payload.write_bytes(b"tampered")
    assert main(["--config", str(config), str(payload), str(signature)]) == 1
    assert "does not match" in capsys.readouterr().err


def test_brand_pubkey_is_a_minisign_ed25519_key() -> None:
    brand = Path(__file__).resolve().parents[2] / "branding/clio/tauri.clio.conf.json"
    key = json.loads(brand.read_text(encoding="utf-8"))["plugins"]["updater"]["pubkey"]
    key_id, _ = public_key(key)
    assert len(key_id) == 8
