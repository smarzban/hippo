"""Expose Hippo's retrieval tools over MCP (Model Context Protocol) so external
agents (Claude Code etc.) can query the knowledge base. The four tools mirror the
in-app agent's tools but call Storage directly — no LLM — so they stay role-filtered.
Returned document text is framed as ⟦untrusted document data⟧…⟦end⟧ (the same
prompt-injection boundary the in-app agent applies, via tool_io.as_untrusted_data)
so a well-behaved external client treats it as evidence, not instructions. (Note:
`search` does embed the query, so this surface is not literally zero-network.) The
caller's role comes from their bearer token (set per request by the HTTP mount's
auth middleware in api.py) via the _mcp_role contextvar."""

from contextvars import ContextVar
from functools import partial

import anyio

from .storage import Storage
from .tool_io import as_untrusted_data, clamp_top_k

# Per-request role, set by the /mcp bearer-auth middleware (api.py) or the stdio
# entrypoint (cli.py). None until set.
_mcp_role: ContextVar[str | None] = ContextVar("hippo_mcp_role", default=None)


def mcp_search(store: Storage, role: str, query: str, top_k: int = 8) -> list[dict]:
    hits = store.search_hybrid(query, top_k=clamp_top_k(top_k), role=role)
    return [
        {
            "doc_id": h.document_id,
            "path": h.path,
            "title": h.title,
            "section": h.heading_path,
            "text": as_untrusted_data(h.text),
        }
        for h in hits
    ]


def mcp_read_document(store: Storage, role: str, doc_id: int) -> dict:
    doc = store.get_document(doc_id, role=role)
    if doc is None:
        return {"error": f"no document with id {doc_id}"}
    return {"doc_id": doc.id, "path": doc.path, "title": doc.title,
            "content": as_untrusted_data(doc.content)}


def mcp_list_documents(store: Storage, role: str, query: str | None = None) -> list[dict]:
    # path/title stay raw (citation identifiers); the free-text summary is
    # document-derived, so frame it as untrusted data like search/read/grep.
    # list_document_meta avoids materializing every doc's full content (MED-17).
    return [
        {"doc_id": d.id, "path": d.path, "title": d.title,
         "summary": as_untrusted_data(d.summary) if d.summary else ""}
        for d in store.list_document_meta(query=query, role=role)
    ]


def mcp_grep(store: Storage, role: str, pattern: str) -> list[dict]:
    try:
        hits = store.grep(pattern, role=role)
    except ValueError as e:
        return [{"error": str(e)}]
    return [
        {"doc_id": h.document_id, "path": h.path, "section": h.heading_path,
         "text": as_untrusted_data(h.text)}
        for h in hits
    ]


def build_mcp_server(store: Storage, *, require_auth: bool):
    """Build a FastMCP server exposing the four tools. require_auth=True (HTTP mount)
    means a missing role is a hard error (the middleware must have set it); False
    (stdio/local) defaults to the single local owner user."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "hippo",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        # DNS-rebinding protection guards browser-to-localhost attacks; it rejects
        # any Host the server wasn't told about, which would 421 every real call
        # behind a reverse proxy / custom domain (hippo.superbalist.com, the
        # container, tests). Hippo's own bearer-token gate authenticates every
        # /mcp request — no browser holds a token — so the rebinding defense is
        # redundant here. Disable it so MCP works behind any host.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def role() -> str:
        r = _mcp_role.get()
        if r is not None:
            return r
        if require_auth:
            raise PermissionError("no authenticated role for this MCP request")
        return "owner"

    # FastMCP invokes sync tool functions DIRECTLY on the event loop (no threadpool),
    # so a blocking call inside one stalls the single MCP session manager's loop for
    # every concurrent client. search embeds the query (network) and grep is a CPU-bound
    # regex scan — both block. Make the tools async and offload the blocking Storage call
    # to a worker thread (mirroring how pydantic-ai offloads the in-app agent's tools),
    # keeping the loop responsive (MED-05). role() is read on the loop thread BEFORE the
    # hop and passed in (anyio copies contextvars, but capturing it is explicit/clear).
    @mcp.tool()
    async def search(query: str, top_k: int = 8) -> list[dict]:
        """Hybrid keyword+semantic search over the team knowledge base. Returns
        chunks with provenance (path, title, section). Use this first."""
        return await anyio.to_thread.run_sync(partial(mcp_search, store, role(), query, top_k))

    @mcp.tool()
    async def read_document(doc_id: int) -> dict:
        """Read a full document by id (ids come from search/list_documents)."""
        return await anyio.to_thread.run_sync(partial(mcp_read_document, store, role(), doc_id))

    @mcp.tool()
    async def list_documents(query: str | None = None) -> list[dict]:
        """Browse indexed documents (titles + summaries), optionally filtered."""
        return await anyio.to_thread.run_sync(partial(mcp_list_documents, store, role(), query))

    @mcp.tool()
    async def grep(pattern: str) -> list[dict]:
        """Exact regex scan over raw document text (case-insensitive) for
        identifiers/codenames fuzzy search might miss."""
        return await anyio.to_thread.run_sync(partial(mcp_grep, store, role(), pattern))

    return mcp
