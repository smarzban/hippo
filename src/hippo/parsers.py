import re
from pathlib import Path

from markdownify import markdownify

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm"}


def parse_file(path: Path) -> tuple[str, str]:
    """Return (title, canonical_markdown). Raises ValueError on unsupported/broken files."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type: {suffix}")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"not a text file: {path.name}") from e
    return parse_content(path.stem, raw, suffix)


def parse_content(fallback_title: str, raw: str, suffix: str) -> tuple[str, str]:
    if suffix in (".html", ".htm"):
        md = markdownify(raw, heading_style="ATX").strip()
    else:
        md = raw.strip()
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    title = m.group(1).strip() if m else fallback_title
    return title, md
