import pydantic_ai.models
from pydantic_ai.models.test import TestModel

from knowledgehub.enrich import Enricher

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


def test_summarize_and_contextualize_use_model():
    e = Enricher(model=TestModel(custom_output_text="A concise summary."))
    assert e.summarize("Doc Title", "# Doc\n\nlots of text") == "A concise summary."
    line = e.contextualize("Polly Guide", "Integrations > Telegram", "webhook setup...")
    assert line == "A concise summary."  # TestModel returns fixed text; wiring is what we test


def test_enricher_feeds_embedding_inputs(tmp_path):
    from knowledgehub.db import connect
    from knowledgehub.embeddings import FakeEmbedder
    from knowledgehub.ingest import Ingestor
    from knowledgehub.storage import Storage

    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    e = Enricher(model=TestModel(custom_output_text="ctxline"))
    ing = Ingestor(store, max_chars=3000, overlap_chars=0, enricher=e)
    f = tmp_path / "a.md"
    f.write_text("# A\n\nsome body text")
    res = ing.ingest_file(f, source_type="folder")
    assert res.status == "added"
    doc = store.list_documents()[0]
    assert doc.summary == "ctxline"
