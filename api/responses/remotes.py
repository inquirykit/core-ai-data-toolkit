"""/remotes: the places outside the service that buckets' files are copied to (api/remote_routes.py)."""

from typing import List, Literal, Optional

from pydantic import ConfigDict

from buckets import RemoteSpec
from .common import OPEN, Out


class RemoteInfo(RemoteSpec):
    """A remote: its spec, as PUT /remotes/{name} takes it, the buckets that name it, and the environment variables
    its secrets name that the service's environment does not set now. No secret's value is ever in it."""
    # Every field of the spec is in the answer, defaults included: required in the schema.
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    # The buckets that name it, as workspace/bucket: while one does, it is not deleted, nor where its copies are
    # changed (its scheme, its options, the variables its options that are not secrets are read from)
    buckets: List[str]
    # Variables its secrets name that are not set: the remote fails until they are
    missing: List[str]
    # When it was made and last changed (Unix time)
    created: Optional[float]
    updated: Optional[float]


class RemoteList(Out):
    remotes: List[RemoteInfo]


class RemoteStep(Out):
    step: Literal["write", "read", "list", "delete"]
    # How long it took
    ms: float


class RemoteTest(Out):
    """A test of a remote that worked: each step with its time (one that fails answers 503 with the remote's error)."""
    name: str
    scheme: str
    steps: List[RemoteStep]
