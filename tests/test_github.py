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
