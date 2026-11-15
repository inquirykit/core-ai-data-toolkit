"""
Writing settings and keys, as the routes that change them do: a setting to the .env file (``write_settings``: setup,
PUT /admin/config), a key typed into /admin to the encrypted KeyStore (``save_secret``: a provider's, an enricher's,
a remote's), and which variables an admin may name to hold a secret (``check_secret_name``).
"""

import asyncio
import logging

from pydantic import BaseModel, Field, field_validator

from core import envfile, procs
from core.config import SETTINGS, Config
from core.errors import Conflict, Refused, ServiceError
from core.keystore import KeyStore
from models.servers import is_key_variable

logger = logging.getLogger(__name__)


async def write_settings(values: dict) -> None:
    """Save ``values`` to the .env file (Config.ENV_FILE); 500 if it cannot be written."""
    try:
        await asyncio.to_thread(envfile.update, Config.ENV_FILE, values)
    except OSError as e:
        logger.exception("Could not write %s", Config.ENV_FILE)
        raise ServiceError(f"Could not write {Config.ENV_FILE}: {e.strerror or type(e).__name__}", status=500)


class SecretRequest(BaseModel):
    model_config = {"extra": "forbid"}

    value: str = Field("", max_length=4096, description="The key; empty clears it")

    @field_validator("value")
    @classmethod
    def _fits_the_environment(cls, value: str) -> str:
        # Checked before it is saved: the environment cannot hold a NUL, so a saved one would stop the next start.
        if "\x00" in value:
            raise ValueError("a key cannot hold a NUL character")
        return value


def check_secret_name(variable: str) -> None:
    """Refuse a variable an enricher or a remote may not name as its secret: a setting of this service, which saving a
    key would change and a request would carry away; a provider's key, which is for that provider's URL alone; or one
    every child process gets (core/procs.py, passed), where a key would not stay with the service."""
    if variable in SETTINGS or is_key_variable(variable):
        raise Refused(f"{variable} is a setting of this service or a provider's key: name a variable of its own")
    if procs.passed(variable):
        raise Refused(f"{variable} is passed to the service's child processes (jobs, model servers): name a variable "
                      "of its own")


async def save_secret(keys: KeyStore, variable: str, value: str) -> None:
    """Keep a key typed into /admin encrypted (core/keystore.py) and in this process's environment, where it takes effect
    at once; empty removes it. A value in the .env file goes, so no plain copy stays. 409 when a real environment
    variable would win over it, 503 without a master key."""
    if Config.set_by_environment(variable):
        raise Conflict(f"{variable} is set in the server's environment, which wins over what is saved here")
    await asyncio.to_thread(keys.put, variable, value)
    if await asyncio.to_thread(envfile.holds, Config.ENV_FILE, variable):
        await write_settings({variable: ""})
    Config.apply({variable: value})
