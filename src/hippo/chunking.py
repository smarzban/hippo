import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    position: int
    heading_path: str
    text: str


def _blocks(md: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, text) blocks: 'heading', 'code', 'para'.

    Code fences are kept as single atomic blocks.
    """
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush():
        nonlocal para
        text = "\n".join(para).strip()
        if text:
            blocks.append(("para", text))
        para = []

    while i < len(lines):
        line = lines[i]
        if FENCE_RE.match(line.strip()):
            flush()
            fence = [line]
            i += 1
            while i < len(lines):
                fence.append(lines[i])
                if FENCE_RE.match(lines[i].strip()):
                    break
                i += 1
            blocks.append(("code", "\n".join(fence)))
        elif HEADING_RE.match(line):
            flush()
            blocks.append(("heading", line))
        elif not line.strip():
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return blocks


def chunk_markdown(md: str, max_chars: int = 3000, overlap_chars: int = 200) -> list[Chunk]:
    """Heading-aware packing: blocks accumulate into chunks <= max_chars.

    Headings update the heading path; a chunk never mixes content from two
    heading sections. Code fences are atomic (oversized ones become their own
    chunk even if > max_chars... but we cap to a single block). Overlap copies
    the tail of the previous chunk into the next within the same section.
    """
    heading_stack: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_path = ""

    def path() -> str:
        return " > ".join(h for _, h in heading_stack)

    def flush():
        nonlocal buf
        text = "\n\n".join(buf).strip()
        if text:
            chunks.append(Chunk(position=len(chunks), heading_path=buf_path, text=text))
        buf = []

    for kind, text in _blocks(md):
        if kind == "heading":
            flush()
            m = HEADING_RE.match(text)
            assert m
            level, title = len(m.group(1)), m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            continue

        current = "\n\n".join(buf)
        if buf and len(current) + len(text) + 2 > max_chars:
            tail = current[-overlap_chars:] if overlap_chars else ""
            flush()
            if tail and len(tail) + 2 + len(text) <= max_chars:
                buf = [tail]
        buf_path = path()
        buf.append(text)

        # an atomic block alone may exceed max; emit it solo
        if len("\n\n".join(buf)) > max_chars and len(buf) == 1:
            flush()

    flush()
    return chunks
