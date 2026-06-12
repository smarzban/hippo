"""Tests for mcp_server.py — role-filtered tools + FastMCP builder.

TDD: this file was written before mcp_server.py existed.
All storage is offline (FakeEmbedder + TestModel pattern); zero network.
"""
import asyncio

import pytest

from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


# ---------------------------------------------------------------------------
# Shared helpers (mirrored from tests/test_storage.py)
# ---------------------------------------------------------------------------

def _add_doc(store, path, text, source_id=None, title=None):
    return store.upsert_document(
        source_type="folder", path=path, title=title or path, content=text,
        content_hash=path + "h",
        chunks=[Chunk(position=0, heading_path=path, text=text)],
        embed_inputs=[text], source_id=source_id,
    )


def _rbac_store(tmp_path):
    """Build a role-filtered store with three docs:
    - team/a.md  → everyone source (visible to all roles)
    - mgr/comp.md → managers source (visible to manager/admin only)
    - upload/x.md → no source (uploads, visible to all)
    All three docs contain the word "zebra" so search finds them.
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    con = connect(db_dir / "t.db", embedding_dim=32)
    store = Storage(con, FakeEmbedder(dim=32))
    team = store.register_source("folder", "/r/team")
    mgr = store.register_source("folder", "/r/mgr", access="managers")
    _add_doc(store, "team/a.md", "public zebra roadmap", source_id=team)
    _add_doc(store, "mgr/comp.md", "manager zebra compensation", source_id=mgr)
    _add_doc(store, "upload/x.md", "uploaded zebra note", source_id=None)
    return store


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from hippo.mcp_server import (  # noqa: E402  (after helpers)
    _mcp_role,
    build_mcp_server,
    mcp_grep,
    mcp_list_documents,
    mcp_read_document,
    mcp_search,
)


# ---------------------------------------------------------------------------
# mcp_search
# ---------------------------------------------------------------------------

def test_mcp_search_role_filtered(tmp_path):
    store = _rbac_store(tmp_path)
    dev = {h["path"] for h in mcp_search(store, "developer", "zebra", 10)}
    assert "mgr/comp.md" not in dev
    assert "team/a.md" in dev

    mgr = {h["path"] for h in mcp_search(store, "manager", "zebra", 10)}
    assert "mgr/comp.md" in mgr


def test_mcp_search_returns_expected_keys(tmp_path):
    store = _rbac_store(tmp_path)
    hits = mcp_search(store, "admin", "zebra", 5)
    assert hits  # at least one result
    for h in hits:
        assert {"doc_id", "path", "title", "section", "text"} <= h.keys()


def test_mcp_search_top_k_clamped_to_one(tmp_path):
    """top_k <= 0 should not crash (clamped to 1)."""
    store = _rbac_store(tmp_path)
    hits = mcp_search(store, "admin", "zebra", 0)
    assert isinstance(hits, list)


# ---------------------------------------------------------------------------
# mcp_list_documents + mcp_read_document
# ---------------------------------------------------------------------------

def test_mcp_list_get_grep_role_filtered(tmp_path):
    store = _rbac_store(tmp_path)

    # list: developer cannot see managers source
    dev_paths = {d["path"] for d in mcp_list_documents(store, "developer")}
    assert "mgr/comp.md" not in dev_paths
    assert "team/a.md" in dev_paths

    # admin sees everything; get the mgr doc id
    mgr_id = next(
        d["doc_id"]
        for d in mcp_list_documents(store, "admin")
        if d["path"] == "mgr/comp.md"
    )

    # read: developer cannot read a managers-only doc (returns error dict)
    result = mcp_read_document(store, "developer", mgr_id)
    assert "error" in result

    # read: manager CAN read it
    result = mcp_read_document(store, "manager", mgr_id)
    assert result["path"] == "mgr/comp.md"

    # grep: developer never sees mgr/comp.md
    assert all(h.get("path") != "mgr/comp.md" for h in mcp_grep(store, "developer", "compensation"))

    # grep: admin sees it
    assert any(h.get("path") == "mgr/comp.md" for h in mcp_grep(store, "admin", "compensation"))


def test_mcp_read_document_unknown_id(tmp_path):
    store = _rbac_store(tmp_path)
    result = mcp_read_document(store, "admin", 99999)
    assert "error" in result


def test_mcp_list_documents_returns_expected_keys(tmp_path):
    store = _rbac_store(tmp_path)
    docs = mcp_list_documents(store, "admin")
    assert docs
    for d in docs:
        assert {"doc_id", "path", "title", "summary"} <= d.keys()


# ---------------------------------------------------------------------------
# mcp_grep
# ---------------------------------------------------------------------------

def test_mcp_grep_invalid_regex_returns_error_dict(tmp_path):
    store = _rbac_store(tmp_path)
    result = mcp_grep(store, "admin", "[invalid")
    assert len(result) == 1 and "error" in result[0]


# ---------------------------------------------------------------------------
# build_mcp_server
# ---------------------------------------------------------------------------

def test_build_mcp_server_registers_four_tools(tmp_path):
    store = _rbac_store(tmp_path)
    server = build_mcp_server(store, require_auth=True)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"search", "read_document", "list_documents", "grep"}


# ---------------------------------------------------------------------------
# _mcp_role contextvar
# ---------------------------------------------------------------------------

def test_role_contextvar_default_and_require_auth(tmp_path):
    """Setting the contextvar routes through correctly; resetting clears it."""
    store = _rbac_store(tmp_path)
    token = _mcp_role.set("developer")
    try:
        # The contextvar is set to developer; use it explicitly in mcp_search
        result = mcp_search(store, _mcp_role.get(), "zebra", 10)
        assert "team/a.md" in {h["path"] for h in result}
    finally:
        _mcp_role.reset(token)

    # After reset, default is None
    assert _mcp_role.get() is None
