# Technical Documentation

For engineers reading, maintaining, or extending Hippo. These pages explain not
just *what* each part does but *why* it's built that way — the invariants are
load-bearing, and several were the result of adversarial review.

> Authoritative companions in the repo: `CLAUDE.md` (the working contract and
> hard rules) and `docs/superpowers/` (the original specs, decision log, and
> build plans). This guide is the narrative explanation; `CLAUDE.md` is the
> terse rulebook.

## Module map (`src/hippo/`)

| Module | Responsibility |
|---|---|
| `roles.py` | The single source of truth for role rank: `ROLE_RANK`, `rank()`, `can_read()`, `can_write()`, `readable_min_roles()`. Nothing else defines rank logic. |
| `storage/` | **All SQL.** A package of domain mixins (documents/folders/users/tokens/config_store/search) behind a thin `Storage` facade. One connection, one lock. |
| `api/` | The FastAPI app. A package: `app.py` (the `build_app` assembler), `context.py` (the dependency bundle), `auth.py` (auth dependencies + authz helpers), `models.py`, and `routes_*.py`. |
| `agent.py` | The pydantic-ai agent: four tools, the untrusted-content boundary, the cite-everything system prompt, and the grounding-detection validator. |
| `auth.py` | Identity → role resolution, the verifiers (IAP, Google ID token), password hashing, domain checks. |
| `config.py` | `Settings` (env) and the `Config` overlay resolver (DB wins for `DB_OVERRIDABLE`; secrets never from DB). |
| `db.py` | `connect()`: schema, WAL, sqlite-vec, FTS5 + triggers, root-folder seeding, legacy-DB guard. |
| `ingest.py` | The ingestion pipeline: parse → dedup → chunk → enrich → embed → index; folder sync. |
| `chunking.py` | Heading-aware Markdown chunking with atomic code fences. |
| `enrich.py` | Per-document summary + per-chunk context line (best-effort). |
| `embeddings.py` | The `Embedder` protocol; `OpenAIEmbedder`; `FakeEmbedder`. |
| `parsers.py` | `.md`/`.txt`/`.html`/`.docx` → canonical Markdown. |
| `mcp_server.py` | The FastMCP server (tools + role propagation). |
| `slack_bot.py` | The Slack Q&A bot. |
| `cli.py` | The Typer CLI. |

The UI is `ui/` (Vite + React 19).

## Reading order

1. **[Architecture](architecture.md)** — the big picture, data flow, and design principles.
2. **[RAG pipeline](rag-pipeline.md)** — how documents become an index.
3. **[Retrieval](retrieval.md)** — hybrid search, grep, reindex, role filtering.
4. **[Storage layer](storage-layer.md)** — the package, schema, lock model.
5. **[API layer](api-layer.md)** — `build_app`, the context, route modules, auth deps.
6. **[Auth & RBAC](auth-and-rbac.md)** — roles, modes, tokens, lockout.
7. **[Agent](agent.md)** — tools, prompt-injection boundary, grounding.
8. **[Config & setup](config-and-setup.md)** — the overlay, secrets policy, wizard.
9. **[Integrations](integrations.md)** — MCP server and Slack bot internals.
10. **[Embeddings](embeddings.md)** — the protocol, dimension stamping, reindex safety.
11. **[CLI](cli.md)** — every command.
12. **[Security model](security-model.md)** — the invariants and the threat model.
13. **[Development](development.md)** — test discipline, the eval harness, CI.

## The non-negotiable invariants (at a glance)

These are enforced and must stay true; each page expands on the *why*.

- **No SQL outside `storage/`.** The agent/API/ingest call the `Storage`
  interface. This is the Postgres exit ramp.
- **Tests never hit the network.** `FakeEmbedder` + pydantic-ai `TestModel`/
  `FunctionModel`; `ALLOW_MODEL_REQUESTS = False`.
- **Role rank is defined once**, in `roles.py`.
- **Retrieval methods take `role` keyword-only with no default** — a forgotten
  call site must be a `TypeError`, never an access-control leak.
- **Tool output is framed as `⟦untrusted document data⟧…⟦end⟧`** — the
  prompt-injection boundary.
- **Secrets are env-only**, never stored in or returned from the DB.
