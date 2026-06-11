import math

from hippo.embeddings import FakeEmbedder


def test_fake_embedder_deterministic_unit_vectors():
    e = FakeEmbedder(dim=32)
    a1 = e.embed(["hello world"])[0]
    a2 = e.embed(["hello world"])[0]
    b = e.embed(["goodbye"])[0]
    assert a1 == a2
    assert a1 != b
    assert len(a1) == 32
    assert math.isclose(sum(x * x for x in a1), 1.0, rel_tol=1e-6)


def test_fake_embedder_similar_texts_share_tokens():
    e = FakeEmbedder(dim=32)
    base = e.embed(["telegram webhook setup"])[0]
    near = e.embed(["telegram webhook configuration"])[0]
    far = e.embed(["quarterly budget report"])[0]

    def dot(u, v):
        return sum(a * b for a, b in zip(u, v))

    assert dot(base, near) > dot(base, far)
