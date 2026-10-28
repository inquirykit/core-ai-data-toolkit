import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.app import service_error_response
from errors import Conflict, Forbidden, NotFound, Refused, ServiceError, TooLarge, Unauthorized, Unavailable
from jobs.store import JobNotFound
from models.cache import CapacityError
from vectorstore.tables import NotFound as RowNotFound


@pytest.mark.parametrize("error, status, builtin", [
    (Refused("bad"), 400, ValueError),
    (Unauthorized("no key"), 401, PermissionError),
    (Forbidden("not yours"), 403, PermissionError),
    (NotFound("gone"), 404, LookupError),
    (Conflict("busy"), 409, ValueError),
    (TooLarge("big"), 413, ValueError),
    (Unavailable("off"), 503, RuntimeError),
])
def test_each_error_has_its_status_and_is_the_builtin_callers_catch(error, status, builtin):
    assert error.status == status
    assert isinstance(error, builtin)


def test_a_status_can_be_given_and_the_message_reads_as_given():
    assert ServiceError("teapot", status=418).status == 418
    # A KeyError would quote it.
    assert str(JobNotFound("Job not found")) == "Job not found"
    assert isinstance(JobNotFound("x"), KeyError)
    assert RowNotFound("no row").status == 404


def app_raising(error: Exception) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ServiceError, service_error_response)

    @app.get("/x")
    async def x():
        raise error

    return TestClient(app)


def test_the_handler_answers_with_the_status_message_and_headers():
    r = app_raising(Conflict("Cancel the job before deleting it")).get("/x")
    assert (r.status_code, r.json()) == (409, {"detail": "Cancel the job before deleting it"})
    r = app_raising(CapacityError("every model is busy", retry_after=2.2)).get("/x")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "3"


def test_a_401_names_the_scheme():
    assert Unauthorized("no key").headers == {"WWW-Authenticate": "Bearer"}
    r = app_raising(Unauthorized("Invalid or missing API key")).get("/x")
    assert (r.status_code, r.headers["WWW-Authenticate"]) == (401, "Bearer")


def test_the_routers_raise_only_these_errors():
    """One vocabulary of refusals: a router never turns an error into a status by hand with FastAPI's HTTPException."""
    offenders = sorted(
        f.name for f in (Path(__file__).parent.parent / "api").glob("*.py")
        if f.name != "app.py" and re.search(r"from fastapi import[^\n]*HTTPException", f.read_text())
    )
    assert offenders == [], f"raise the errors.py class instead of HTTPException in {offenders}"
