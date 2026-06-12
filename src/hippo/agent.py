from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from .storage import Storage

SYSTEM_PROMPT = """You are Hippo, the team's knowledge base — a sharp, friendly teammate who knows the
team's docs inside out. You answer ONLY from the indexed documents, found via your tools.

Grounding rules (non-negotiable):
- Always search before answering. Use multiple searches with different phrasings when the
  first results look incomplete.
- For "why" questions, prefer read_document on the most relevant document over answering
  from a fragment; follow references to other documents by searching for them.
- Use grep for exact identifiers, codenames, or acronyms that search may miss.
- Cite every claim with its source, formatted exactly as [path > section] using the path and
  section returned by your tools — e.g. [docs/polly.md > Webhook setup]. Never use numeric,
  footnote, or line-range citation markers. Never state facts without a citation.
- If the knowledge base does not contain the answer, say exactly that and name what you
  looked for. Never improvise from general knowledge.

Voice:
- Talk like a helpful colleague, not a search engine: plain language, complete sentences,
  lead with the answer, then the supporting detail.
- Weave citations into the prose where they belong rather than dumping them at the end.
- Be warm and direct ("Short answer: no — here's why"); when it helps, point out related
  docs the asker might actually be looking for.
- Quote the source where exact wording matters; paraphrase conversationally everywhere else.
- Keep it tight — conversational doesn't mean long."""


@dataclass
class HubDeps:
    store: Storage


def build_agent(model) -> Agent[HubDeps, str]:
    agent: Agent[HubDeps, str] = Agent(
        model,
        deps_type=HubDeps,
        system_prompt=SYSTEM_PROMPT,
        retries=2,
        defer_model_check=True,
    )

    @agent.tool
    def search(ctx: RunContext[HubDeps], query: str, top_k: int = 8) -> list[dict]:
        """Hybrid keyword+semantic search over the knowledge base.

        Returns chunks with provenance (path, title, section). Use this first for
        every question; vary the phrasing across calls if results look incomplete.
        """
        hits = ctx.deps.store.search_hybrid(query, top_k=max(1, top_k), role="admin")
        return [
            {
                "doc_id": h.document_id,
                "path": h.path,
                "title": h.title,
                "section": h.heading_path,
                "text": h.text,
            }
            for h in hits
        ]

    @agent.tool
    def read_document(ctx: RunContext[HubDeps], doc_id: int) -> dict:
        """Read a full document by id (ids come from search/list_documents results).

        Use this when a chunk looks relevant but truncated, and for 'why' questions
        where surrounding context matters.
        """
        doc = ctx.deps.store.get_document(doc_id, role="admin")
        if doc is None:
            return {"error": f"no document with id {doc_id}"}
        return {"doc_id": doc.id, "path": doc.path, "title": doc.title, "content": doc.content}

    @agent.tool
    def list_documents(ctx: RunContext[HubDeps], query: str | None = None) -> list[dict]:
        """Browse indexed documents (titles + summaries), optionally filtered.

        Use this to discover which documents exist about a topic before deep-diving.
        """
        return [
            {"doc_id": d.id, "path": d.path, "title": d.title, "summary": d.summary or ""}
            for d in ctx.deps.store.list_documents(query=query, role="admin")
        ]

    @agent.tool
    def grep(ctx: RunContext[HubDeps], pattern: str) -> list[dict]:
        """Exact regex scan over raw document text (case-insensitive).

        Use for identifiers, codenames, acronyms, or exact strings that fuzzy
        search might miss (e.g. 'POLLY_WEBHOOK_URL').
        """
        try:
            hits = ctx.deps.store.grep(pattern, role="admin")
        except ValueError as e:
            return [{"error": str(e)}]
        return [
            {"doc_id": h.document_id, "path": h.path, "section": h.heading_path, "text": h.text}
            for h in hits
        ]

    return agent
