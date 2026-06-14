"""AppContext — the dependency bundle the route modules close over.

build_app used to be a ~770-line god-function whose ~25 route handlers were
closures over a couple dozen local variables (store, cfg, settings, auth_mode,
the oidc/iap wiring, the agent cache, …). That made every helper re-derive its
context inline and made none of them testable without standing up the whole app
(MED-04). The decomposition keeps the SAME objects but gathers them onto one
`AppContext` value that the route modules receive explicitly.

Critically, this preserves the live-overlay reads: the handlers that re-resolved
effective config per request (chat_model, allowed_domain) still call
`ctx.cfg.get(...)` / `ctx.live_agent()` at request time against the very same
`cfg`/agent-cache objects — nothing is snapshotted that wasn't snapshotted before.
The construction-time snapshots (auth_mode and the oidc/iap/domain wiring, which
the original resolved once at build time) are kept as plain fields, exactly as
before.
"""

import contextlib
import logging
import secrets
import sys
from dataclasses import dataclass, field

from ..agent import build_agent
from ..auth import IapVerifier
from ..config import Config, Settings
from ..db import connect
from ..embeddings import build_embedder
from ..enrich import Enricher
from ..ingest import Ingestor
from ..mcp_server import build_mcp_server
from ..storage import Storage

log = logging.getLogger("hippo.auth")


@dataclass
class AppContext:
    """Everything the route modules and auth helpers need, assembled once."""

    settings: Settings
    store: Storage
    cfg: Config
    # Construction-time snapshots (the env/overlay value at build time). auth_mode and
    # the oidc/iap/domain wiring were resolved once in the original build_app; keep that.
    auth_mode: str
    allowed_domain: str
    oidc_client_id: str
    public_url: str
    iap_audience: str
    iap: object | None
    enricher: Enricher | None
    ingestor: Ingestor
    effective_setup_token: str
    model_override: object
    code_exchanger: object
    google_key_fetcher: object
    mcp_server_obj: object | None
    lifespan: object | None
    _agent_cache: dict = field(default_factory=dict)

    def live_agent(self):
        """chat_model is live (spec §3): rebuild the agent when the DB overlay
        changes it. Reads cfg.get('chat_model') at request time, same as before."""
        m = self.model_override or self.cfg.get("chat_model")
        if m != self._agent_cache["model"]:
            self._agent_cache.update(model=m, agent=build_agent(m))
        return self._agent_cache["agent"]


def build_context(settings, *, model_override=None, iap_verifier=None,
                  code_exchanger=None, google_key_fetcher=None) -> AppContext:
    """Assemble the connection/store/config/agent dependencies (everything the old
    build_app did before `app = FastAPI(...)`)."""
    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    embedder = build_embedder(settings)
    store = Storage(con, embedder)

    # Operational config: a DB override (config table) wins over the env Settings
    # default, but ONLY for DB_OVERRIDABLE keys (secrets/env-only keys never come
    # from the DB). With an empty config table every cfg.get(...) returns the env
    # value, so these locals are identical to settings.X — no behavior change.
    # Resolved-at-construction (changes take effect on next restart): auth_mode and
    # the oidc/iap/domain wiring. chat_model is read LIVE per /chat request.
    cfg = Config(settings, store)
    auth_mode = cfg.get("auth_mode")
    allowed_domain = cfg.get("allowed_domain")
    oidc_client_id = cfg.get("oidc_client_id")
    public_url = cfg.get("public_url")
    iap_audience = cfg.get("iap_audience")

    # First-run wizard gate. Use HIPPO_SETUP_TOKEN (env, never stored) if set; else
    # generate a random token and LOG it once at startup so the operator can read it
    # from the logs. Only generated while setup is incomplete (after that the wizard
    # is inert). Compared constant-time in POST /setup.
    effective_setup_token = settings.setup_token
    if not effective_setup_token and not store.is_setup_complete():
        effective_setup_token = secrets.token_urlsafe(24)
        # The token is a live credential that authorizes POST /setup. Emit it ONLY to
        # the local console (stderr), not the application logger that may ship to a
        # centralized aggregator where the secret would persist (LOW-22). The logger
        # gets a value-free notice so operators know where to look.
        print(f"\nHIPPO_SETUP_TOKEN not set — first-run setup token: {effective_setup_token}\n",
              file=sys.stderr, flush=True)
        log.warning("HIPPO_SETUP_TOKEN not set — a one-time first-run setup token was "
                    "printed to stderr (the console), not to this log.")

    enricher = Enricher(settings.enrich_model) if settings.enrich_enabled else None
    ingestor = Ingestor(
        store, max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars, enricher=enricher,
        max_doc_chars=settings.max_doc_chars,
        max_decompressed_bytes=settings.max_decompressed_bytes,
    )
    default_model = model_override or cfg.get("chat_model")
    agent_cache = {"model": default_model, "agent": build_agent(default_model)}

    if auth_mode == "iap" and iap_verifier is None and not iap_audience:
        raise ValueError("HIPPO_IAP_AUDIENCE is required when HIPPO_AUTH_MODE=iap")
    iap = iap_verifier or (IapVerifier(iap_audience) if auth_mode == "iap" else None)

    mcp_server_obj = build_mcp_server(store, require_auth=True) if settings.mcp_enabled else None
    lifespan = None
    if mcp_server_obj is not None:
        @contextlib.asynccontextmanager
        async def lifespan(_app):  # runs the MCP streamable-http session manager
            async with mcp_server_obj.session_manager.run():
                yield

    return AppContext(
        settings=settings, store=store, cfg=cfg, auth_mode=auth_mode,
        allowed_domain=allowed_domain, oidc_client_id=oidc_client_id,
        public_url=public_url, iap_audience=iap_audience, iap=iap,
        enricher=enricher, ingestor=ingestor,
        effective_setup_token=effective_setup_token, model_override=model_override,
        code_exchanger=code_exchanger, google_key_fetcher=google_key_fetcher,
        mcp_server_obj=mcp_server_obj, lifespan=lifespan,
        _agent_cache=agent_cache,
    )
