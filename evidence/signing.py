"""
The Ed25519 key that signs every capture record.

A PKCS#8 PEM file (mode 0600), made on first use, so it can be inspected and
used with ``openssl pkeyutl``; the public key is written beside it for
verifiers. For real casework, move the key into a keychain, TPM or hardware
token: only ``Signer.sign`` would change.
"""

import hashlib
import os
import threading
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def public_key_file(key_file: Path) -> Path:
    name = key_file.name[:-4] if key_file.name.endswith(".pem") else key_file.name
    return key_file.with_name(name + ".pub.pem")


def fingerprint(key: Ed25519PublicKey) -> str:
    """SHA-256 over the raw 32-byte public key, hex; each record names the key it was signed with."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def verify(key: Ed25519PublicKey, message: bytes, signature_hex: str) -> bool:
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    if len(signature) != 64:
        return False
    try:
        key.verify(signature, message)
        return True
    except InvalidSignature:
        return False


def load_public_key(pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return key


class Signer:
    """The signing key, read from (or first written to) ``key_file``."""

    def __init__(self, key_file: Path):
        self.key_file = Path(key_file)
        self._key = None
        self._lock = threading.Lock()

    def _load(self) -> Ed25519PrivateKey:
        if self._key is None:
            with self._lock:
                if self._key is None:
                    self._key = self._load_or_create()
        return self._key

    def _load_or_create(self) -> Ed25519PrivateKey:
        path = self.key_file
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{path} is not an Ed25519 private key")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        # Always (re)write the public key, so verifiers have it next to the log.
        public_key_file(path).write_text(self.public_pem_of(key))
        return key

    @staticmethod
    def public_pem_of(key: Ed25519PrivateKey) -> str:
        return key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def sign(self, message: bytes) -> bytes:
        """The raw 64-byte signature."""
        return self._load().sign(message)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._load().public_key()

    @property
    def public_pem(self) -> str:
        return self.public_pem_of(self._load())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)
