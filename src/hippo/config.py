from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs. Override via env vars prefixed HIPPO_ (e.g. HIPPO_CHAT_MODEL)."""

    # extra="ignore": .env also holds provider keys (OPENAI_API_KEY etc.) that
    # belong to the process environment, not to Settings.
    model_config = SettingsConfigDict(env_prefix="HIPPO_", env_file=".env", extra="ignore")

    db_path: Path = Path("hippo.db")
    chat_model: str = "openai:gpt-5.2"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embed_timeout_s: float = 60.0   # per-request bound on the embedding endpoint (SDK default is 600s)
    embed_max_retries: int = 2      # embedding client retry budget on transient failures
    enrich_enabled: bool = True
    enrich_model: str = "openai:gpt-5-mini"
    chunk_max_chars: int = 3000  # ~750 tokens
    chunk_overlap_chars: int = 200
    max_upload_bytes: int = 10_485_760  # 10 MiB — reject larger uploads pre-decode
    max_doc_chars: int = 1_000_000      # skip docs whose parsed text exceeds this (pre-embed)
    max_decompressed_bytes: int = 100_000_000  # docx ZIP-bomb guard (100 MB uncompressed)
    max_tool_calls: int = 15
    search_top_k: int = 8

    # --- auth (spec §1) ---
    auth_mode: Literal["none", "oidc", "iap", "password"] = "none"
    allowed_domain: str = ""  # e.g. example.com; empty = any domain
    admin_emails: str = ""  # comma-separated bootstrap admins (always admin)
    secret_key: str = ""  # session-cookie signing; required in oidc mode
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    public_url: str = "http://localhost:8000"  # OIDC redirect URI base
    iap_audience: str = ""  # /projects/<n>/global/backendServices/<m>
    # --- sources / upload-to-repo (spec §1+2) ---
    source_roots: str = ""  # colon-separated dirs /sources may register
    github_token: str = ""
    github_docs_repo: str = ""  # e.g. example/hippo-docs
    github_managers_repo: str = ""
    github_branch: str = "main"
    ui_dist: str = ""  # path to built UI (ui/dist); empty = don't serve static UI
    mcp_enabled: bool = True  # mount the /mcp MCP server
    # --- slack bot (spec: 2026-06-13-slack-integration) ---
    slack_enabled: bool = False  # `hippo slack` refuses to start unless true
    slack_bot_token: str = ""    # xoxb-… bot token
    slack_app_token: str = ""    # xapp-… app-level token (Socket Mode)
    setup_token: str = ""  # first-run wizard gate; if empty, a random one is logged at startup

    @property
    def admin_email_list(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def source_root_list(self) -> list[Path]:
        return [Path(p).resolve() for p in self.source_roots.split(":") if p.strip()]


def get_settings() -> Settings:
    return Settings()


# Operational keys the DB config store may override (env supplies the default).
# Everything NOT here is ENV-ONLY and never read from the DB:
#   - secrets/bootstrap: provider keys, oidc_client_secret, secret_key, db_path,
#     setup_token, github_*, source_roots;
#   - embedding_model / embedding_dim: these define the vector space and the
#     chunk_vec table width, which is fixed at table creation and only changeable
#     via `hippo reindex` (a CLI op that reads env). A DB override could not take
#     effect AND could silently go stale relative to env after a reindex, so the
#     embedder is the single source of truth — they stay env-only.
DB_OVERRIDABLE: frozenset[str] = frozenset({
    "auth_mode", "chat_model", "enrich_model",
    "allowed_domain", "oidc_client_id", "public_url", "iap_audience",
})


class Config:
    """Live operational config: a DB value (if set) overrides the env Settings
    default — but ONLY for DB_OVERRIDABLE keys. Secrets/env-only keys always come
    from Settings, so a stray DB row can never leak or override a secret."""

    def __init__(self, settings: "Settings", store):
        self.settings = settings
        self.store = store

    def get(self, key: str):
        if key in DB_OVERRIDABLE:
            v = self.store.get_config(key)
            if v is not None:
                return v
        return getattr(self.settings, key)
