import pydantic_ai.models
from pydantic_ai.models.test import TestModel

from hippo.enrich import Enricher

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


def test_summarize_and_contextualize_use_model():
    e = Enricher(model=TestModel(custom_output_text="A concise summary."))
    assert e.summarize("Doc Title", "# Doc\n\nlots of text") == "A concise summary."
    line = e.contextualize("Polly Guide", "Integrations > Telegram", "webhook setup...")
    assert line == "A concise summary."  # TestModel returns fixed text; wiring is what we test


def test_enricher_feeds_embedding_inputs(tmp_path):
    from hippo.db import connect
    from hippo.embeddings import FakeEmbedder
    from hippo.ingest import Ingestor
    from hippo.storage import Storage

    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    e = Enricher(model=TestModel(custom_output_text="ctxline"))
    ing = Ingestor(store, max_chars=3000, overlap_chars=0, enricher=e)
    f = tmp_path / "a.md"
    f.write_text("# A\n\nsome body text")
    res = ing.ingest_file(f, source_type="folder")
    assert res.status == "added"
    doc = store.list_documents()[0]
    assert doc.summary == "ctxline"


def test_enricher_constructs_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    Enricher("openai:gpt-5-mini")  # must not raise at construction
