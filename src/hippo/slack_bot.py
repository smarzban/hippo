"""Slack front door onto the Hippo agent (roadmap item 7). Read-only Q&A over
DM and channel @mention, role-filtered, Socket Mode. The Slack-facing logic is
split into pure functions (unit-tested) plus a thin handle_event adapter tested
with a fake client; the AsyncSocketModeHandler runner lives in cli.py.

Design: docs/superpowers/specs/2026-06-13-slack-integration-design.md
"""

import re

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

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
