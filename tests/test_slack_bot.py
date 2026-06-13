import pydantic_ai.models
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.messages import ModelResponse as MR, TextPart as TP
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hippo.agent import build_agent
from hippo.config import Settings
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.slack_bot import HISTORY_TURNS, build_history, format_answer, surface_role, answer_question
from hippo.storage import Storage

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

pytestmark = pytest.mark.anyio

BOT = "UBOT"


def _store(tmp_path):
    con = connect(tmp_path / "h.db", embedding_dim=8)
    return Storage(con, FakeEmbedder(dim=8))


def test_surface_role_dm_keeps_full_role():
    assert surface_role("manager", is_dm=True) == "manager"
    assert surface_role("admin", is_dm=True) == "admin"
    assert surface_role("developer", is_dm=True) == "developer"


def test_surface_role_channel_forces_developer():
    # Public surface: only everyone-access docs, regardless of asker's role.
    assert surface_role("manager", is_dm=False) == "developer"
    assert surface_role("admin", is_dm=False) == "developer"
    assert surface_role("developer", is_dm=False) == "developer"


def test_format_answer_passthrough():
    assert format_answer("Short answer: yes [docs/x.md > Setup]") == \
        "Short answer: yes [docs/x.md > Setup]"


def test_format_answer_blank_falls_back():
    assert format_answer("") == "I couldn't find an answer to that in the knowledge base."
    assert format_answer("   ") == "I couldn't find an answer to that in the knowledge base."


def test_build_history_maps_user_and_bot_turns():
    prior = [
        {"user": "UALICE", "text": "<@UBOT> how do webhooks work?"},
        {"user": BOT, "bot_id": "B1", "text": "They POST to your endpoint [docs/x.md > Hooks]"},
    ]
    history = build_history(prior, bot_user_id=BOT)
    assert len(history) == 2
    assert isinstance(history[0], ModelRequest)
    assert isinstance(history[0].parts[0], UserPromptPart)
    assert history[0].parts[0].content == "how do webhooks work?"  # mention stripped
    assert isinstance(history[1], ModelResponse)
    assert isinstance(history[1].parts[0], TextPart)
    assert history[1].parts[0].content.startswith("They POST")


def test_build_history_skips_blank_and_bounds_window():
    prior = [{"user": "U", "text": ""}]  # blank skipped
    prior += [{"user": "U", "text": f"q{i}"} for i in range(HISTORY_TURNS + 5)]
    history = build_history(prior, bot_user_id=BOT)
    assert len(history) == HISTORY_TURNS  # bounded
    # newest retained, oldest dropped
    assert history[-1].parts[0].content == f"q{HISTORY_TURNS + 4}"


async def test_answer_question_returns_agent_output(tmp_path):
    def reply(messages: list[ModelMessage], info: AgentInfo) -> MR:
        return MR(parts=[TP(content="Here is the answer [docs/x.md > S]")])

    agent = build_agent(FunctionModel(reply))
    store = _store(tmp_path)
    out = await answer_question(
        agent, store, Settings(), question="hi", role="developer", history=[]
    )
    assert out == "Here is the answer [docs/x.md > S]"


async def test_answer_question_friendly_on_error(tmp_path):
    def boom(messages, info):
        raise RuntimeError("model exploded")

    agent = build_agent(FunctionModel(boom))
    store = _store(tmp_path)
    out = await answer_question(
        agent, store, Settings(), question="hi", role="developer", history=[]
    )
    assert "error" in out.lower()  # friendly, not a stack trace


# ---------------------------------------------------------------------------
# handle_event tests (fake Slack client)
# ---------------------------------------------------------------------------
from hippo.slack_bot import handle_event


class FakeSlack:
    """Records calls; returns canned payloads. No network."""
    def __init__(self, email="dev@superbalist.com", replies=None):
        self._email = email
        self._replies = replies or []
        self.posted = []      # chat_postMessage kwargs
        self.updated = []     # chat_update kwargs

    async def users_info(self, *, user):
        if self._email is None:
            return {"user": {"profile": {}}}
        return {"user": {"profile": {"email": self._email}}}

    async def conversations_replies(self, *, channel, ts, **kw):
        return {"messages": self._replies}

    async def conversations_history(self, *, channel, **kw):
        return {"messages": list(reversed(self._replies))}  # API returns newest-first

    async def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": "111.222"}

    async def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}


