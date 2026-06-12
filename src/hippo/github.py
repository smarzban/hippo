"""Upload-to-repo via the GitHub Contents API: one HTTP call commits one file —
no clone, no local git state (spec §1, 'version control as the default path')."""

import base64

import httpx


class GitHubError(Exception):
    pass


class GitHubContentsClient:
    def __init__(self, repo: str, token: str, branch: str = "main",
                 client: httpx.Client | None = None):
        self.repo = repo
        self.branch = branch
        self._http = client or httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )

    def put_file(self, path: str, content: bytes, message: str) -> str:
        """Create or update `path` on the branch; returns the commit sha."""
        url = f"/repos/{self.repo}/contents/{path}"
        body = {"message": message,
                "content": base64.b64encode(content).decode(),
                "branch": self.branch}
        existing = self._http.get(url, params={"ref": self.branch})
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]  # update needs the current blob sha
        r = self._http.put(url, json=body)
        if r.status_code not in (200, 201):
            raise GitHubError(f"GitHub commit failed ({r.status_code}): {r.text[:200]}")
        return r.json()["commit"]["sha"]
