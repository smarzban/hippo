"""Request body schemas + shared validation constants + the upload-filename
sanitizer. Kept together so the route modules import their schemas from one place."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    """Reduce an upload filename to a safe, clean basename for the document path.
    Path-stripped by the caller; this removes query/fragment/space chars."""
    base = Path(name).name  # strip any path components
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    return cleaned


class FolderIn(BaseModel):
    parent_id: int
    name: str
    origin: Literal["manual", "folder"] = "manual"
    location: str | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None


class RoleIn(BaseModel):
    role: str  # validated manually in the route handler so we return 400 (not 422)


class TokenIn(BaseModel):
    name: str = ""


class ProfileIn(BaseModel):
    name: str = ""  # email is read-only; only the display name is self-editable


class CreateUserIn(BaseModel):
    email: str
    role: str = "user"  # validated in the handler so we return 400, not 422
    name: str = ""


MIN_PASSWORD_LEN = 8
MAX_NAME_LEN = 100
# Reject multiple @, empty local/domain labels, and trailing dots (e.g. a@b@c.com,
# a@.com, a@b.). Not RFC-perfect — a sanity gate so we never create a login identity
# that can't be typed back. Single @, non-empty local, dotted non-empty domain labels.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
