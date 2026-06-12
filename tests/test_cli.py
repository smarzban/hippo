from typer.testing import CliRunner

from hippo.cli import app

runner = CliRunner()


def _env(tmp_path):
    return {
        "HIPPO_DB_PATH": str(tmp_path / "t.db"),
        "HIPPO_EMBEDDING_MODEL": "fake",
        "HIPPO_EMBEDDING_DIM": "32",
        "HIPPO_ENRICH_ENABLED": "false",
    }


def test_sync_and_resync(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha content")
    r = runner.invoke(app, ["sync", str(docs)], env=_env(tmp_path))
    assert r.exit_code == 0, r.output
    assert "synced 1" in r.output

    # re-sync all registered sources (no arg)
    r = runner.invoke(app, ["sync"], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "skipped 1" in r.output


def test_add_single_file(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Note\n\nbody")
    r = runner.invoke(app, ["add", str(f)], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "added" in r.output


def test_search_command(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Note\n\ntelegram webhook details")
    runner.invoke(app, ["add", str(f)], env=_env(tmp_path))
    r = runner.invoke(app, ["search", "telegram"], env=_env(tmp_path))
    assert r.exit_code == 0
    assert "note.md" in r.output


def test_role_set_and_list(tmp_path):
    env = _env(tmp_path)
    r = runner.invoke(app, ["role", "set", "a@x.com", "manager"], env=env)
    assert r.exit_code == 0
    r = runner.invoke(app, ["role", "list"], env=env)
    assert "manager" in r.output and "a@x.com" in r.output
    r = runner.invoke(app, ["role", "set", "a@x.com", "superuser"], env=env)
    assert r.exit_code != 0


def test_token_create_prints_token(tmp_path):
    r = runner.invoke(app, ["token", "create", "a@x.com", "--name", "laptop"], env=_env(tmp_path))
    assert r.exit_code == 0 and "hk_" in r.output


def test_backup_command_writes_file(tmp_path):
    env = _env(tmp_path)
    runner.invoke(app, ["token", "create", "a@x.com"], env=env)  # touch the db so it exists
    dest = tmp_path / "out.db"
    r = runner.invoke(app, ["backup", str(dest)], env=env)
    assert r.exit_code == 0 and dest.exists()


def test_cli_sync_honors_max_doc_chars(tmp_path):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "big.md").write_text("# Big\n\n" + "x" * 5000)
    env = _env(tmp_path) | {"HIPPO_MAX_DOC_CHARS": "1000"}
    r = runner.invoke(app, ["sync", str(docs)], env=env)
    assert r.exit_code == 0
    # the oversized doc must be skipped, not indexed
    out = runner.invoke(app, ["search", "Big"], env=env)
    assert "big.md" not in out.output


def test_backup_to_existing_dest_fails_cleanly(tmp_path):
    env = _env(tmp_path)
    runner.invoke(app, ["token", "create", "a@x.com"], env=env)  # ensure db exists
    dest = tmp_path / "exists.db"; dest.write_text("already here")
    r = runner.invoke(app, ["backup", str(dest)], env=env)
    assert r.exit_code != 0 and "backup failed" in (r.output + str(r.stderr or ""))


def test_token_list_and_revoke(tmp_path):
    env = _env(tmp_path)
    create = runner.invoke(app, ["token", "create", "a@x.com"], env=env)
    token = [ln for ln in create.output.splitlines() if ln.startswith("hk_")][0]
    r = runner.invoke(app, ["token", "list", "a@x.com"], env=env)
    assert "#1" in r.output
    r = runner.invoke(app, ["token", "revoke", "a@x.com", "1"], env=env)
    assert r.exit_code == 0 and "revoked" in r.output
    r = runner.invoke(app, ["token", "revoke", "a@x.com", "1"], env=env)
    assert r.exit_code != 0  # already revoked
