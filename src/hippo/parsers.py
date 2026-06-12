import io
import re
import zipfile
from pathlib import Path

from markdownify import markdownify

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".docx"}

# ZIP-bomb guard: reject .docx files whose total uncompressed size exceeds this
# (checked via central-directory declared sizes, before any decompression).
DOCX_MAX_DECOMPRESSED_BYTES = 100_000_000  # 100 MB total uncompressed
DOCX_MAX_ENTRIES = 2000                    # sane cap on zip member count


def parse_file(path: Path) -> tuple[str, str]:
    """Return (title, canonical_markdown). Raises ValueError on unsupported/broken files."""
    return parse_bytes(path.stem, path.read_bytes(), path.suffix.lower())


def parse_bytes(fallback_title: str, data: bytes, suffix: str,
                *, max_decompressed: int | None = None) -> tuple[str, str]:
    """Parse raw bytes of a supported file into (title, canonical markdown).

    max_decompressed: override the module-level DOCX_MAX_DECOMPRESSED_BYTES cap
    (used in tests and by Ingestor when settings.max_decompressed_bytes is set).
    """
    suffix = suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type: {suffix}")
    if suffix == ".docx":
        md = _docx_to_markdown(data, max_decompressed or DOCX_MAX_DECOMPRESSED_BYTES)
        m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
        return (m.group(1).strip() if m else fallback_title), md
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"not a text file: {fallback_title}") from e
    return parse_content(fallback_title, text, suffix)


def _docx_to_markdown(data: bytes, max_decompressed: int) -> str:
    """Convert .docx bytes to markdown, with ZIP-bomb and XXE defences."""
    import mammoth

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("invalid .docx file (not a zip archive)") from e

    infos = zf.infolist()
    if len(infos) > DOCX_MAX_ENTRIES:
        raise ValueError(
            f"docx has too many entries ({len(infos)} > {DOCX_MAX_ENTRIES})")

    total = sum(i.file_size for i in infos)  # declared uncompressed size (central dir)
    if total > max_decompressed:
        raise ValueError(
            f"docx decompresses to {total} bytes, exceeds limit {max_decompressed}")

    # XXE / entity defence-in-depth: reject DTD/entity declarations in XML parts
    # (bounded read — total uncompressed already capped above).
    for info in infos:
        if info.filename.lower().endswith((".xml", ".rels")):
            head = zf.read(info.filename)[:65536]
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise ValueError(
                    "docx contains a DTD/entity declaration (rejected)")

    try:
        result = mammoth.convert_to_html(
            io.BytesIO(data),
            convert_image=mammoth.images.img_element(lambda image: {}),
        )
    except ValueError:
        raise
    except Exception as e:  # normalize any mammoth/zip parse failure
        raise ValueError(f"could not parse .docx: {e}") from e

    return markdownify(result.value, heading_style="ATX").strip()


def parse_content(fallback_title: str, raw: str, suffix: str) -> tuple[str, str]:
    if suffix in (".html", ".htm"):
        md = markdownify(raw, heading_style="ATX").strip()
    else:
        md = raw.strip()
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    title = m.group(1).strip() if m else fallback_title
    return title, md
