from hippo.db import connect
from hippo.embeddings import FakeEmbedder
from hippo.storage import Storage


def _store(tmp_path):
    return Storage(connect(tmp_path / "h.db", embedding_dim=8), FakeEmbedder(dim=8))


def test_list_all_tokens_spans_users(tmp_path):
    s = _store(tmp_path)
    s.create_token("a@x.com", "laptop")
    s.create_token("b@x.com", "ci")
    rows = s.list_all_tokens()
    emails = {r[1] for r in rows}              # (id, email, name, created_at, last_used_at)
    assert emails == {"a@x.com", "b@x.com"}
    assert all(len(r) == 5 for r in rows)


def test_revoke_token_any_ignores_owner(tmp_path):
    s = _store(tmp_path)
    s.create_token("a@x.com", "laptop")
    tok_id = s.list_all_tokens()[0][0]
    assert s.revoke_token_any(tok_id) is True       # admin revokes without owning it
    assert s.list_all_tokens() == []
    assert s.revoke_token_any(tok_id) is False      # already gone


def test_token_resolves_after_email_attribute_change(tmp_path):
    s = _store(tmp_path)
    s.set_role("dev@x.com", "user")
    tok = s.create_token("dev@x.com", "laptop")
    # email is now a mutable attribute on the surrogate-keyed row
    s.con.execute("UPDATE users SET email='dev2@x.com' WHERE email='dev@x.com'")
    s.con.commit()
    assert s.resolve_token(tok) == "dev2@x.com"  # token followed the user_id
