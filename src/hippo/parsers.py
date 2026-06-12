import io
import re
from pathlib import Path

from markdownify import markdownify

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".docx"}


def parse_file(path: Path) -> tuple[str, str]:
    """Return (title, canonical_markdown). Raises ValueError on unsupported/broken files."""
    return parse_bytes(path.stem, path.read_bytes(), path.suffix.lower())


def parse_bytes(fallback_title: str, data: bytes, suffix: str) -> tuple[str, str]:
    """Parse raw bytes of a supported file into (title, canonical markdown)."""
    suffix = suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type: {suffix}")
    if suffix == ".docx":
        import mammoth
        html = mammoth.convert_to_html(io.BytesIO(data)).value
        md = markdownify(html, heading_style="ATX").strip()
        m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
        return (m.group(1).strip() if m else fallback_title), md
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"not a text file: {fallback_title}") from e
    return parse_content(fallback_title, text, suffix)


def parse_content(fallback_title: str, raw: str, suffix: str) -> tuple[str, str]:
    if suffix in (".html", ".htm"):
        md = markdownify(raw, heading_style="ATX").strip()
    else:
        md = raw.strip()
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    title = m.group(1).strip() if m else fallback_title
    return title, md
