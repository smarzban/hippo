import pytest

from knowledgehub.chunking import Chunk
from knowledgehub.db import connect
from knowledgehub.embeddings import FakeEmbedder
from knowledgehub.storage import Storage


@pytest.fixture
def store(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=32)
    s = Storage(con, FakeEmbedder(dim=32))
    docs = [
        ("polly/telegram.md", "Polly Telegram", "polly connects to telegram via webhook callbacks"),
        ("polly/slack.md", "Polly Slack", "polly posts messages to slack channels"),
        ("infra/budget.md", "Budget", "quarterly infrastructure budget planning numbers"),
    ]
    for path, title, text in docs:
        chunks = [Chunk(position=0, heading_path=title, text=text)]
        s.upsert_document(
            source_type="folder", path=path, title=title, content=text,
            content_hash=path, chunks=chunks, embed_inputs=[text],
        )
    return s


def test_keyword_match_wins_on_exact_terms(store):
    hits = store.search_hybrid("telegram webhook", top_k=3)
    assert hits[0].path == "polly/telegram.md"
    assert hits[0].heading_path == "Polly Telegram"


def test_semantic_side_contributes(store):
    # FakeEmbedder is token-overlap based; shared tokens rank the right doc up
    hits = store.search_hybrid("budget planning", top_k=3)
    assert hits[0].path == "infra/budget.md"


def test_fts_query_with_special_chars_does_not_crash(store):
    hits = store.search_hybrid('why "polly" (telegram)? -slack', top_k=3)
    assert isinstance(hits, list)


def test_grep(store):
    hits = store.grep(r"webhook", limit=10)
    assert len(hits) == 1
    assert hits[0].path == "polly/telegram.md"
    assert store.grep(r"nonexistentzzz") == []


def test_grep_invalid_regex_raises_value_error(store):
    import pytest as _pytest

    with _pytest.raises(ValueError, match="invalid regex"):
        store.grep("[")


def test_empty_query_returns_nothing(store):
    assert store.search_hybrid("   ", top_k=3) == []
