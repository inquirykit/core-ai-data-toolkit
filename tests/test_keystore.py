import json
import os
import subprocess
import sys
from pathlib import Path

import keyring
import pytest
from cryptography.fernet import Fernet
from keyring.backend import KeyringBackend
from keyring.backends import fail, null

from core.errors import Unavailable
from core.keystore import ENTRY, SERVICE, KeyStore

MASTER = Fernet.generate_key().decode()


class Memory(KeyringBackend):
    """A credential store that lasts for one test, so the developer's own is never touched."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.kept = {}

    def get_password(self, service, username):
        return self.kept.get((service, username))

    def set_password(self, service, username, password):
        self.kept[(service, username)] = password

    def delete_password(self, service, username):
        del self.kept[(service, username)]


@pytest.fixture
def store(tmp_path):
    return KeyStore(tmp_path / "keys" / "secrets.json", MASTER)


@pytest.fixture
def credential_store():
    """Set one with ``keyring.set_keyring``; the one before comes back."""
    before = keyring.get_keyring()
    yield
    keyring.set_keyring(before)


def test_a_key_is_encrypted_on_disk_and_opens_after_a_restart(store):
    store.put("PROVIDER_CLOUD_API_KEY", "sk-the-real-key")
    assert "sk-the-real-key" not in store.path.read_text()
    assert KeyStore(store.path, MASTER).load() == {"PROVIDER_CLOUD_API_KEY": "sk-the-real-key"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no such file modes")
def test_the_file_is_its_owners_alone(store):
    store.put("A", "1")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_an_empty_value_removes_the_key(store):
    store.put("A", "1")
    store.put("B", "2")
    store.put("A", "")
    store.put("NEVER_SAVED", "")
    assert store.load() == {"B": "2"}


def test_another_master_key_opens_nothing_until_the_key_is_saved_again(store, caplog):
    store.put("A", "1")
    other = KeyStore(store.path, Fernet.generate_key().decode())
    assert other.load() == {} and "A does not open with this master key" in caplog.text
    other.put("A", "2")
    assert other.load() == {"A": "2"}


def test_a_master_key_of_the_wrong_form_is_refused(tmp_path):
    store = KeyStore(tmp_path / "secrets.json", "too-short")
    with pytest.raises(Unavailable, match="not a Fernet key"):
        store.put("A", "1")
    assert not store.path.exists()


def test_the_credential_store_keeps_a_master_key_made_once(tmp_path, credential_store):
    memory = Memory()
    keyring.set_keyring(memory)
    store = KeyStore(tmp_path / "secrets.json")
    store.put("A", "1")
    store.put("B", "2")
    master = memory.kept[(SERVICE, ENTRY)]
    assert len(memory.kept) == 1 and master not in store.path.read_text()
    assert KeyStore(store.path).load() == {"A": "1", "B": "2"}


def test_a_set_master_key_and_an_empty_store_leave_the_credential_store_alone(tmp_path, store, credential_store):
    memory = Memory()
    keyring.set_keyring(memory)
    store.put("A", "1")
    assert store.load() == {"A": "1"}
    assert KeyStore(tmp_path / "none.json").load() == {}
    assert memory.kept == {}


@pytest.mark.parametrize("no_store", [fail.Keyring, null.Keyring])
def test_without_a_credential_store_saving_says_what_to_set(tmp_path, credential_store, no_store):
    """fail raises, and null drops what it is given: a master key made there would be gone at the next start."""
    keyring.set_keyring(no_store())
    store = KeyStore(tmp_path / "secrets.json")
    with pytest.raises(Unavailable, match="SECRETS_MASTER_KEY"):
        store.put("A", "1")
    assert not store.path.exists()


def test_keys_that_cannot_be_opened_do_not_stop_the_start(store, credential_store, caplog):
    store.put("A", "1")
    keyring.set_keyring(fail.Keyring())
    assert KeyStore(store.path).load() == {} and "cannot be opened" in caplog.text


@pytest.mark.parametrize("text", ["{ not json", "[]", "null", '{"A": 1}'])
def test_a_file_it_cannot_read_is_never_saved_over(tmp_path, caplog, text):
    """JSON of the wrong shape included: load() runs at import, so what it does not catch stops the service starting."""
    path = tmp_path / "secrets.json"
    path.write_text(text)
    store = KeyStore(path, MASTER)
    assert store.load() == {} and "cannot be read" in caplog.text
    with pytest.raises(Unavailable, match="cannot be read"):
        store.put("A", "1")
    assert path.read_text() == text


def test_the_saved_keys_come_back_at_a_real_start(tmp_path):
    """A start in a process of its own: a saved key wins over the .env file and loses to the real environment, and no
    child gets any of them."""
    master = Fernet.generate_key().decode()
    store = KeyStore(tmp_path / "secrets.json", master)
    names = ["PROVIDER_SAVED_API_KEY", "PROVIDER_FILE_API_KEY", "PROVIDER_REAL_API_KEY", "MY_ENRICHER_KEY"]
    for name in names:
        store.put(name, "from-the-store")
    env_file = tmp_path / "settings.env"
    env_file.write_text("PROVIDER_FILE_API_KEY=from-the-file\n")
    script = f"""
import json, os
from api import app
from core import procs
names = {names!r}
print(json.dumps({{"values": {{n: os.environ.get(n) for n in names}}, "children": sorted(set(names) & set(procs.child_env()))}}))
"""
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=120,
        env={**os.environ, "ENV_FILE": str(env_file), "DATA_DIR": str(store.path.parent), "SECRETS_MASTER_KEY": master,
             "PROVIDER_REAL_API_KEY": "from-the-environment"},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    assert answer["values"] == {
        "PROVIDER_SAVED_API_KEY": "from-the-store",
        "PROVIDER_FILE_API_KEY": "from-the-store",
        "PROVIDER_REAL_API_KEY": "from-the-environment",
        "MY_ENRICHER_KEY": "from-the-store",
    }
    assert answer["children"] == []
