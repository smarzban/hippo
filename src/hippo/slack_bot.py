"""Slack front door onto the Hippo agent (roadmap item 7). Read-only Q&A over
DM and channel @mention, role-filtered, Socket Mode. The Slack-facing logic is
split into pure functions (unit-tested) plus a thin handle_event adapter tested
with a fake client; the AsyncSocketModeHandler runner lives in cli.py.

Design: docs/superpowers/specs/2026-06-13-slack-integration-design.md
"""

import logging
import re

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import UsageLimits

from .agent import HubDeps
from .auth import AuthError, resolve_role
from .config import Settings
from .storage import Storage

log = logging.getLogger("hippo.slack")

_BLANK_ANSWER = "I couldn't find an answer to that in the knowledge base."

HISTORY_TURNS = 10  # cap reconstructed turns (token budget on Ollama Cloud)

_MENTION = re.compile(r"<@[^>]+>")


def surface_role(resolved_role: str, *, is_dm: bool) -> str:
    """Pick the role passed to the agent based on the Slack surface. DMs are
    private, so the asker's full role applies. A channel @mention has an audience,
    so force the 'everyone'-access view (developer) regardless of who asks —
    fails closed, never leaks manager-only docs into a public channel (spec §4)."""
    return resolved_role if is_dm else "developer"


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


def format_answer(text: str) -> str:
    """Final agent text → Slack message body. Citations stay as literal
    [path > section] text. Guards the empty-output case (e.g. the gpt-oss
    empty-content quirk) so the bot always says something."""
    return text.strip() if text and text.strip() else _BLANK_ANSWER


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

    # AsyncApp defers token verification to runtime (no auth.test at construction),
    # so no signing secret or verification flag is needed for Socket Mode.
    app = AsyncApp(token=settings.slack_bot_token)

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
