"""
The API keys typed into /admin, encrypted on disk.

One JSON file (DATA_DIR/secrets.json) holds each key as a Fernet token (``cryptography``),
by the name of the environment variable it fills. The master key that opens
them is kept apart from it: in SECRETS_MASTER_KEY, which a container or a
headless server needs, or else in the operating system's credential store
through ``keyring`` (macOS Keychain, Windows Credential Locker, Secret Service
on Linux), where one is made the first time it is needed.

So a copied data folder or a backup gives no key away. Code running as this
service still can: the service needs the real keys to call others.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Optional

import keyring
from cryptography.fernet import Fernet, InvalidToken

from . import envfile
from .errors import Unavailable

logger = logging.getLogger(__name__)

# Where the master key is filed in the credential store.
SERVICE = "ai-toolkit"
ENTRY = "secrets-master-key"


class KeyStore:
    """The keys in ``path``, opened with ``master`` or, without one, the credential store's master key."""

    def __init__(self, path: Path, master: Optional[str] = None):
        self.path = Path(path)
        self._master = master
        self._lock = threading.Lock()

    def load(self) -> Dict[str, str]:
        """Every key it can open, by variable. What it cannot open is logged, so the service still starts."""
        try:
            tokens = self._read()
            box = self._box() if tokens else None
        except Unavailable as e:
            logger.error("The saved API keys cannot be opened: %s", e)
            return {}
        opened = {}
        for name, token in tokens.items():
            try:
                opened[name] = box.decrypt(token.encode()).decode()
            except InvalidToken:
                logger.error("%s does not open with this master key: save it again", name)
        return opened

    def put(self, name: str, value: str) -> None:
        """Keep ``value`` for ``name``; an empty one removes it. Unavailable without a master key."""
        with self._lock:
            tokens = self._read()
            if value:
                tokens[name] = self._box().encrypt(value.encode()).decode()
            elif tokens.pop(name, None) is None:
                return
            envfile.write_private(self.path, json.dumps(tokens, indent=2))

    def _read(self) -> Dict[str, str]:
        """The file's tokens. Unavailable when it cannot be read, so that a save never replaces keys it did not see."""
        if not self.path.is_file():
            return {}
        try:
            tokens = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise Unavailable(f"{self.path} cannot be read: {e}") from None
        if not isinstance(tokens, dict) or not all(isinstance(token, str) for token in tokens.values()):
            raise Unavailable(f"{self.path} cannot be read: it is not a JSON object of tokens")
        return tokens

    def _box(self) -> Fernet:
        try:
            return Fernet(self._master or _stored_master())
        except ValueError:
            raise Unavailable("SECRETS_MASTER_KEY is not a Fernet key (see env.example)") from None


def _stored_master() -> str:
    """The credential store's master key, made there if it has none. Each backend raises its own errors, and the null
    backend drops what it is given, so a new key counts only once it reads back."""
    try:
        master = keyring.get_password(SERVICE, ENTRY)
        if master is None:
            keyring.set_password(SERVICE, ENTRY, Fernet.generate_key().decode())
            master = keyring.get_password(SERVICE, ENTRY)
    except Exception:
        master = None
    if master is None:
        raise Unavailable("No credential store here to keep the master key in: set SECRETS_MASTER_KEY (see env.example)")
    return master
