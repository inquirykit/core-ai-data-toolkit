"""
/db/{workspace}/{bucket}/_files: the files of a bucket, a /db table made
with ``bucket`` (db/tables.py), whose bytes buckets/ keeps on disk in
DATA_DIR/files, a row per file. The list of a bucket's files is its list of rows
(GET /db/{workspace}/{bucket}, paged and filtered as any table's); these
routes write them, as nothing else may.

The client key, as for rows, under the same rules as the table routes
(``admin_for_kept``): the service's own workspaces need the admin key. An
upload's body is the file's bytes as they are, any Content-Type (recorded,
application/octet-stream when none), streamed to disk off the event loop
and refused past MAX_FILE_BYTES (413). A download is the bytes with byte
ranges, as an attachment that a browser never runs as a page of the
service's origin (Content-Security-Policy: sandbox, nosniff), since /admin
keeps its keys there.

A bucket may name a remote (GET /remotes, api/remote_routes.py), with the
admin key: its files are then copied there by a task of the kind "copy"
after each upload and delete (buckets/copies.py), a file deleted here is
deleted there, and a download whose bytes are missing from disk reads them
back from it. These routes come before /db's own, whose PUT and GET of
``/db/{ws}/{table}/{id}`` would take ``_remote`` for a row's id.

    POST   /db/{ws}/{bucket}/_files?name=   bytes  201 + the file's row
    GET    /db/{ws}/{bucket}/_files/{id}            its bytes (206 for a Range)
    DELETE /db/{ws}/{bucket}/_files/{id}            204: its row, then its bytes
    GET    /db/{ws}/{bucket}/_remote                {remote, count, copied, waiting, task} (admin)
    PUT    /db/{ws}/{bucket}/_remote   {remote}     name its remote, or none; the same one: copy now (admin)
"""

from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from core.errors import Refused
from db.tables import MAX_FILE_NAME
from . import deps
from . import responses
from .table_routes import admin_for_kept

router = APIRouter(prefix="/db", tags=["files"], dependencies=[*deps.CLIENT, Depends(admin_for_kept)])
# This module's routes that only the admin's key opens.
admin_router = APIRouter(prefix="/db", tags=["files"], dependencies=deps.ADMIN)

BINARY = {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}
# On every download: a file the browser saves and never shows as a page of this origin, nor reads as another type.
DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


async def _body(request: Request) -> AsyncIterator[bytes]:
    """The request's body as it arrives. A client gone before its end is refused (nobody reads the answer), not an
    internal error."""
    try:
        async for chunk in request.stream():
            if chunk:
                yield chunk
    except ClientDisconnect:
        raise Refused("The upload stopped: the client went away before the file was whole") from None


class SetRemote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote: Optional[str] = Field(..., min_length=1, max_length=48,
                                  description="A remote's name (GET /remotes), or null for none")


def _length(request: Request) -> Optional[int]:
    """The body's length as the request says it (Content-Length), if it says one."""
    said = request.headers.get("content-length", "")
    return int(said) if said.isdigit() else None


@router.post(
    "/{workspace}/{bucket}/_files", status_code=201, **responses.answering(responses.BucketFile),
    openapi_extra={"requestBody": {"required": True, "description": "The file's bytes, as they are", "content": BINARY}},
)
async def upload_file(
    files: deps.Buckets, request: Request, response: Response, workspace: str, bucket: str,
    name: str = Query(..., min_length=1, max_length=MAX_FILE_NAME,
                      description="The file's name: 1 to 255 characters, no control characters; need not be unique"),
):
    """Keep the request's body as a new file of the bucket, named ``name``, of the request's Content-Type: its row
    (``id``, ``name``, ``size``, ``sha256``, ``content_type``, ``created``), with its download in Location. The
    bytes are streamed to disk; an empty body is an empty file. 413 past MAX_FILE_BYTES (at once when the
    Content-Length says so), 404 for a bucket that is not there, 400 for a table that is not a bucket."""
    row = await files.upload(workspace, bucket, name, request.headers.get("content-type"), _body(request),
                             _length(request))
    response.headers["Location"] = str(request.url_for("download_file", workspace=workspace, bucket=bucket,
                                                       file_id=row["id"]))
    return row


@router.get(
    "/{workspace}/{bucket}/_files/{file_id}", response_class=Response,
    responses={200: {"description": "The file's bytes", "content": BINARY},
               206: {"description": "The part of them a Range header asked for", "content": BINARY}},
)
async def download_file(files: deps.Buckets, workspace: str, bucket: str, file_id: str):
    """The file's bytes, with the media type it was uploaded with, as an attachment named as it was: a Range
    header gets a part of them (206, with Content-Range). Bytes missing from disk are read back from the bucket's
    remote when they were copied there, then answered as usual; 503 when that fails. 404 for a file that is not
    there, or whose bytes are missing from disk and were never copied to a remote (the message says which)."""
    row, path, found = await files.locate(workspace, bucket, file_id)
    return FileResponse(path, media_type=row["content_type"], filename=row["name"], stat_result=found,
                        headers=DOWNLOAD_HEADERS)


@router.delete("/{workspace}/{bucket}/_files/{file_id}", status_code=204)
async def delete_file(files: deps.Buckets, workspace: str, bucket: str, file_id: str):
    """Delete the file: its row, then its bytes. Deleting one that is not there is not an error."""
    await files.delete(workspace, bucket, file_id)
    return Response(status_code=204)


@admin_router.get("/{workspace}/{bucket}/_remote", **responses.answering(responses.BucketRemote))
async def get_bucket_remote(copies: deps.Copies, workspace: str, bucket: str):
    """The bucket's remote (null for none), its files, how many are copied there and how many wait to be (null
    without a remote), and its latest copy (``task``: its status, progress, and the remote's error when it failed;
    null before the first). 400 for a table that is not a bucket."""
    return await copies.state(workspace, bucket)


@admin_router.put("/{workspace}/{bucket}/_remote", **responses.answering(responses.BucketRemote))
async def set_bucket_remote(copies: deps.Copies, workspace: str, bucket: str, body: SetRemote):
    """Name the bucket's remote, or none (null), and answer as GET does. A new remote gets every file, copied by a
    task queued now; what was copied to the remote before stays there (delete it there). Naming the remote it names
    already copies now what is behind (files waiting, a delete not done there): a copy that gave up, or waits for
    its retry, starts at once. 404 for a remote that is not there, 400 for a table that is not a bucket."""
    return await copies.set_remote(workspace, bucket, body.remote)
