import io
import zipfile

import pytest
from pathlib import Path

from hippo.parsers import parse_bytes, parse_file


def _minimal_docx(body_text: str) -> bytes:
    """A minimal but valid .docx (OOXML) with one paragraph — enough for mammoth."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_parse_docx_bytes():
    data = _minimal_docx("Hello from Word. This is the body.")
    title, md = parse_bytes("Report", data, ".docx")
    assert "Hello from Word" in md
    assert title == "Report"  # no H1 -> filename-stem fallback


def test_parse_docx_file(tmp_path):
    p = tmp_path / "Quarterly Plan.docx"
    p.write_bytes(_minimal_docx("revenue grew in Q3"))
    title, md = parse_file(p)
    assert "revenue grew" in md and title == "Quarterly Plan"


def test_text_paths_still_work(tmp_path):
    md_file = tmp_path / "n.md"
    md_file.write_text("# Real Title\n\nbody")
    title, md = parse_file(md_file)
    assert title == "Real Title" and "body" in md
    # html
    title2, md2 = parse_bytes("fallback", b"<h1>HTML Doc</h1><p>hi</p>", ".html")
    assert title2 == "HTML Doc" and "hi" in md2


def test_unsupported_suffix_raises():
    with pytest.raises(ValueError):
        parse_bytes("x", b"data", ".pdf")
