import pydantic_ai.models
import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from hippo.agent import HubDeps, build_agent
from hippo.chunking import Chunk
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
pytestmark = pytest.mark.anyio


@pytest.fixture
def deps(tmp_path):
    store = Storage(connect(tmp_path / "t.db", embedding_dim=32), FakeEmbedder(dim=32))
    text = "polly connects to telegram via webhook callbacks registered in setup.py"
    store.upsert_document(
        source_type="folder", path="polly/telegram.md", title="Polly Telegram",
        content=f"# Polly Telegram\n\n{text}", content_hash="h",
        chunks=[Chunk(position=0, heading_path="Polly Telegram", text=text)],
        embed_inputs=[text],
    )
    return HubDeps(store=store, role="admin")


async def test_all_four_tools_registered(deps):
    agent = build_agent("openai:gpt-5.2")
    m = TestModel(call_tools=[])
    with agent.override(model=m):
        await agent.run("hello", deps=deps)
    tool_names = {t.name for t in m.last_model_request_parameters.function_tools}
    assert tool_names == {"search", "read_document", "list_documents", "grep"}


async def test_search_tool_returns_provenance(deps):
    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "telegram webhook"})])
        tool_return = messages[-1].parts[0]
        assert "polly/telegram.md" in str(tool_return.content)
        assert "Polly Telegram" in str(tool_return.content)
        return ModelResponse(parts=[TextPart("Answer with citation [polly/telegram.md]")])

    agent = build_agent("openai:gpt-5.2")
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("how does polly integrate with telegram?", deps=deps)
    assert "polly/telegram.md" in result.output


async def test_read_document_tool(deps):
    doc_id = deps.store.list_documents(role="admin")[0].id

    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_document", {"doc_id": doc_id})])
        content = str(messages[-1].parts[0].content)
        assert "registered in setup.py" in content
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent("openai:gpt-5.2")
    with agent.override(model=FunctionModel(script)):
        await agent.run("read it", deps=deps)


async def test_system_prompt_demands_citations_and_honesty():
    agent = build_agent("openai:gpt-5.2")
    sp = " ".join(agent._system_prompts)
    assert "cite" in sp.lower()
    assert "knowledge base" in sp.lower()


async def test_grep_invalid_regex_returns_error_dict(deps):
    """grep tool with an invalid regex pattern must return a list with an error dict
    instead of crashing the run."""

    def script(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            # "[" is an invalid regex pattern
            return ModelResponse(parts=[ToolCallPart("grep", {"pattern": "["})])
        tool_return = messages[-1].parts[0]
        content = str(tool_return.content)
        assert "error" in content.lower()
        return ModelResponse(parts=[TextPart("handled invalid regex")])

    agent = build_agent("openai:gpt-5.2")
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("find something", deps=deps)
    assert "handled invalid regex" in result.output


@pytest.fixture
def rbac_store(tmp_path):
    store = Storage(connect(tmp_path / "rbac.db", embedding_dim=32), FakeEmbedder(dim=32))
    team_sid = store.register_source("folder", "/r/team")
    mgr_sid = store.register_source("folder", "/r/mgr", access="managers")
    team_text = "public quarterly roadmap zebra"
    store.upsert_document(
        source_type="folder", path="team/a.md", title="Team Roadmap",
        content=f"# Team Roadmap\n\n{team_text}", content_hash="h1",
        chunks=[Chunk(position=0, heading_path="Team Roadmap", text=team_text)],
        embed_inputs=[team_text],
        source_id=team_sid,
    )
    mgr_text = "manager compensation zebra"
    store.upsert_document(
        source_type="folder", path="mgr/comp.md", title="Manager Comp",
        content=f"# Manager Comp\n\n{mgr_text}", content_hash="h2",
        chunks=[Chunk(position=0, heading_path="Manager Comp", text=mgr_text)],
        embed_inputs=[mgr_text],
        source_id=mgr_sid,
    )
    return store


async def test_agent_search_respects_role(rbac_store):
    """A developer's agent must not see manager-source chunks through any tool."""

    def call_search_then_answer(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "zebra", "top_k": 10})])
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent(FunctionModel(call_search_then_answer))
    result = await agent.run("q", deps=HubDeps(store=rbac_store, role="developer"))
    tool_returns = [
        p.content for m in result.all_messages() for p in m.parts
        if getattr(p, "part_kind", "") == "tool-return"
    ]
    flat = str(tool_returns)
    assert "mgr/comp.md" not in flat and "team/a.md" in flat
