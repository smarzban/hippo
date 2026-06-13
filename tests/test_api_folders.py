# tests/test_api_folders.py
from fastapi.testclient import TestClient

from hippo.api import build_app
from hippo.config import Settings


def _settings(tmp_path, **over):
    base = dict(_env_file=None, db_path=tmp_path / "t.db", embedding_model="fake",
                embedding_dim=32, enrich_enabled=False)
    base.update(over)
    return Settings(**base)


def test_get_folders_returns_seeded_tree(tmp_path):
    c = TestClient(build_app(_settings(tmp_path)))  # none-mode caller is owner
    rows = c.get("/folders").json()
    names = {r["name"]: r for r in rows}
    assert {"Default", "Private", "Owner"} <= set(names)
    assert names["Default"]["tier"] == "user" and names["Default"]["writable"] is True
    assert names["Owner"]["tier"] == "owner"


def test_create_rename_move_delete_folder(tmp_path):
    c = TestClient(build_app(_settings(tmp_path)))
    rows = c.get("/folders").json()
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    owner_id = next(r["id"] for r in rows if r["name"] == "Owner")
    # create
    r = c.post("/folders", json={"parent_id": default_id, "name": "Retail"})
    assert r.status_code == 200
    fid = r.json()["id"]
    assert r.json()["tier"] == "user"
    # duplicate sibling rejected
    assert c.post("/folders", json={"parent_id": default_id, "name": "Retail"}).status_code == 400
    # rename
    assert c.patch(f"/folders/{fid}", json={"name": "RetailOps"}).status_code == 200
    # move across roots rewrites tier
    assert c.patch(f"/folders/{fid}", json={"parent_id": owner_id}).status_code == 200
    moved = next(x for x in c.get("/folders").json() if x["id"] == fid)
    assert moved["tier"] == "owner"
    # delete
    assert c.delete(f"/folders/{fid}").status_code == 200
    # roots are undeletable
    assert c.delete(f"/folders/{default_id}").status_code == 400


def test_non_owner_cannot_create_folder_in_iap_mode(tmp_path):
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from hippo.auth import IapVerifier

    AUD = "/projects/1/global/backendServices/2"
    s = _settings(tmp_path, auth_mode="iap", iap_audience=AUD)
    key = ec.generate_private_key(ec.SECP256R1())
    app = build_app(s, iap_verifier=IapVerifier(AUD, key_fetcher=lambda: {"k1": key.public_key()}))
    c = TestClient(app)
    tok = jwt.encode({"aud": AUD, "iss": "https://cloud.google.com/iap",
                      "exp": int(time.time()) + 600, "email": "dev@x.com"},
                     key, algorithm="ES256", headers={"kid": "k1"})
    h = {"x-goog-iap-jwt-assertion": tok}
    default_id = next(r["id"] for r in c.get("/folders", headers=h).json() if r["name"] == "Default")
    # a plain user (rank 0) cannot create folders (admin+ only)
    assert c.post("/folders", json={"parent_id": default_id, "name": "X"}, headers=h).status_code == 403


def test_admin_cannot_mutate_owner_tier_folder(tmp_path):
    """Regression (PR #11 review, HIGH): folder mutate/delete/resync endpoints must
    check the TARGET folder's tier, not just the require_admin floor. Otherwise an
    admin could move an owner-tier folder under Default (rewriting its subtree to
    user tier and leaking owner-only docs), or delete/resync it by guessed id."""
    app = build_app(_settings(tmp_path))  # none-mode caller (no header) is owner
    c = TestClient(app)
    rows = c.get("/folders").json()
    owner_id = next(r["id"] for r in rows if r["name"] == "Owner")
    default_id = next(r["id"] for r in rows if r["name"] == "Default")
    # owner sets up an owner-tier subfolder
    secret = c.post("/folders", json={"parent_id": owner_id, "name": "Secrets"}).json()
    assert secret["tier"] == "owner"
    sid = secret["id"]

    # mint an admin-rank bearer token (works in any mode)
    store = app.state.store
    store.set_role("mgr@x.com", "admin")
    admin_tok = store.create_token("mgr@x.com")
    ah = {"Authorization": f"Bearer {admin_tok}"}

    # admin (rank 1) cannot move / rename / delete / resync the owner-tier folder
    assert c.patch(f"/folders/{sid}", json={"parent_id": default_id}, headers=ah).status_code == 403
    assert c.patch(f"/folders/{sid}", json={"name": "Pwned"}, headers=ah).status_code == 403
    assert c.delete(f"/folders/{sid}", headers=ah).status_code == 403
    assert c.post(f"/folders/{sid}/resync", headers=ah).status_code == 403
    # the folder is untouched (still owner tier, original name, still present)
    still = next(x for x in c.get("/folders").json() if x["id"] == sid)
    assert still["name"] == "Secrets" and still["tier"] == "owner"

    # sanity: the same admin CAN manage a user-tier folder (not over-restricted)
    sub = c.post("/folders", json={"parent_id": default_id, "name": "TeamDocs"}, headers=ah)
    assert sub.status_code == 200
    assert c.patch(f"/folders/{sub.json()['id']}", json={"name": "Team"}, headers=ah).status_code == 200
    # and owner can still delete the owner-tier folder
    assert c.delete(f"/folders/{sid}").status_code == 200


def test_duplicate_location_mount_rejected(tmp_path):
    """Regression (PR #11 review, MEDIUM): folders.location is unique, so the same
    filesystem path can't be mounted twice into ambiguous duplicate folders."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nhello")
    c = TestClient(build_app(_settings(tmp_path)))  # none-mode owner; no source_roots needed
    default_id = next(r["id"] for r in c.get("/folders").json() if r["name"] == "Default")
    first = c.post("/folders", json={"parent_id": default_id, "name": "Mirror",
                                     "origin": "folder", "location": str(docs)})
    assert first.status_code == 200
    dup = c.post("/folders", json={"parent_id": default_id, "name": "Mirror2",
                                   "origin": "folder", "location": str(docs)})
    assert dup.status_code == 400 and "mounted" in dup.json()["detail"]
