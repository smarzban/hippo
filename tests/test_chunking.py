from knowledgehub.chunking import Chunk, chunk_markdown


def test_heading_path_recorded():
    md = "# Polly\n\nIntro text.\n\n## Integrations\n\n### Telegram\n\nWebhook setup details."
    chunks = chunk_markdown(md, max_chars=3000, overlap_chars=0)
    paths = [c.heading_path for c in chunks]
    assert any("Polly > Integrations > Telegram" in p for p in paths)
    tg = next(c for c in chunks if "Telegram" in c.heading_path)
    assert "Webhook setup details." in tg.text


def test_long_section_splits_with_overlap():
    paras = "\n\n".join(f"Paragraph {i}. " + "x" * 200 for i in range(30))
    md = f"# Doc\n\n{paras}"
    chunks = chunk_markdown(md, max_chars=1000, overlap_chars=100)
    assert len(chunks) > 3
    assert all(len(c.text) <= 1000 for c in chunks)
    # overlap: end of chunk N appears at start of chunk N+1
    assert chunks[1].text[:50] in chunks[0].text


def test_code_fence_never_split():
    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```"
    md = f"# Doc\n\nbefore\n\n{code}\n\nafter"
    chunks = chunk_markdown(md, max_chars=300, overlap_chars=0)
    fenced = [c for c in chunks if "```python" in c.text]
    assert len(fenced) == 1
    assert fenced[0].text.count("```") == 2  # open + close in same chunk


def test_positions_sequential():
    md = "# A\n\n" + "\n\n".join("p" * 400 for _ in range(10))
    chunks = chunk_markdown(md, max_chars=900, overlap_chars=0)
    assert [c.position for c in chunks] == list(range(len(chunks)))
