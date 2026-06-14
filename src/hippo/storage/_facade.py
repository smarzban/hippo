"""The `Storage` facade. One sqlite connection, one lock, six domain mixins.

The class is intentionally thin: it owns construction (`con`, `embedder`, the
serializing `_lock`) and composes the per-domain mixins. Every method body lives
in its domain module (documents/folders/users/tokens/config_store/search); the
mixins all operate on `self.con`/`self._lock`/`self.embedder`, so cross-domain
calls (e.g. the folder cascade reusing `_delete_chunks`, token mint resolving
`_user_id_for`) resolve through this class's MRO exactly as they did when every
method shared one class body."""

import sqlite3
import threading

from ..embeddings import Embedder
from .config_store import _ConfigMixin
from .documents import _DocumentsMixin
from .folders import _FoldersMixin
from .search import _SearchMixin
from .tokens import _TokensMixin
from .users import _UsersMixin


class Storage(
    _DocumentsMixin,
    _FoldersMixin,
    _UsersMixin,
    _TokensMixin,
    _ConfigMixin,
    _SearchMixin,
):
    """All database access. The agent and ingestion never touch SQL directly."""

    def __init__(self, con: sqlite3.Connection, embedder: Embedder):
        self.con = con
        self.embedder = embedder
        # One shared connection is used from the event loop AND run_in_threadpool
        # workers (agent tools + ingest). sqlite3's per-statement mutex does NOT
        # prevent interleaved statement-stepping across threads, which raises
        # InterfaceError. Serialize every DB critical section through this lock.
        # Network embedding is always done OUTSIDE the lock so a slow ingest can't
        # block concurrent reads. (Team scale: swap for per-worker/pooled conns.)
        self._lock = threading.Lock()
