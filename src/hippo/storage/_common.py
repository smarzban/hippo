"""Shared types, helpers, and tuning constants for the storage package.

Every domain mixin (documents/folders/users/tokens/config/search) imports from
here so the dataclasses, the role-filter SQL fragment, and the email-normalizer
have a single definition. The logger name stays `hippo.storage` so the package
split is invisible to anything filtering logs by logger name."""

import logging
from dataclasses import dataclass

from ..roles import readable_min_roles

log = logging.getLogger("hippo.storage")


@dataclass
class Document:
    id: int
    source_type: str        # stored + forward-compat; not read off a returned Document (INF-05/08)
    path: str
    title: str
    content: str
    content_hash: str       # dedup compares it via is_unchanged()/SQL, not off the instance
    summary: str | None


@dataclass
class DocumentMeta:
    """Lightweight document projection — no `content` column. For browse/list surfaces
    (agent list_documents tool, MCP, GET /documents) that only need metadata, so the
    full canonical markdown of every document isn't read+materialized per request."""
    id: int
    path: str
    title: str
    summary: str | None


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    path: str
    title: str
    heading_path: str
    text: str
    score: float


@dataclass
class Folder:
    id: int
    parent_id: int | None
    name: str
    min_role: str
    origin: str          # manual | folder
    location: str | None
    doc_count: int


def _norm_email(e: str) -> str:
    return e.strip().lower()


def _role_filter(role: str) -> tuple[str, tuple[str, ...]]:
    """Return an SQL fragment + params restricting documents to folders the role
    may read. Joins assume the folders table is aliased `f`."""
    allowed = readable_min_roles(role)  # raises ValueError on an unknown role
    placeholders = ",".join("?" * len(allowed))
    return f"f.min_role IN ({placeholders})", allowed
