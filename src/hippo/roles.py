"""Access-control primitives for the role-tiered folder model (SP1).

The single source of truth for the rank comparison. `storage.py` filters
retrieval with `readable_min_roles`; `api.py` gates writes with `can_write`.
Pure — imports nothing else from hippo — so the rule is testable in isolation
and there is exactly one definition of "who can see/write what"."""

ROLE_RANK: dict[str, int] = {"user": 0, "admin": 1, "owner": 2}
VALID_ROLES: tuple[str, ...] = ("user", "admin", "owner")
DEFAULT_ROLE = "user"


def rank(role: str) -> int:
    """Numeric rank for a role. Raises ValueError on an unknown role so a typo or
    a stale 'manager'/'developer' value fails loudly instead of silently denying."""
    try:
        return ROLE_RANK[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected one of {VALID_ROLES}") from None


def can_read(caller_role: str, folder_min_role: str) -> bool:
    """A caller may read a folder iff their rank is at least the folder's tier."""
    return rank(caller_role) >= rank(folder_min_role)


def can_write(caller_role: str, folder_min_role: str, origin: str) -> bool:
    """A caller may upload into a folder iff it is a manual folder AND their rank
    is at least the folder's tier. Synced ('folder') folders are pull-only."""
    return origin == "manual" and rank(caller_role) >= rank(folder_min_role)


def readable_min_roles(caller_role: str) -> tuple[str, ...]:
    """The set of folder tiers a caller may read, as a tuple of role names — used
    to build a `min_role IN (...)` SQL filter without rank math in SQL."""
    cr = rank(caller_role)
    return tuple(r for r in VALID_ROLES if ROLE_RANK[r] <= cr)
