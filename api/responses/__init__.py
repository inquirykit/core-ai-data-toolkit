"""
Response models: what the routes that build their answers as dicts answer with.

They put those answers in the OpenAPI schema, so the browser pages' types
are generated from it (frontend/src/shared/types.ts aliases them), and check
each answer as it goes out. Every one allows fields beyond those declared,
so an answer is never cut down to its model: a field the pages do not read
yet still reaches other clients.

A field without a default is required in the schema even when it may be
null (``Optional[x]`` with no default), so the generated types say "always
there" where it is.
"""

# Every module's names, so a route says responses.X whichever module declares X, and a new model needs no line here.
# tests/test_api.py fails when two modules declare one name, which would hide one of them.
from .common import *  # noqa: F401,F403
from .admin import *  # noqa: F401,F403
from .models import *  # noqa: F401,F403
from .playground import *  # noqa: F401,F403
from .servers import *  # noqa: F401,F403
from .enrichers import *  # noqa: F401,F403
from .evals import *  # noqa: F401,F403
from .tasks import *  # noqa: F401,F403
from .jobs import *  # noqa: F401,F403
from .evidence import *  # noqa: F401,F403
from .tables import *  # noqa: F401,F403
from .files import *  # noqa: F401,F403
from .keys import *  # noqa: F401,F403
from .remotes import *  # noqa: F401,F403
from .views import *  # noqa: F401,F403
from .workflows import *  # noqa: F401,F403
