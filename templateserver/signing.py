import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .store import json_bytes


def load_key(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "signing.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as file:
            file.write(Ed25519PrivateKey.generate().private_bytes_raw())
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def envelope(payload: dict, key: Ed25519PrivateKey) -> dict:
    raw = json_bytes(payload)
    # Payload bytes are signed directly; clients do not need to reproduce Python JSON formatting.
    return {
        "payload_base64": base64.b64encode(raw).decode(),
        "signature": base64.b64encode(key.sign(raw)).decode(),
    }


def verify(document: dict, pinned_public_key: bytes) -> dict:
    import json

    raw = base64.b64decode(document["payload_base64"], validate=True)
    signature = base64.b64decode(document["signature"], validate=True)
    Ed25519PublicKey.from_public_bytes(pinned_public_key).verify(signature, raw)
    return json.loads(raw)
