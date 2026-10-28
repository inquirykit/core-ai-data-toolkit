"""
First-run setup wizard: /admin/setup.

While no API key is configured (and SETUP_WIZARD is not false), /admin sends
the browser here. The wizard picks the admin and client keys, the allowed
models and browser access, and writes them to the .env file (Config.ENV_FILE).

Until then the service has no keys, so anyone who can reach it could finish
setup and lock its owner out. Saving therefore needs the one-time setup code
the server prints to its log at startup (see api.app).
"""

import asyncio
import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, constr, model_validator

import envfile
from config import Config
from errors import Conflict, Forbidden, ServiceError
from .pages import page
from . import responses

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/setup")

# Values go straight into .env, so none of them may contain whitespace,
# quotes, commas or "#".
ApiKey = constr(pattern=r"^[A-Za-z0-9._~+/=-]{16,256}$")
ModelId = constr(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
Origin = constr(pattern=r"^(\*|https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?)$")


class SetupCode(BaseModel):
    model_config = {"extra": "forbid"}

    code: str = Field(..., max_length=256)


class SetupRequest(SetupCode):
    admin_api_key: ApiKey
    # None: clients need no key.
    api_key: Optional[ApiKey] = None
    # None: any Hugging Face Hub model.
    allowed_models: Optional[List[ModelId]] = Field(None, min_length=1, max_length=500)
    # Empty: no browser access from other origins.
    cors_origins: List[Origin] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def keys_differ(self):
        if self.api_key is not None and secrets.compare_digest(self.api_key, self.admin_api_key):
            raise ValueError("the client key and the admin key must differ")
        return self


def _check_code(request: Request, code: str) -> None:
    if not Config.needs_setup():
        raise Conflict("This instance is already set up")
    expected = request.app.state.setup_code
    if not expected or not secrets.compare_digest(code.encode(), expected.encode()):
        raise Forbidden("Wrong setup code: use the one in the server log")


async def write_settings(values: dict) -> None:
    """Save ``values`` to the .env file (Config.ENV_FILE); 500 if it cannot be written."""
    try:
        await asyncio.to_thread(envfile.update, Config.ENV_FILE, values)
    except OSError as e:
        logger.exception("Could not write %s", Config.ENV_FILE)
        raise ServiceError(f"Could not write {Config.ENV_FILE}: {e.strerror or type(e).__name__}", status=500)


@router.get("", include_in_schema=False)
async def setup_page():
    return page("setup.html")


@router.get("/status", **responses.answering(responses.SetupStatus))
async def setup_status():
    """Whether the wizard should run. Public: the page needs it before any key exists."""
    return {"needed": Config.needs_setup()}


@router.post("", **responses.answering(responses.SetupResult))
async def complete_setup(request: Request, body: SetupRequest):
    """Write the chosen settings to the .env file and apply what can be applied now."""
    _check_code(request, body.code)
    values = {
        "ADMIN_API_KEY": body.admin_api_key,
        "API_KEY": body.api_key or "",
        "ALLOWED_MODELS": ",".join(body.allowed_models or []),
        "CORS_ORIGINS": ",".join(body.cors_origins),
    }
    await write_settings(values)

    notes = []
    if body.cors_origins != Config.CORS_ORIGINS:
        notes.append("Restart the server for the browser access (CORS) setting to take effect.")
    if Config.WORKERS > 1:
        notes.append("Restart the server: only this worker has the new keys until then.")
    Config.apply({k: v for k, v in values.items() if k != "CORS_ORIGINS"})
    request.app.state.setup_code = None
    logger.info("Setup complete; settings written to %s", Config.ENV_FILE)
    return {"env_file": str(Config.ENV_FILE), "restart_required": bool(notes), "notes": notes}


@router.post("/skip", **responses.answering(responses.SetupSkipped))
async def skip_setup(request: Request, body: SetupCode):
    """Run without keys (local development) and stop showing the wizard."""
    _check_code(request, body.code)
    await write_settings({"SETUP_WIZARD": "false"})
    Config.apply({"SETUP_WIZARD": "false"})
    request.app.state.setup_code = None
    logger.info("Setup skipped; SETUP_WIZARD=false written to %s", Config.ENV_FILE)
    return {"env_file": str(Config.ENV_FILE)}
