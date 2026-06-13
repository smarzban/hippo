from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from hippo.slack_bot import HISTORY_TURNS, build_history, format_answer, surface_role

BOT = "UBOT"


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
