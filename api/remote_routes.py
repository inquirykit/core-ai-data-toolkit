"""
/remotes: places outside the service that a bucket's files are copied to (S3,
R2, B2, GCS, Azure Blob, a folder...), each an OpenDAL scheme with its options
(see buckets/remotes.py). A bucket names one with PUT /db/{ws}/{bucket}/_remote
(api/file_routes.py).

A secret option (a key, a password, a token) is never given: ``secrets`` names
the environment variable of the service that holds it, which is read when the
remote is used, never stored or shown, and kept from the service's child
processes. An option in ``options`` whose name looks like a secret's is
refused (400). All of it needs the admin key; the Remotes section of /admin is
a client of these routes.

    GET    /remotes                   {remotes}: each spec, the buckets naming it, the variables not set
    GET    /remotes/{name}
    PUT    /remotes/{name}  spec      create or replace it; 409 to move one a bucket names; its buckets' copies asked
    DELETE /remotes/{name}            204; 409 while a bucket names it
    POST   /remotes/{name}/test       write, read, list and delete a probe object; 503 with the remote's error
"""

from fastapi import APIRouter, Response

from buckets import RemoteSpec
from core.errors import Refused
from . import audit, deps
from . import responses
from .settings import check_secret_name

router = APIRouter(prefix="/remotes", tags=["remotes"], dependencies=deps.ADMIN)


@router.get("", **responses.answering(responses.RemoteList))
async def list_remotes(remotes: deps.Remotes):
    """Every remote: its spec, the buckets that name it (``workspace/bucket``), and ``missing``, the environment
    variables its secrets name that the service's environment does not set (the remote fails until they are)."""
    return {"remotes": await remotes.list()}


@router.get("/{name}", **responses.answering(responses.RemoteInfo))
async def get_remote(remotes: deps.Remotes, name: str):
    """One remote, as GET /remotes lists it."""
    return await remotes.describe(name)


@router.put("/{name}", **responses.answering(responses.RemoteInfo))
async def put_remote(remotes: deps.Remotes, name: str, spec: RemoteSpec):
    """Create or replace a remote. 400 for an option in ``options`` whose name looks like a secret's (key, secret,
    password, token, credential, connection_string) or whose value is a URL with a password in it: name the
    environment variable that holds it in ``secrets`` instead. 400 too for a variable in ``secrets`` that is one of the
    service's settings, a provider's key or a variable its child processes get, and for a folder (``fs``) whose root is the buckets' folder (DATA_DIR/files), inside it or above it.
    While a bucket names the remote, what says where the copies are (its scheme, its options, and the variables its
    options that are not secrets are read from) cannot change (409): make another remote and name that on the bucket.
    Its label and the variables of its secrets can (a key rotated); then the copies of the buckets that name it that
    are behind are queued, in case it works now."""
    if spec.name != name:
        raise Refused(f"The body names {spec.name!r}, the path {name!r}")
    for variable in spec.secrets.values():
        check_secret_name(variable)
    return await remotes.put(spec)


@router.delete("/{name}", status_code=204, response_class=Response)
async def delete_remote(remotes: deps.Remotes, name: str):
    """Delete a remote; 409 while a bucket names it. What was copied there stays there."""
    await remotes.delete(name)


@router.post("/{name}/test", **responses.answering(responses.RemoteTest))
@audit.reads
async def test_remote(remotes: deps.Remotes, name: str):
    """Try the remote now: write a small probe object under ``.toolkit-probe/`` at its root, read it back, find it in
    a list and delete it, answering each step with its time. 503 for the first step that fails, with the remote's
    own message and the steps that worked before it, or naming a variable its secrets name that is not set."""
    return await remotes.test(name)
