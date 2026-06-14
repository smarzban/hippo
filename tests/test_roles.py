import pytest

from hippo.roles import (
    DEFAULT_ROLE,
    ROLE_RANK,
    VALID_ROLES,
    can_read,
    can_write,
    rank,
    readable_min_roles,
)


def test_ranks_are_ordered():
    assert ROLE_RANK == {"user": 0, "admin": 1, "owner": 2}
    assert VALID_ROLES == ("user", "admin", "owner")
    assert DEFAULT_ROLE == "user"
    assert rank("user") < rank("admin") < rank("owner")


def test_rank_rejects_unknown_role():
    with pytest.raises(ValueError):
        rank("manager")  # old role name is gone


@pytest.mark.parametrize(
    "caller,folder,expected",
    [
        ("user", "user", True),
        ("user", "admin", False),
        ("user", "owner", False),
        ("admin", "user", True),
        ("admin", "admin", True),
        ("admin", "owner", False),
        ("owner", "user", True),
        ("owner", "admin", True),
        ("owner", "owner", True),
    ],
)
def test_can_read_is_rank_gte(caller, folder, expected):
    assert can_read(caller, folder) is expected


def test_can_write_requires_manual_origin_and_rank():
    assert can_write("owner", "owner", "manual") is True
    assert can_write("owner", "owner", "folder") is False  # synced = upload-locked
    assert can_write("user", "admin", "manual") is False   # below tier


def test_readable_min_roles_grows_with_rank():
    assert readable_min_roles("user") == ("user",)
    assert readable_min_roles("admin") == ("user", "admin")
    assert readable_min_roles("owner") == ("user", "admin", "owner")
