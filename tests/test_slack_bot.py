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
