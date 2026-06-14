# Development

Conventions and tooling for working on Hippo. The terse rulebook is `CLAUDE.md`;
this is the narrative version.

## Setup

```bash
uv sync                 # Python deps into .venv
cd ui && npm install    # UI deps
```

Run the API with `uv run hippo serve` and the UI with `cd ui && npm run dev` (see
[Install locally](../install/local-uv.md)).

## Testing

```bash
uv run pytest           # the full Python suite — fast, ZERO network
cd ui && npm test       # the Vitest UI suites
```

### Tests never hit the network (hard rule)

The suite must stay offline and fast. How it's kept that way:

- **`FakeEmbedder`** for all embeddings.
- **pydantic-ai `TestModel` / `FunctionModel`** for the agent — never a real
  model.
- Agent/API/enrich tests set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False`.

If you add a test that touches a model or the network, it's a bug. Use the fakes.

> **`.env` bleed:** a working `.env` with `HIPPO_AUTH_MODE=password` + a live
> `HIPPO_SECRET_KEY`/`HIPPO_SETUP_TOKEN` can leak into the test process and break
> tests. Run with a clean environment if needed:
> `env -i HOME="$HOME" PATH="$PATH" uv run pytest`.

### Test DB placement

Test databases must **not** live inside folders that get synced — SQLite WAL
files would pollute an `rglob`. Use a separate tmp dir for the DB (see the
`tests/test_ingest.py` fixture).

## Discipline

- **TDD:** failing test first, then the minimal change, then commit. Commit per
  green step.
- **DRY / YAGNI:** one source of truth per concept (rank in `roles.py`, SQL in
  `storage/`, secrets in env).
- **Match the surrounding code:** comment density, naming, idioms.

## The non-negotiables (enforced)

When changing code, keep these true (they have tests and reviewers behind them):

- No SQL outside `storage/`.
- Retrieval methods + `HubDeps.role` take `role` keyword-only, no default.
- The `⟦untrusted document data⟧…⟦end⟧` framing on tool output.
- `Agent(...)` constructed with `defer_model_check=True`.
- Role rank only in `roles.py`.
- Secrets never in the DB; `embedding_model`/`embedding_dim` env-only.
- One `Storage` per connection / one lock.
- The grounding validator logs; it must not raise `ModelRetry` on `/chat`.
- Chat protocol payloads require `"trigger": "submit-message"` (Vercel AI schema).

See [Security model](security-model.md) for why each matters.

## The eval harness

```bash
uv run hippo eval eval/golden.yaml
```

A retrieval **recall@k** regression gate: the golden YAML lists questions and the
documents that should be retrieved; the harness fails if recall drops. Run it
after changes that touch chunking, enrichment, embedding, or retrieval — it's the
guard that retrieval quality didn't regress (it runs offline with `FakeEmbedder`
on the seed fixtures).

## Continuous integration

`.github/workflows/ci.yml` runs **pytest** and **`npm run build`** on every PR.
Keep both green. The UI build also typechecks (`tsc`), so a type error fails CI.

## Project layout recap

- `src/hippo/` — the application (see the [module map](README.md#module-map-srchippo)).
- `ui/` — Vite + React 19 SPA. `App.tsx` is the orchestrator; focused components
  (`ChatView`, `AssistantText`, `SetupWizard`, `LoginScreen`, `UploadModal`) and
  pure helpers (`folders.ts`, `setup.ts`, `citations.ts`, `auth.ts`) are
  Vitest-covered.
- `eval/` — the recall@k harness + fixtures.
- `docs/superpowers/` — original specs, decision log, build plans.
- `docs/guide/` — this documentation set.

## Extending Hippo

- **New retrieval behavior?** Add it to `storage/search.py` behind `Storage`,
  keep `role` keyword-only, and add an eval/golden case.
- **New endpoint?** Add it to the right `routes_*.py`, depend on the shared auth
  callables, and read effective config via `ctx.cfg.get(...)`.
- **New surface?** Reuse the agent + `Storage` (like MCP/Slack do) so role
  filtering and grounding come for free — don't open a second retrieval path.
- **Another storage backend (e.g. Postgres+pgvector)?** Reimplement `Storage`
  behind the same interface; that's the exit ramp the "no SQL outside `storage/`"
  rule exists to protect.
