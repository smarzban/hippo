# Slack Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roadmap item 7 — a read-only Slack Q&A bot that answers from the indexed docs with citations and role-filtering, reusing the existing agent. Design: `../specs/2026-06-13-slack-integration-design.md`.

**Architecture:** A standalone `hippo slack` process runs Slack Bolt in **Socket Mode** (outbound WebSocket — no inbound traffic crosses IAP, no public endpoint, no signing secret). Inbound messages resolve the Slack user → work email → role (shared `auth.resolve_role`), pick a surface-appropriate role (DM = full role, channel @mention = `everyone`-only), reconstruct thread/DM history as pydantic-ai `message_history`, run the agent to a final string, and post it (placeholder → `chat.update`). The Slack-facing logic is split into **pure functions** (unit-tested, zero-network) plus a thin `handle_event` adapter tested with a **fake Slack client**; the `AsyncSocketModeHandler` runner is thin and not unit-tested (same posture as the `hippo mcp` stdio runner).

**Tech Stack:** `slack-bolt` (async, Socket Mode) + `aiohttp`; pydantic-ai (`ModelRequest`/`ModelResponse` history); existing `Storage`/agent/`Settings`.

**Hard rules:** tests zero-network (`FakeEmbedder` + `TestModel`/`FunctionModel`, `ALLOW_MODEL_REQUESTS=False`); no SQL outside `storage.py`; retrieval keeps keyword-only `role`; TDD; commit per green step.

---

### Task 1: Extract shared `resolve_role` into `auth.py`

Today the domain-check + role-resolution + admin-bootstrap logic lives only in the `_email_to_role` closure inside `api.py:build_app`. The Slack bot (a separate process) needs the same logic, so extract it to a module-level function with one source of truth.

**Files:**
- Modify: `src/hippo/auth.py` (add `resolve_role`)
- Modify: `src/hippo/api.py:149-157` (`_email_to_role` delegates to it)
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
import sqlite3
import pytest
from hippo.auth import AuthError, resolve_role
from hippo.config import Settings
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


def _store(tmp_path):
    con = connect(tmp_path / "h.db", embedding_dim=8)
    return Storage(con, FakeEmbedder(dim=8))


