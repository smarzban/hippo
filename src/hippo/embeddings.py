import hashlib
import math
from typing import Protocol


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic bag-of-token-hashes embedder for tests. No network."""

    def __init__(self, dim: int = 32):
        self.model = "fake"
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in text.lower().split():
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class OpenAIEmbedder:
    """Real embeddings via the OpenAI API (default: text-embedding-3-small)."""

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536):
        from openai import OpenAI

        self.model = model
        self.dim = dim
        self._client = OpenAI()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]


def build_embedder(settings) -> Embedder:
    """Embedder from config. 'fake' is allowed for offline/dev use."""
    if settings.embedding_model == "fake":
        return FakeEmbedder(dim=settings.embedding_dim)
    return OpenAIEmbedder(model=settings.embedding_model, dim=settings.embedding_dim)
