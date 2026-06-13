import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True, scope="session")
def _fast_argon2():
    """Hash at minimal cost so the suite stays fast (argon2 is local CPU, never
    network). Production keeps the library defaults."""
    from argon2 import PasswordHasher

    from hippo.auth import set_password_hasher

    set_password_hasher(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    yield
