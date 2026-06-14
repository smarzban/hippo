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

# NB: the grep/KNN tuning constants (GREP_MAX_PATTERN/GREP_TIMEOUT_S/GREP_MAX_CHUNKS/
# VEC_OVERFETCH) are intentionally NOT re-exported here. They live in `.search`
# alongside the methods that read them, so `search.py` is their single source of
# truth. Re-exporting them at the package level would create a stale COPY: `grep()`
# reads the `search` binding, so patching a package-level copy would be silently
# inert (and `from .search import NAME` doesn't alias — it binds a separate name).
# Import from `hippo.storage.search` to read or monkeypatch them.

__all__ = [
    "Storage",
    "Document",
    "DocumentMeta",
    "Folder",
    "SearchHit",
    "_role_filter",
    "_norm_email",
]