def _fixed_agent(text="Answer [docs/x.md > S]"):
    def reply(messages, info):
        return MR(parts=[TP(content=text)])
    return build_agent(FunctionModel(reply))


async def test_handle_channel_mention_posts_then_updates_in_thread(tmp_path):
    client = FakeSlack()
    await handle_event(
        {"user": "UALICE", "channel": "C1", "ts": "100.0", "text": "<@UBOT> hi"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(), bot_user_id="UBOT", is_dm=False,
    )
    assert client.posted and client.posted[0]["thread_ts"] == "100.0"   # reply in thread
    assert client.updated and client.updated[0]["text"].startswith("Answer")
    assert client.updated[0]["ts"] == "111.222"                          # updated the placeholder


async def test_handle_dm_is_flat_no_thread(tmp_path):
    client = FakeSlack()
    await handle_event(
        {"user": "UALICE", "channel": "D1", "ts": "100.0", "text": "hi"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(), bot_user_id="UBOT", is_dm=True,
    )
    assert client.posted[0].get("thread_ts") is None   # flat
    assert client.updated[0]["text"].startswith("Answer")


async def test_handle_out_of_domain_is_refused(tmp_path):
    client = FakeSlack(email="outsider@gmail.com")
    await handle_event(
        {"user": "UX", "channel": "D1", "ts": "1.0", "text": "hi"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(allowed_domain="superbalist.com"),
        bot_user_id="UBOT", is_dm=True,
    )
    assert "don't have access" in client.posted[0]["text"].lower()
    assert not client.updated   # never ran the agent


async def test_handle_ignores_bot_messages(tmp_path):
    client = FakeSlack()
    await handle_event(
        {"user": "UBOT", "channel": "C1", "ts": "1.0", "text": "loop?", "bot_id": "B1"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(), bot_user_id="UBOT", is_dm=False,
    )
    assert not client.posted and not client.updated   # no self-reply


# ---------------------------------------------------------------------------
# RBAC end-to-end: manager DM sees manager doc, channel does not
# ---------------------------------------------------------------------------
from hippo.chunking import Chunk


def _rbac_store(tmp_path):
    """Store with one everyone doc and one managers-only doc."""
    con = connect(tmp_path / "h.db", embedding_dim=8)
    store = Storage(con, FakeEmbedder(dim=8))
    everyone_sid = store.register_source("folder", "/e", access="everyone")
    mgr_sid = store.register_source("folder", "/m", access="managers")
    store.upsert_document(
        source_type="folder", path="/e/pub.md", title="Public",
        content="public onboarding info", content_hash="h1",
        chunks=[Chunk(position=0, heading_path="Public", text="public onboarding info")],
        embed_inputs=["public onboarding info"],
        source_id=everyone_sid,
    )
    store.upsert_document(
        source_type="folder", path="/m/sal.md", title="Salaries",
        content="secret salary bands", content_hash="h2",
        chunks=[Chunk(position=0, heading_path="Salaries", text="secret salary bands")],
        embed_inputs=["secret salary bands"],
        source_id=mgr_sid,
    )
    return store


async def test_manager_dm_sees_manager_doc_channel_does_not(tmp_path):
    store = _rbac_store(tmp_path)
    # Oracle via list_documents visibility:
    assert any(d.title == "Salaries" for d in store.list_documents(role="manager"))
    assert not any(d.title == "Salaries" for d in store.list_documents(role="developer"))
    # surface_role is what handle_event passes to the agent:
    assert surface_role("manager", is_dm=True) == "manager"      # DM: sees Salaries
    assert surface_role("manager", is_dm=False) == "developer"   # channel: does not


# ---------------------------------------------------------------------------
# build_slack_app construction smoke (offline) — guards against a bad Bolt
# kwarg / API drift that handle_event tests (fake client) never exercise.
# ---------------------------------------------------------------------------
from hippo.slack_bot import build_slack_app


def test_build_slack_app_constructs_offline_with_two_handlers(tmp_path):
    store = _store(tmp_path)
    settings = Settings(_env_file=None, slack_bot_token="xoxb-fake", slack_app_token="xapp-fake")
    app = build_slack_app(store, _fixed_agent(), settings)
    # Constructed without network or signing secret, with both event handlers wired.
    assert len(app._async_listeners) == 2
