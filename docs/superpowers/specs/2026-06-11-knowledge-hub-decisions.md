# Knowledge Hub — Design Decision Log

**Date:** 2026-06-11
**Companion to:** `2026-06-11-knowledge-hub-design.md` (the design spec — *what* we're building; this document records *why*)

Each entry: the decision, the options considered, and the reasoning. Format loosely follows ADRs (Architecture Decision Records).

---

## D1. Audience & deployment: personal first, team-ready

**Decision:** Build as a local/personal tool, architected so it can be deployed for the team later.

**Options:** team-shared from day one / personal only / personal now, team later.

**Why:** Avoids premature multi-user complexity (auth, shared infra) while it's one user, but bakes in the cheap forward-compatibility: a clean API layer, an auth middleware stub, and a storage interface with a Postgres exit ramp. Team deployment becomes additive, not a rewrite.

---

## D2. Ingestion model: syncing connectors + chat upload

**Decision:** Background-syncing sources (local folders in v1) plus drag-and-drop upload in the chat UI. CLI drives v1 syncing.

**Options:** drop-folder + CLI only / upload-via-chat only / continuous connectors / CLI now + connectors later.

**Why:** Connectors keep the hub current without manual effort (the long-term vision); chat upload covers the "here, absorb this" moment during a conversation. v1 scopes connectors down to local folders — Google Docs are handled by exporting/Drive-syncing into a watched folder — with a connector interface (`list_items()` / `fetch()`) designed in so Drive/Confluence are later additions, not redesigns.

---

## D3. Chat surface: web app now, Slack later

**Decision:** Web chat UI as the v1 surface; the agent exposed via an API so a Slack bot can be added when the team adopts it.

**Options:** web app / Slack bot first / terminal CLI chat.

**Why:** Web app works for one user now and the team later with one deployment. Slack-first would tie testing and iteration to Slack's surface. The decisive architectural consequence: every front door (web, Slack, future CLI) is a client of the same `POST /chat` API.

---

## D4. Retrieval philosophy: hybrid with agentic escalation

**Decision:** Fast hybrid (keyword + vector) search as the first pass, with the agent able to escalate to multi-step retrieval (read full docs, follow references, re-search) when the question demands it. Escalation is emergent from a tool-use loop, not a hard-coded router.

**Options considered (and why not):**

- **Classic one-shot RAG** (retrieve top-k → stuff → answer): cheapest, but fails on the flagship "why did we do X" questions, which need answers *assembled* across a decision doc + RFC + retro. Our design degenerates to one-shot RAG when one search suffices, so we lose nothing.
- **Pure agentic grep over raw files** (Claude Code style, no embeddings): zero ingestion pipeline, never stale — but keyword-only recall misses semantically-phrased questions ("what does X solve"). *Borrowed:* the agent gets a `grep` tool alongside hybrid search.
- **Long-context stuffing** (no retrieval; paste corpus into a 1M-token prompt): perfect recall for tiny corpora, but cost scales with corpus size per question and hits a hard ceiling as the corpus grows — then you build retrieval in a hurry. Wrong for an accumulating team brain.
- **GraphRAG / knowledge graphs**: purpose-built for "why"/cross-doc synthesis, but 5× ingestion complexity, fragile LLM extraction, hard-to-detect staleness. Deferred — the agent does the same connecting lazily at query time. Revisit if query-time digging proves too slow at scale.
- **Hierarchical summarization (RAPTOR-style)**: great for broad survey questions; heavy ingestion machinery. *Borrowed the light version:* one-paragraph document summaries generated at ingestion, searchable via `list_documents`.
- **Explicit two-tier routing** (classifier picks cheap path vs agent path): the router becomes its own failure mode; the tool loop already gets the same economics implicitly (stops after one search when that's enough).

**Also adopted:** contextual retrieval (Anthropic's published technique) — a cheap-model-generated context line prepended to each chunk before embedding. Material retrieval-quality win for a small, config-toggleable ingestion cost.

---

## D5. Agent framework: Pydantic AI

**Decision:** Pydantic AI as the agent harness (provider-agnostic, Python).

**Options considered:**

- **Vercel AI SDK (TS)** — best provider abstraction in TS, streaming chat UI nearly free via `useChat`. Was the original recommendation *when the web UI was assumed to be a TS frontend workstream*. Lost its decisive edge once we found Pydantic AI natively speaks the Vercel AI protocol (see D7) — we get its UI benefits without committing the backend to TS.
- **Mastra (TS)** — batteries-included (built-in RAG, workflows, memory); fastest v1, but the retrieval design — the differentiating part of this project — would live inside their abstractions. Framework risk + less learning.
- **Custom loop + LiteLLM** — maximum ownership; but the loop is the easy 10%, and the plumbing (per-provider tool-call normalization, streaming, retries) is solved commodity code elsewhere.
- **pi.dev** — genuinely minimal and hackable, but essentially a one-maintainer project; adopting it buys ~500 lines we could own ourselves.
- **Claude Agent SDK** — strongest agentic harness (subagents, context management, MCP) but Anthropic-only; ruled out by the provider-agnostic requirement.
- **LangGraph** — graph engine for complex stateful workflows we don't have; massive abstraction surface for "a chat agent with four retrieval tools".
- **LlamaIndex** — RAG-first with ready connectors, but weak agent layer and notorious customization pain exactly where our design is custom (agentic escalation).

**Why Pydantic AI won:** right-sized (agent + tools, nothing more), best typed ergonomics in Python (DI for handing the storage layer to tools, validated structured outputs), provider-agnostic out of the box, `pydantic-evals` for retrieval-quality regression testing, Python's deeper document-parsing ecosystem for future ingestion growth, and high maintenance confidence (Pydantic team). The deciding insight: **for a headless/API-first system, Pydantic AI beats the AI SDK; the UI question is solved separately** (D7).

---

## D6. Storage: SQLite + sqlite-vec + FTS5 (not a dedicated vector DB)

**Decision:** One embedded SQLite file holding documents, chunks, an FTS5 keyword index, and a sqlite-vec vector index. Hybrid search merged with Reciprocal Rank Fusion.

**Options:** Qdrant (or similar dedicated vector DB) / Postgres + pgvector / embedded SQLite.

**Why:** Dedicated vector DBs earn their keep at millions of vectors under concurrent load — this corpus is thousands of documents / tens-of-thousands of chunks, where SQLite's exact brute-force scan is milliseconds and Qdrant's HNSW approximation solves a problem we don't have. Embedded storage means zero infrastructure, backup-by-file-copy, and keyword + vector + metadata in one query layer (vs. reconciling IDs across two stores). FTS5 covers proper nouns/codenames ("polly", "telegram") where embeddings are weak.

**Known limits, accepted:** single-writer (WAL mode handles v1's one-ingester-many-readers); the exit ramp is reimplementing the storage interface on Postgres + pgvector when team-scale concurrency demands it. Embedding model is stamped per vector; `reindex` re-embeds on model swaps.

---

## D7. Frontend: React chat UI over the Vercel AI Data Stream Protocol

**Decision:** React chat frontend consuming the Vercel AI protocol that Pydantic AI natively emits; start from the official `pydantic/ai-chat-ui`.

**Options considered:**

- **Open WebUI** (via OpenAI-compat shim): lots of UI free (auth/RBAC, history) but the shim flattens agent richness (tool traces, citations become markdown), and drag-drop uploads route to *its* built-in RAG, not our pipeline — fighting the product on a stated requirement.
- **Chainlit**: 100% Python, agent-aware rendering; solid runner-up for internal tools, less UI control long-term.
- **AG-UI/CopilotKit**: for generative-UI apps where the agent drives frontend state; overkill for Q&A chat.
- **Streamlit/Gradio**: demo-grade; outgrown fast.

**Why:** The Vercel-protocol pairing is the one officially supported by the Pydantic team, gives streaming + tool-call progress display + full control over upload and citation UX, and resolves the D5 dilemma — Python backend ergonomics *and* the polished React chat surface, without OpenAI-compat flattening.

---

## D8. LLM strategy: provider-agnostic, config-driven

**Decision:** No hard provider dependency. Chat model, enrichment model (cheap), and embedding model are all config. Default embeddings: OpenAI `text-embedding-3-small` (cheap, ubiquitous), swappable to Voyage/Ollama.

**Why:** User requirement. Pydantic AI's model abstraction makes the chat model a config string; the embedding layer records the model per vector so swaps are a `reindex`, not a migration crisis. Cost note: provider lock-in is avoided at the price of not using provider-exclusive features (e.g. Anthropic server-side tools); acceptable for this system's needs.

---

## D9. Honesty over coverage

**Decision:** The agent answers only from retrieved content, cites every claim (document + section), and says "the knowledge base doesn't cover this" rather than improvising. Tool-call cap (~15) with honest partial answers at the limit.

**Why:** A knowledge hub people don't trust is worthless. Structural citations (provenance flows from the storage schema through tools to the UI) are the core trust feature; refusing to free-style from general knowledge is what distinguishes "team brain" from "chatbot with vibes".
