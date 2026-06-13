"""`.env.example` must document every setting and nothing fictional — a drift guard."""
import re
from pathlib import Path

from hippo.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
# Matches HIPPO_FOO= whether the line is active or commented out (# HIPPO_FOO=...).
_KEY_RE = re.compile(r"^#?\s*(HIPPO_[A-Z0-9_]+)=", re.MULTILINE)


def _documented_fields() -> set[str]:
    keys = set(_KEY_RE.findall(ENV_EXAMPLE.read_text()))
    return {k.removeprefix("HIPPO_").lower() for k in keys}


def test_env_example_covers_every_setting() -> None:
    documented = _documented_fields()
    actual = set(Settings.model_fields)  # excludes @property helpers
    assert documented == actual, (
        f"missing from .env.example: {actual - documented}; "
        f"not real settings: {documented - actual}"
    )


def test_env_example_documents_provider_keys() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "OPENAI_API_KEY=" in text
    assert "OPENAI_BASE_URL=" in text
