# Fresh-clone fixes & small improvements

> Bug-fix / polish branch (`fix/fresh-clone-ui-findings`). Surfaced by a clean-room
> "fresh clone" run-through of the README on 2026-06-13. Not a feature — surgical fixes
> plus onboarding docs. Review rigor unchanged (Codex + Opus on the PR before landing).

**Goal:** Fix three UI/agent defects a new user hits, and close the two onboarding gaps
that made the fresh-clone run harder than it should be.

---

## Findings (from the fresh-clone run)

1. **Settings gear never renders in `npm run dev`.** `App.tsx` fetches `/me` to learn the
   user; Vite's dev proxy doesn't forward `/me` (or `/auth`), so the request hits the Vite
   server, returns HTML, `r.json()` throws, `me` stays `null`, and the gear + whoami line
   never render. Dev-only — single-origin (FastAPI serving `ui/dist`) is same-origin and works,
   which is why CI/tests never caught it.

2. **Adjacent citations render as `` `⟦1⟧``⟦2⟧` ``.** `processCitations` rewrites each citation
   to a backtick-wrapped sentinel so it survives as an inline-code node. Two citations emitted
   back-to-back by the model produce `` `⟦1⟧``⟦2⟧` ``; CommonMark parses the middle `` `` `` as
   part of one code span, so `MARKER_RE` no longer matches and the user sees stray backticks.

3. **"⚠ No sources cited" warning misfires on refusals.** The warning fires on
   `sources.length === 0 && processed.length > 120`. A *long refusal* ("I couldn't find any
   document…") is sourceless and long, so it trips the wire — a false positive that breeds alarm
   fatigue. Hybrid search always returns top-k chunks, so "did retrieval return anything?" is
   true even here; relevance is the model's judgment, so the refusal signal must come from the
   model itself.

4. **Onboarding gaps.** No `.env.example` ships (a cloner reverse-engineers `.env` from the
   README table), and the Quickstart only documents the OpenAI path — nothing on the Ollama/local
   setup (`OPENAI_BASE_URL`, gpt-oss models, dim 768, the `set -a; source .env` requirement).

5. **Leftover name.** `ui/package.json` still names the package `knowledgehub-ui` (missed in the
   knowledgehub→hippo rename).

---

## Tasks

### Task 1: Vite dev proxy — add `/me` and `/auth`

**Files:** Modify `ui/vite.config.ts`

- [ ] Add `"/me": "http://127.0.0.1:8000"` and `"/auth": "http://127.0.0.1:8000"` to the `proxy` map.
- [ ] `cd ui && npm run build` succeeds. Manual: gear + Settings reachable in `npm run dev`.

### Task 2: Vitest harness + adjacent-citation fix

**Files:** Modify `ui/package.json` (add `vitest` devDep + `"test": "vitest run"` script; fix
`name` → `hippo-ui`). Create `ui/src/citations.test.ts`. Modify `ui/src/citations.ts`.

- [ ] Add `vitest` to devDependencies and a `test` script.
- [ ] **Failing test first:** two adjacent citations (`[A > x][A > y]`) → `processed` contains two
  separately-parseable markers (no `` `` `` collision), and `sources.length === 2`.
- [ ] Fix `processCitations`: after the replace, separate adjacent backtick-wrapped sentinels
  (insert a space between a closing and an immediately-following opening backtick of two markers).
- [ ] Keep existing behavior green: single citation, fullwidth `【…】`, ambiguous/non-clickable,
  dedup of repeated citations.
- [ ] `npm test` passes.

### Task 3: Refusal marker — suppress warning on genuine refusals

**Files:** Modify `src/hippo/agent.py` (system prompt). Modify `ui/src/citations.ts` (sentinel +
pure helper). Modify `ui/src/App.tsx` (`AssistantText`). Add tests in `ui/src/citations.test.ts`.

- [ ] Agent system prompt: when the KB lacks the answer, after the refusal sentence the model
  appends, on its own final line, the exact marker `<!--hippo:no-sources-->`.
- [ ] `citations.ts`: export `NO_SOURCES_MARKER` and `stripNoSourcesMarker(text) ->
  { text, refused }` (removes every occurrence, trims, reports presence). Pure; fails safe.
- [ ] **Failing test first:** `stripNoSourcesMarker` removes the marker and reports `refused: true`;
  absent → `refused: false`, text unchanged.
- [ ] `AssistantText`: run text through `stripNoSourcesMarker` before `processCitations`; suppress
  the no-sources warning when `refused` (keep `>120` gate for the genuine uncited-claim case).
- [ ] `npm test` + `npm run build` pass.

### Task 4: `.env.example` (all settings) + README Ollama Quickstart

**Files:** Create `.env.example`. Modify `README.md`.

- [ ] `.env.example`: every `HIPPO_*` setting from `config.py` (commented, with its default) grouped
  by section, plus the `OPENAI_*` provider vars, with a header note that `OPENAI_*` must be in the
  process env (`set -a; source .env; set +a`) and that Ollama needs a local embedding model.
- [ ] README: add an "Ollama / local" Quickstart variant alongside the OpenAI one; mention
  `cp .env.example .env`.
- [ ] Keep `.env.example` tracked (it's a template — no secrets); `.gitignore` still ignores `.env`.

### Task 5: CI — run frontend tests

**Files:** Modify `.github/workflows/ci.yml`

- [ ] Add `npm test` (in `ui/`) to the UI job after install, before/after build.
- [ ] Workflow still valid YAML.

---

## Verification

- `uv run pytest -q` — full suite green, zero network.
- `cd ui && npm install && npm test && npm run build` — Vitest green, UI builds clean.
- Manual (fresh clone, `npm run dev`): gear renders; two-citation answer shows clean `¹ ²`;
  "vacation policy" refusal shows **no** warning; a forced uncited claim still warns.
