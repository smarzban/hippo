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


EMBED_BATCH = 64  # max texts per API request (parity with reindex's batch size)


class OpenAIEmbedder:
    """Real embeddings via the OpenAI-compatible API (default: text-embedding-3-small;
    also Ollama via OPENAI_BASE_URL)."""

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536, *,
                 timeout: float = 60.0, max_retries: int = 2):
        from openai import OpenAI

        self.model = model
        self.dim = dim
        # Explicit bounds: the SDK default is a 600s read timeout with 2 retries, so a
        # hung/unreachable endpoint (Ollama Cloud / local Ollama) could otherwise block
        # an ingest/reindex/chat thread for ~10 minutes per call with no operator knob.
        self._client = OpenAI(timeout=timeout, max_retries=max_retries)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Only the text-embedding-3-* family supports the `dimensions` parameter; for
        # other models (e.g. a local nomic-embed-text) the returned length is the model's
        # native dim and sending `dimensions` would error. Without this, a non-default
        # embedding_dim was honored only by the chunk_vec width — a latent mismatch.
        extra = {"dimensions": self.dim} if self.model.startswith("text-embedding-3") else {}
        out: list[list[float]] = []
        # Batch so one document's worth of chunks (up to ~330) isn't sent as a single
        # oversized request that could hit a provider array/token limit or time out.
        for i in range(0, len(texts), EMBED_BATCH):
            resp = self._client.embeddings.create(
                model=self.model, input=texts[i:i + EMBED_BATCH], **extra)
            out.extend(d.embedding for d in resp.data)
        return out


def build_embedder(settings) -> Embedder:
    """Embedder from config. 'fake' is allowed for offline/dev use."""
    if settings.embedding_model == "fake":
        return FakeEmbedder(dim=settings.embedding_dim)
    return OpenAIEmbedder(model=settings.embedding_model, dim=settings.embedding_dim,
                          timeout=settings.embed_timeout_s, max_retries=settings.embed_max_retries)
