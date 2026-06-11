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
