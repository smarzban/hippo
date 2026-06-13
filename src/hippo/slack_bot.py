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
