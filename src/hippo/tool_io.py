"""Shared input/output hygiene for the retrieval tools that Hippo exposes on TWO
surfaces — the in-app pydantic-ai agent (agent.py) and the MCP server (mcp_server.py).

Keeping this in one place guarantees both surfaces apply the identical
prompt-injection boundary and the identical argument clamping, so an external MCP
client gets the same defensive framing the in-app agent does. This module imports
nothing from the rest of hippo (no pydantic-ai), so mcp_server.py can use it
without pulling in the agent.
"""

# The exact sentinel the UI keys on to flag an ungrounded answer (see
# ui/src/citations.ts stripNoSourcesMarker). Document text must never be able to
# reproduce this byte-for-byte, or a quoted document could suppress the
# "no sources cited" warning on a genuinely ungrounded reply.
NO_SOURCES_MARKER = "<!--hippo:no-sources-->"

# Upper bound on a tool-supplied top_k. A model talked into (via a prompt-injected
# document, or simple confusion) passing top_k=1_000_000 would otherwise drive an
# oversized KNN/FTS limit and a large result materialization. The tool-call budget
# bounds the NUMBER of calls, not the cost of one call — so we bound it here.
MAX_TOP_K = 50


def clamp_top_k(top_k: int) -> int:
    """Clamp a tool-supplied top_k into [1, MAX_TOP_K]. Lower clamp keeps a
    zero/negative value from returning nothing; upper clamp caps the blast radius
    of an absurdly large value."""
    return max(1, min(int(top_k), MAX_TOP_K))


def as_untrusted_data(text: str) -> str:
    """Frame document text as untrusted data so a downstream model can't be hijacked
    by instructions embedded in documents (prompt-injection mitigation).

    Two sanitizations on the body before framing:
    - The marker glyphs ⟦ ⟧ are replaced with [ ] so a document cannot forge a
      closing ⟦end⟧ and smuggle text outside the envelope.
    - The no-sources sentinel is broken up so quoted document text cannot reproduce
      the exact sequence the UI uses to suppress its ungrounded-answer warning.
    """
    body = text.replace("⟦", "[").replace("⟧", "]")
    body = body.replace(NO_SOURCES_MARKER, "<!-- hippo:no-sources -->")
    return f"⟦untrusted document data⟧\n{body}\n⟦end⟧"


# --- result shaping (shared by the in-app agent tools and the MCP server) ----------
# Single-source the provenance dict shapes so the two surfaces can't drift. path/title
# stay raw (citation identifiers the model echoes); free-text fields are framed.

def shape_search_hit(h) -> dict:
    return {"doc_id": h.document_id, "path": h.path, "title": h.title,
            "section": h.heading_path, "text": as_untrusted_data(h.text)}


def shape_grep_hit(h) -> dict:
    return {"doc_id": h.document_id, "path": h.path, "section": h.heading_path,
            "text": as_untrusted_data(h.text)}


def shape_doc_meta(d) -> dict:
    return {"doc_id": d.id, "path": d.path, "title": d.title,
            "summary": as_untrusted_data(d.summary) if d.summary else ""}


def shape_doc_full(d) -> dict:
    return {"doc_id": d.id, "path": d.path, "title": d.title,
            "content": as_untrusted_data(d.content)}
