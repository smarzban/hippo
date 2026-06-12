import base64
import json

import httpx
import pytest

from hippo.github import GitHubContentsClient, GitHubError


def _client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHubContentsClient("org/docs", "tok", branch="main", client=http)


def test_put_file_creates_new():
    seen = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"commit": {"sha": "abc123"}})

    sha = _client(handler).put_file("uploads/n.md", b"# N", "hippo upload: n.md (by a@x.com)")
    assert sha == "abc123"
    assert seen["url"].endswith("/repos/org/docs/contents/uploads/n.md")
    assert base64.b64decode(seen["body"]["content"]) == b"# N"
    assert seen["body"]["branch"] == "main" and "sha" not in seen["body"]


def test_put_file_updates_existing_with_sha():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "oldsha"})
        assert json.loads(request.content)["sha"] == "oldsha"
        return httpx.Response(200, json={"commit": {"sha": "newsha"}})

    assert _client(handler).put_file("a.md", b"x", "m") == "newsha"


def test_put_file_error_raises():
    def handler(request):
        return httpx.Response(404) if request.method == "GET" else httpx.Response(422, text="nope")

    with pytest.raises(GitHubError):
        _client(handler).put_file("a.md", b"x", "m")


def test_put_file_retries_once_on_409_race():
    calls = {"get": 0, "put": 0}

    def handler(request):
        if request.method == "GET":
            calls["get"] += 1
            # first GET: file absent; second GET (after 409): now exists
            return httpx.Response(404) if calls["get"] == 1 else httpx.Response(200, json={"sha": "racer"})
        calls["put"] += 1
        if calls["put"] == 1:
            return httpx.Response(409, text="conflict")  # lost the race
        assert json.loads(request.content)["sha"] == "racer"
        return httpx.Response(200, json={"commit": {"sha": "final"}})

    assert _client(handler).put_file("a.md", b"x", "m") == "final"
    assert calls == {"get": 2, "put": 2}


def test_put_file_409_twice_raises():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(409, text="conflict")

    with pytest.raises(GitHubError):
        _client(handler).put_file("a.md", b"x", "m")
