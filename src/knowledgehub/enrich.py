from pydantic_ai import Agent

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
        self._agent = Agent(model, system_prompt="You annotate documents for a search index. Be terse.")

    def summarize(self, title: str, content: str) -> str:
        prompt = SUMMARY_PROMPT.format(title=title, content=content[:20000])
        return self._agent.run_sync(prompt).output.strip()

    def contextualize(self, title: str, section: str, chunk: str) -> str:
        prompt = CONTEXT_PROMPT.format(title=title, section=section or "(top)", chunk=chunk[:4000])
        return self._agent.run_sync(prompt).output.strip()
