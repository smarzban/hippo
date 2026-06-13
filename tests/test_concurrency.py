"""M1: a single sqlite3 connection is shared across the event loop and threadpool
callers (agent tools + run_in_threadpool ingest). Without serialization, interleaved
statement-stepping on one connection raises InterfaceError / FK violations and can
return None rows into SearchHit. Storage must serialize DB access."""

import threading

from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


def test_concurrent_read_write_does_not_corrupt(tmp_path):
    con = connect(tmp_path / "t.db", embedding_dim=16)
    store = Storage(con, FakeEmbedder(dim=16))
    errors: list[str] = []
    folder_id = con.execute(
        "SELECT id FROM folders WHERE min_role='user' AND parent_id IS NULL"
    ).fetchone()[0]

    def writer(n: int) -> None:
        try:
            for i in range(30):
                ch = [Chunk(position=0, heading_path="H", text=f"doc {n} iter {i} telegram webhook")]
                store.upsert_document(
                    source_type="folder", path=f"p{n}-{i}.md", title="t",
                    content="c", content_hash=f"h{n}-{i}", chunks=ch,
                    embed_inputs=[c.text for c in ch],
                    folder_id=folder_id,
                )
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def reader(n: int) -> None:
        try:
            for _ in range(30):
                store.search_hybrid("telegram webhook", top_k=5, role="owner")
                store.list_documents(role="owner")
                store.grep("webhook", limit=5, role="owner")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent DB access raised: {errors[:3]}"
