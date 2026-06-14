"""Storage package — all SQL for hippo lives here (the Postgres exit ramp).

This was a single `storage.py`; it was decomposed into one mixin module per
persistence domain behind a thin `Storage` facade (LOW-01). The public surface
is unchanged: `from .storage import Storage` (and the dataclasses) resolves
exactly as before. Nothing outside this package issues SQL — the agent, API,
ingest, MCP, and Slack surfaces all call the `Storage` interface.
"""

from ._common import (
    Document,
    DocumentMeta,
    Folder,
    SearchHit,
    _norm_email,
    _role_filter,
)
from ._facade import Storage
from .search import (
    GREP_MAX_CHUNKS,
    GREP_MAX_PATTERN,
    GREP_TIMEOUT_S,
    VEC_OVERFETCH,
)

__all__ = [
    "Storage",
    "Document",
    "DocumentMeta",
    "Folder",
    "SearchHit",
    "GREP_MAX_PATTERN",
    "GREP_TIMEOUT_S",
    "GREP_MAX_CHUNKS",
    "VEC_OVERFETCH",
    "_role_filter",
    "_norm_email",
]
