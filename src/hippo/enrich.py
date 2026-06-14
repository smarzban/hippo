import logging

from pydantic_ai import Agent

log = logging.getLogger("hippo.enrich")

SUMMARY_PROMPT = (
    "Write a single-paragraph summary (max 80 words) of the document below. "
    "State what it is, what it covers, and any project/system names it mentions. "
    "Output only the summary.\n\nTitle: {title}\n\n{content}"
)

CONTEXT_PROMPT = (
    "Write ONE short sentence situating this chunk within its document, naming the "
    "document and section so the chunk is retrievable on its own. Output only the sentence.\n\n"
    "Document: {title}\nSection: {section}\n\nChunk:\n{chunk}"
)


class Enricher:
    """Cheap-model ingestion enrichment: doc summaries + contextual retrieval lines."""

    def __init__(self, model):
        # model: a pydantic-ai model name string ("openai:gpt-5-mini") or Model instance (TestModel in tests)
        self._agent = Agent(
            model,
            system_prompt="You annotate documents for a search index. Be terse.",
            defer_model_check=True,
        )

    def _run(self, prompt: str, *, what: str) -> str:
        """Run the enrichment model, best-effort. Enrichment is an optimization, not a
        requirement — a document still indexes with its raw text. An empty/blank model
        response (a documented gpt-oss quirk) makes pydantic-ai retry then raise
        UnexpectedModelBehavior; a rate limit / timeout raises too. None of those must
        fail the document's ingestion, so degrade to no enrichment ("") and log it (LOW-43)."""
        try:
            return (self._agent.run_sync(prompt).output or "").strip()
        except Exception as e:  # best-effort: never let enrichment abort an ingest
            log.warning("enrichment (%s) failed, continuing without it: %s", what, e)
            return ""

    def summarize(self, title: str, content: str) -> str:
        return self._run(SUMMARY_PROMPT.format(title=title, content=content[:20000]),
                         what="summary")

    def contextualize(self, title: str, section: str, chunk: str) -> str:
        return self._run(
            CONTEXT_PROMPT.format(title=title, section=section or "(top)", chunk=chunk[:4000]),
            what="context")
