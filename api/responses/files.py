"""/db/{workspace}/{bucket}/_files and _remote: a bucket's files, and the remote they are copied to
(api/file_routes.py)."""

from typing import Optional

from .common import Out
from .tasks import Task


class BucketFile(Out):
    """A file of a bucket: its row, the same as GET /db/{workspace}/{bucket} lists (its bytes are downloaded)."""
    # Made by the service when the file was uploaded: 32 hex digits
    id: str
    # The file's name as uploaded: 1 to 255 characters
    name: str
    # Bytes
    size: int
    # The SHA-256 of its bytes, in hex
    sha256: str
    # Its media type as uploaded (application/octet-stream when none was said)
    content_type: str
    # When it was uploaded (Unix time)
    created: float
    # When its bytes reached the bucket's remote (Unix time); absent until then, and in a bucket without one
    copied: Optional[float] = None


class BucketRemote(Out):
    """A bucket's remote and how its copy stands."""
    # The remote's name; null for none
    remote: Optional[str]
    # Its files
    count: int
    # Of them, copied to the remote, and waiting to be; null without a remote
    copied: Optional[int]
    waiting: Optional[int]
    # Its latest copy; null before the first
    task: Optional[Task]