def test_resolve_role_first_timer_is_developer(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com")
    assert resolve_role(store, settings, "new.person@example.com") == "developer"


def test_resolve_role_admin_bootstrap_wins(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com", admin_emails="boss@example.com")
    assert resolve_role(store, settings, "Boss@Example.com") == "admin"


def test_resolve_role_out_of_domain_raises(tmp_path):
    store = _store(tmp_path)
    settings = Settings(allowed_domain="example.com")
    with pytest.raises(AuthError):
        resolve_role(store, settings, "outsider@gmail.com")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auth.py::test_resolve_role_first_timer_is_developer -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_role'`.

- [ ] **Step 3: Implement `resolve_role` in `auth.py`**

At the top of `src/hippo/auth.py`, under the existing imports add a typing guard (avoids any import cycle; `auth.py` must stay import-light):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Storage
```

Then add at module level:

```python
def resolve_role(store: "Storage", settings: "Settings", email: str) -> str:
    """Canonical identity → role: normalize, enforce the domain gate, ensure the
    user row (first-timers default to 'developer'), then apply the admin-email
    bootstrap. Raises AuthError if the email is out of the allowed domain. Shared
    by the HTTP bearer path (api.py) and the Slack bot."""
    email = email.strip().lower()
    check_domain(email, settings.allowed_domain)  # raises AuthError
    role = store.ensure_user(email)
    if email in settings.admin_email_list:
        role = "admin"  # env bootstrap always wins (spec §1)
    return role
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auth.py -v`
Expected: PASS.

- [ ] **Step 5: Refactor `api.py` to delegate (no behavior change)**

In `src/hippo/api.py`, replace the body of the `_email_to_role` closure (lines ~149-157) so it calls the shared function — keeping the closure name so the rest of `build_app` is untouched:

```python
    from .auth import resolve_role

    def _email_to_role(email: str) -> str:
        """Canonical domain-check + role resolution. Raises AuthError on domain failure.
        Used by both the HTTP bearer path (_user_for) and the MCP ASGI middleware."""
        return resolve_role(store, settings, email)
```

(Move the `from .auth import ...` to join the existing top-level `auth` import line instead of a local import if cleaner — `from .auth import AuthError, AuthenticatedUser, IapVerifier, check_domain, resolve_role, validate_google_id_token`.)

- [ ] **Step 6: Run the full auth/api suite to verify no regression**

Run: `uv run pytest tests/test_auth.py tests/test_api_auth.py -v`
Expected: PASS (existing behavior preserved; admin bootstrap, domain gate, first-timer default all unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/hippo/auth.py src/hippo/api.py tests/test_auth.py
git commit -m "refactor: extract shared auth.resolve_role (single source of truth)"
```

---

### Task 2: Config knobs + dependency declaration

**Files:**
- Modify: `src/hippo/config.py:44` (add three settings after `mcp_enabled`)
- Modify: `pyproject.toml` (declare `slack-bolt`, `aiohttp`)
- Test: `tests/test_config.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
from hippo.config import Settings


def test_slack_settings_defaults_off():
    s = Settings()
    assert s.slack_enabled is False
    assert s.slack_bot_token == ""
    assert s.slack_app_token == ""


def test_slack_settings_from_env(monkeypatch):
    monkeypatch.setenv("HIPPO_SLACK_ENABLED", "true")
    monkeypatch.setenv("HIPPO_SLACK_BOT_TOKEN", "xoxb-abc")
    monkeypatch.setenv("HIPPO_SLACK_APP_TOKEN", "xapp-xyz")
    s = Settings()
    assert s.slack_enabled is True
    assert s.slack_bot_token == "xoxb-abc"
    assert s.slack_app_token == "xapp-xyz"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'slack_enabled'`.

- [ ] **Step 3: Add the settings**

In `src/hippo/config.py`, immediately after the `mcp_enabled` line (line 44):

```python
    # --- slack bot (spec: 2026-06-13-slack-integration) ---
    slack_enabled: bool = False  # `hippo slack` refuses to start unless true
    slack_bot_token: str = ""    # xoxb-… bot token
    slack_app_token: str = ""    # xapp-… app-level token (Socket Mode)
```

- [ ] **Step 4: Declare the dependency**

In `pyproject.toml`, add to the `[project] dependencies` list:

```toml
    "slack-bolt>=1.18",
    "aiohttp>=3.9",
```

Then run `uv sync` to install.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hippo/config.py pyproject.toml uv.lock tests/test_config.py
git commit -m "feat: slack config (HIPPO_SLACK_*) + slack-bolt/aiohttp deps"
```

---

### Task 3: Pure helpers — `surface_role` and `format_answer`

**Files:**
- Create: `src/hippo/slack_bot.py`
- Test: `tests/test_slack_bot.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_slack_bot.py`:

```python
from hippo.slack_bot import format_answer, surface_role


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_slack_bot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hippo.slack_bot'`.

- [ ] **Step 3: Implement the helpers**

Create `src/hippo/slack_bot.py`:

```python
"""Slack front door onto the Hippo agent (roadmap item 7). Read-only Q&A over
DM and channel @mention, role-filtered, Socket Mode. The Slack-facing logic is
split into pure functions (unit-tested) plus a thin handle_event adapter tested
with a fake client; the AsyncSocketModeHandler runner lives in cli.py.

Design: docs/superpowers/specs/2026-06-13-slack-integration-design.md
"""

_BLANK_ANSWER = "I couldn't find an answer to that in the knowledge base."


def surface_role(resolved_role: str, *, is_dm: bool) -> str:
    """Pick the role passed to the agent based on the Slack surface. DMs are
    private, so the asker's full role applies. A channel @mention has an audience,
    so force the 'everyone'-access view (developer) regardless of who asks —
    fails closed, never leaks manager-only docs into a public channel (spec §4)."""
    return resolved_role if is_dm else "developer"


def format_answer(text: str) -> str:
    """Final agent text → Slack message body. Citations stay as literal
    [path > section] text. Guards the empty-output case (e.g. the gpt-oss
    empty-content quirk) so the bot always says something."""
    return text.strip() if text and text.strip() else _BLANK_ANSWER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_slack_bot.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/hippo/slack_bot.py tests/test_slack_bot.py
git commit -m "feat: slack_bot surface_role + format_answer helpers"
```

---

### Task 4: `build_history` — Slack messages → pydantic-ai `message_history`

**Files:**
- Modify: `src/hippo/slack_bot.py`
- Test: `tests/test_slack_bot.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_slack_bot.py`:

```python
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from hippo.slack_bot import HISTORY_TURNS, build_history

BOT = "UBOT"


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_slack_bot.py -k build_history -v`
Expected: FAIL — `ImportError: cannot import name 'HISTORY_TURNS'`.

- [ ] **Step 3: Implement `build_history` + mention stripping**

Add to `src/hippo/slack_bot.py` (add `import re` at top):

```python
import re

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

HISTORY_TURNS = 10  # cap reconstructed turns (token budget on Ollama Cloud)

_MENTION = re.compile(r"<@[^>]+>")


def strip_mention(text: str) -> str:
    """Remove Slack user-mention tokens (<@U123>) and trim — leaves the question."""
    return _MENTION.sub("", text or "").strip()


def _is_bot(msg: dict, bot_user_id: str) -> bool:
    return bool(msg.get("bot_id")) or msg.get("user") == bot_user_id


def build_history(prior: list[dict], *, bot_user_id: str) -> list[ModelMessage]:
    """Map prior Slack messages (chronological, excluding the current one) to a
    pydantic-ai message history: the bot's own messages become ModelResponse
    (assistant) turns, everyone else's become ModelRequest (user) turns. Blank
    messages are skipped; the list is bounded to the most recent HISTORY_TURNS."""
    out: list[ModelMessage] = []
    for msg in prior:
        text = strip_mention(msg.get("text", ""))
        if not text:
            continue
        if _is_bot(msg, bot_user_id):
            out.append(ModelResponse(parts=[TextPart(content=text)]))
        else:
            out.append(ModelRequest(parts=[UserPromptPart(content=text)]))
    return out[-HISTORY_TURNS:]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_slack_bot.py -k build_history -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/slack_bot.py tests/test_slack_bot.py
git commit -m "feat: slack_bot build_history (Slack turns -> pydantic-ai message_history)"
```

---

### Task 5: `answer_question` — run the agent to a final string

**Files:**
- Modify: `src/hippo/slack_bot.py`
- Test: `tests/test_slack_bot.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_slack_bot.py`:

```python
import pydantic_ai.models
import pytest
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelMessage, ModelResponse as MR, TextPart as TP

from hippo.agent import build_agent
from hippo.config import Settings
from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.slack_bot import answer_question
from hippo.storage import Storage

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


def _store(tmp_path):
    con = connect(tmp_path / "h.db", embedding_dim=8)
    return Storage(con, FakeEmbedder(dim=8))


@pytest.mark.anyio
async def test_answer_question_returns_agent_output(tmp_path):
    def reply(messages: list[ModelMessage], info: AgentInfo) -> MR:
        return MR(parts=[TP(content="Here is the answer [docs/x.md > S]")])

    agent = build_agent(FunctionModel(reply))
    store = _store(tmp_path)
    out = await answer_question(
        agent, store, Settings(), question="hi", role="developer", history=[]
    )
    assert out == "Here is the answer [docs/x.md > S]"


@pytest.mark.anyio
async def test_answer_question_friendly_on_error(tmp_path):
    def boom(messages, info):
        raise RuntimeError("model exploded")

    agent = build_agent(FunctionModel(boom))
    store = _store(tmp_path)
    out = await answer_question(
        agent, store, Settings(), question="hi", role="developer", history=[]
    )
    assert "error" in out.lower()  # friendly, not a stack trace
```

(`anyio` marker: the repo's other async tests already configure the anyio backend — match their pattern; if a fixture/conftest provides `anyio_backend`, reuse it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_slack_bot.py -k answer_question -v`
Expected: FAIL — `ImportError: cannot import name 'answer_question'`.

- [ ] **Step 3: Implement `answer_question`**

Add to `src/hippo/slack_bot.py` (add imports at top):

```python
import logging

from pydantic_ai.usage import UsageLimits

from .agent import HubDeps  # build_slack_app receives the agent; slack_bot only needs HubDeps
from .config import Settings
from .storage import Storage

log = logging.getLogger("hippo.slack")
```

```python
async def answer_question(agent, store: Storage, settings: Settings, *,
                          question: str, role: str, history: list) -> str:
    """Run the agent to a final answer string for one Slack message. Reuses the
    same tool-call budget as the web chat. Any failure (including usage-limit
    hits) becomes a friendly message, never a stack trace."""
    deps = HubDeps(store=store, role=role)
    limits = UsageLimits(
        tool_calls_limit=settings.max_tool_calls,
        request_limit=settings.max_tool_calls + 5,
    )
    try:
        result = await agent.run(
            question, deps=deps, message_history=history, usage_limits=limits
        )
    except Exception:  # noqa: BLE001 — surface a friendly message, log the detail
        log.exception("agent run failed for a Slack message")
        return "Sorry — I hit an error answering that. Please try again."
    return format_answer(result.output)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_slack_bot.py -k answer_question -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/slack_bot.py tests/test_slack_bot.py
git commit -m "feat: slack_bot answer_question (run agent to a final string)"
```

---

### Task 6: `handle_event` adapter + `build_slack_app` wiring

`handle_event` is the testable heart: given a Slack event dict, a duck-typed async client, and deps, it resolves identity, picks the surface role, reconstructs history, posts a placeholder, runs the agent, and updates the placeholder. `build_slack_app` wires two thin Bolt handlers onto it.

**Files:**
- Modify: `src/hippo/slack_bot.py`
- Test: `tests/test_slack_bot.py`

- [ ] **Step 1: Write the failing test (fake client, full handler path)**

Add to `tests/test_slack_bot.py`:

```python
from hippo.slack_bot import handle_event


class FakeSlack:
    """Records calls; returns canned payloads. No network."""
    def __init__(self, email="dev@example.com", replies=None):
        self._email = email
        self._replies = replies or []
        self.posted = []      # chat_postMessage kwargs
        self.updated = []      # chat_update kwargs

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


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_handle_dm_is_flat_no_thread(tmp_path):
    client = FakeSlack()
    await handle_event(
        {"user": "UALICE", "channel": "D1", "ts": "100.0", "text": "hi"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(), bot_user_id="UBOT", is_dm=True,
    )
    assert client.posted[0].get("thread_ts") is None   # flat
    assert client.updated[0]["text"].startswith("Answer")


@pytest.mark.anyio
async def test_handle_out_of_domain_is_refused(tmp_path):
    client = FakeSlack(email="outsider@gmail.com")
    await handle_event(
        {"user": "UX", "channel": "D1", "ts": "1.0", "text": "hi"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(allowed_domain="example.com"),
        bot_user_id="UBOT", is_dm=True,
    )
    assert "don't have access" in client.posted[0]["text"].lower()
    assert not client.updated   # never ran the agent


@pytest.mark.anyio
async def test_handle_ignores_bot_messages(tmp_path):
    client = FakeSlack()
    await handle_event(
        {"user": "UBOT", "channel": "C1", "ts": "1.0", "text": "loop?", "bot_id": "B1"},
        client, store=_store(tmp_path), agent=_fixed_agent(),
        settings=Settings(), bot_user_id="UBOT", is_dm=False,
    )
    assert not client.posted and not client.updated   # no self-reply
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_slack_bot.py -k handle_ -v`
Expected: FAIL — `ImportError: cannot import name 'handle_event'`.

- [ ] **Step 3: Implement `handle_event` + `build_slack_app`**

Add to `src/hippo/slack_bot.py` (add `from .auth import AuthError, resolve_role` to imports):

```python
_NO_ACCESS = ("You don't have access to Hippo. Sign in to Slack with your work "
              "email, or contact an admin.")
_PLACEHOLDER = "_Searching the knowledge base…_"


async def handle_event(event: dict, client, *, store: Storage, agent,
                       settings: Settings, bot_user_id: str, is_dm: bool) -> None:
    """Handle one inbound Slack message (DM or channel @mention). Pure of Bolt:
    takes the event dict + a duck-typed async web client, so it is fully testable
    with a fake client. Resolves identity, applies the split-by-surface access
    rule, reconstructs history, posts a placeholder, runs the agent, updates it."""
    # Ignore the bot's own / other bots' messages — no self-reply loops.
    if _is_bot(event, bot_user_id):
        return
    user_id = event.get("user")
    channel = event["channel"]
    question = strip_mention(event.get("text", ""))
    if not user_id or not question:
        return

    # Channel @mention replies live in a thread (parent = the mention, or the
    # thread it's already in); DMs are flat.
    thread_ts = None if is_dm else (event.get("thread_ts") or event["ts"])

    # Identity → role. No email or out-of-domain → polite refusal, never answer.
    info = await client.users_info(user=user_id)
    email = (info.get("user", {}).get("profile", {}) or {}).get("email")
    if not email:
        await client.chat_postMessage(channel=channel, text=_NO_ACCESS, thread_ts=thread_ts)
        return
    try:
        role = resolve_role(store, settings, email)
    except AuthError:
        log.warning("slack: out-of-domain user %s", email)
        await client.chat_postMessage(channel=channel, text=_NO_ACCESS, thread_ts=thread_ts)
        return
    role = surface_role(role, is_dm=is_dm)

    # Reconstruct prior turns (exclude the current message), then placeholder→update.
    history = build_history(
        await _fetch_prior(client, channel, thread_ts, current_ts=event["ts"]),
        bot_user_id=bot_user_id,
    )
    placeholder = await client.chat_postMessage(
        channel=channel, text=_PLACEHOLDER, thread_ts=thread_ts)
    answer = await answer_question(
        agent, store, settings, question=question, role=role, history=history)
    await client.chat_update(channel=channel, ts=placeholder["ts"], text=answer)


async def _fetch_prior(client, channel: str, thread_ts: str | None,
                       *, current_ts: str) -> list[dict]:
    """Fetch the conversation so far, chronological, excluding the current message.
    Channel thread → conversations.replies; DM → conversations.history (reversed)."""
    if thread_ts is not None:
        resp = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=HISTORY_TURNS + 1)
        msgs = resp.get("messages", [])
    else:
        resp = await client.conversations_history(channel=channel, limit=HISTORY_TURNS + 1)
        msgs = list(reversed(resp.get("messages", [])))
    return [m for m in msgs if m.get("ts") != current_ts]


def build_slack_app(store: Storage, agent, settings: Settings):
    """Wire Bolt handlers for app_mention (channel) and message.im (DM) onto
    handle_event. Thin glue — not unit-tested (the runner is in cli.py)."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=settings.slack_bot_token, token_verification_enabled=False)

    @app.event("app_mention")
    async def _on_mention(event, client, context):
        await handle_event(event, client, store=store, agent=agent, settings=settings,
                           bot_user_id=context.bot_user_id, is_dm=False)

    @app.event("message")
    async def _on_message(event, client, context):
        # message.im only; ignore message_changed/deleted subtypes and non-DM.
        if event.get("channel_type") != "im" or event.get("subtype"):
            return
        await handle_event(event, client, store=store, agent=agent, settings=settings,
                           bot_user_id=context.bot_user_id, is_dm=True)

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_slack_bot.py -k handle_ -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Add the access-enforcement end-to-end test**

This is the security-critical assertion from spec §7: a manager's DM sees a manager doc; the same manager's channel @mention does not. Add to `tests/test_slack_bot.py`:

```python
def _rbac_store(tmp_path):
    """Store with one everyone doc and one managers-only doc."""
    con = connect(tmp_path / "h.db", embedding_dim=8)
    store = Storage(con, FakeEmbedder(dim=8))
    store.register_source("everyone-src", kind="folder", location="/e", access="everyone")
    store.register_source("mgr-src", kind="folder", location="/m", access="managers")
    store.upsert_document(source_id="everyone-src", path="/e/pub.md",
                          title="Public", content="public onboarding info", chunks=None)
    store.upsert_document(source_id="mgr-src", path="/m/sal.md",
                          title="Salaries", content="secret salary bands", chunks=None)
    return store


@pytest.mark.anyio
async def test_manager_dm_sees_manager_doc_channel_does_not(tmp_path):
    # An agent that echoes which doc-ids list_documents returns, so we can assert
    # visibility without depending on retrieval ranking.
    def reply(messages, info):
        return MR(parts=[TP(content="(answered)")])
    # Use list_documents visibility directly as the oracle:
    store = _rbac_store(tmp_path)
    assert any(d.title == "Salaries" for d in store.list_documents(role="manager"))
    assert not any(d.title == "Salaries" for d in store.list_documents(role="developer"))
    # surface_role is what handle_event passes to the agent:
    assert surface_role("manager", is_dm=True) == "manager"      # DM: sees Salaries
    assert surface_role("manager", is_dm=False) == "developer"   # channel: does not
```

> NOTE for the implementer: adapt `register_source` / `upsert_document` calls to the **actual** `Storage` signatures (check `storage.py`) — the intent is one `everyone` source and one `managers` source with a doc each. If wiring a full agent run is simpler than the oracle above, do that instead; the assertion that matters is *manager-DM sees the managers doc, manager-channel does not*.

Run: `uv run pytest tests/test_slack_bot.py -k manager_dm -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hippo/slack_bot.py tests/test_slack_bot.py
git commit -m "feat: slack_bot handle_event + build_slack_app (split-by-surface, thread-aware)"
```

---

### Task 7: `hippo slack` CLI command

**Files:**
- Modify: `src/hippo/cli.py`
- Test: `tests/test_cli.py` (add cases)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py` (match the existing CliRunner import/pattern in that file):

```python
from typer.testing import CliRunner
from hippo.cli import app

runner = CliRunner()


def test_slack_refuses_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HIPPO_SLACK_ENABLED", "false")
    monkeypatch.setenv("HIPPO_DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setenv("HIPPO_EMBEDDING_MODEL", "fake")
    result = runner.invoke(app, ["slack"])
    assert result.exit_code != 0
    assert "HIPPO_SLACK_ENABLED" in result.output


def test_slack_refuses_without_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("HIPPO_SLACK_ENABLED", "true")
    monkeypatch.setenv("HIPPO_SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("HIPPO_SLACK_APP_TOKEN", "")
    monkeypatch.setenv("HIPPO_DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setenv("HIPPO_EMBEDDING_MODEL", "fake")
    result = runner.invoke(app, ["slack"])
    assert result.exit_code != 0
    assert "token" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k slack -v`
Expected: FAIL — no such command `slack` (exit code 2 / usage error, but assertion on the specific message fails).

- [ ] **Step 3: Implement the command**

In `src/hippo/cli.py`, add (match how other commands build `Settings`/`Storage`/agent — `serve`/`mcp` are the templates):

```python
@app.command()
def slack():
    """Run the Slack bot over Socket Mode (read-only Q&A; requires HIPPO_SLACK_*)."""
    import asyncio

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    from .agent import build_agent
    from .config import Settings
    from .db import connect
    from .embeddings import build_embedder
    from .slack_bot import build_slack_app
    from .storage import Storage

    settings = Settings()
    if not settings.slack_enabled:
        typer.echo("Slack bot is disabled. Set HIPPO_SLACK_ENABLED=true to run it.", err=True)
        raise typer.Exit(code=1)
    if not settings.slack_bot_token or not settings.slack_app_token:
        typer.echo("Missing Slack tokens: set HIPPO_SLACK_BOT_TOKEN and "
                   "HIPPO_SLACK_APP_TOKEN.", err=True)
        raise typer.Exit(code=1)

    con = connect(settings.db_path, embedding_dim=settings.embedding_dim)
    store = Storage(con, build_embedder(settings))
    agent = build_agent(settings.chat_model)
    slack_app = build_slack_app(store, agent, settings)
    handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
    typer.echo("Hippo Slack bot connecting over Socket Mode…")
    asyncio.run(handler.start_async())
```

(Use the file's existing `typer`/`app` references; don't re-import `typer` if it's already module-level.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -k slack -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hippo/cli.py tests/test_cli.py
git commit -m "feat: hippo slack CLI command (Socket Mode runner)"
```

---

### Task 8: Docs — README, CLAUDE.md, roadmap

**Files:**
- Modify: `README.md` (config table, CLI list, new "Slack bot" section)
- Modify: `CLAUDE.md` (architecture block, state line, test count)
- Modify: `docs/superpowers/plans/2026-06-12-roadmap.md` (item 7 status)

- [ ] **Step 1: README config table** — add three rows after the `HIPPO_MCP_ENABLED`/`HIPPO_UI_DIST` rows:

```markdown
| `HIPPO_SLACK_ENABLED` | `false` | enable the `hippo slack` bot |
| `HIPPO_SLACK_BOT_TOKEN` | _(unset)_ | Slack bot token (`xoxb-…`) |
| `HIPPO_SLACK_APP_TOKEN` | _(unset)_ | Slack app-level token (`xapp-…`, Socket Mode) |
```

- [ ] **Step 2: README CLI list** — add under the CLI section:

```markdown
    hippo slack                     # Slack bot over Socket Mode (read-only Q&A)
```

- [ ] **Step 3: README "Slack bot" section** — add after the MCP section:

````markdown
## Slack bot

Ask Hippo questions from Slack — DM the app, or `@Hippo <question>` in a channel.
Answers are role-filtered: a DM uses your full access; a channel @mention only ever
surfaces `everyone`-access docs (sensitive content stays in DMs). Follow-ups work —
DMs are a flowing conversation; in channels, reply in the thread and `@Hippo` again.

It runs in **Socket Mode** (an outbound WebSocket), so it needs no public endpoint and
works behind IAP. Create a Slack app with Socket Mode enabled, the bot scopes
`app_mentions:read, chat:write, im:history, im:read, im:write, users:read,
users:read.email, channels:history, groups:history`, and event subscriptions
`app_mention` + `message.im`. Then:

```bash
export HIPPO_SLACK_ENABLED=true
export HIPPO_SLACK_BOT_TOKEN=xoxb-…     # Bot User OAuth Token
export HIPPO_SLACK_APP_TOKEN=xapp-…     # App-Level Token (connections:write)
uv run hippo slack
```

Run it as its own process/container alongside `hippo serve` (it keeps its own
connection to the same DB).
````

- [ ] **Step 4: CLAUDE.md** — add to the architecture block:

```
slack_bot.py   Slack Q&A bot (roadmap item 7): Socket Mode via slack-bolt. Pure helpers
               (surface_role/build_history/format_answer/answer_question) + handle_event
               adapter (tested with a fake client) + build_slack_app wiring. Split-by-surface
               access: DM=asker's role, channel @mention=everyone-only. `hippo slack` runs it.
```

Update `auth.py` line to mention `resolve_role` (shared identity→role). Bump the test count and add a State note: "Roadmap item 7 (Slack bot) implemented on branch `build/slack-integration`."

- [ ] **Step 5: roadmap** — in `docs/superpowers/plans/2026-06-12-roadmap.md`, change item 7's Status cell to **built** and reference `../specs/2026-06-13-slack-integration-design.md`.

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/plans/2026-06-12-roadmap.md
git commit -m "docs: Slack bot (README, CLAUDE.md, roadmap item 7)"
```

---

### Task 9: Final gate

- [ ] **Step 1: Full test suite, zero-network**

Run: `uv run pytest`
Expected: all pass (177 existing + the new Slack/auth/config tests), no network.

- [ ] **Step 2: UI build (CI parity; UI unaffected but keep the gate green)**

Run: `cd ui && npm run build`
Expected: clean build.

- [ ] **Step 3: Import smoke (no API keys needed — `defer_model_check`/lazy imports hold)**

Run: `uv run python -c "import hippo.slack_bot, hippo.cli; print('ok')"`
Expected: `ok` (no network, no key requirement at import).

- [ ] **Step 4: Eval (only if Ollama is up)**

Run: `uv run hippo eval eval/golden.yaml`
Expected: recall@k unchanged (Slack adds a front door; retrieval is untouched).

## Self-review notes
- **Spec coverage:** §2 connection/process → Tasks 2,6,7; §3 identity → Task 1 + handle_event; §4 split-by-surface → Task 3 (`surface_role`) + Task 6 enforcement test; §5 threading/memory → Tasks 4,6; §6 UX/format → Tasks 3,5,6; §7 module layout/tests → all; §8 out-of-scope → respected (no slash commands, no allowlist, no rate limiting).
- **No SQL outside storage.py:** the bot calls existing `Storage` methods (`list_documents`, and the agent's tools) with the chosen role; adds none.
- **Zero-network:** pure helpers + `handle_event` use `FakeEmbedder`/`FunctionModel` and a fake client; only `build_slack_app`/`AsyncSocketModeHandler` touch Slack and are not unit-tested.
- **Fail-closed:** `surface_role` returns `developer` for channels even for admins; out-of-domain / no-email → refusal before any agent run.
- **DRY:** `resolve_role` is now the single identity→role function shared by `api.py` and the bot.
