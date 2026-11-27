"""The hosted embedding client: request shape, normalization, batches,
errors, and the key and width settings. No network: httpx's mock
transport answers."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from hailhq.core.config import settings
from hailhq.core.knowledge import embed
from hailhq.core.knowledge.embed import (
    BATCH,
    DIMS,
    MAX_INPUT_CHARS,
    Embedder,
    EmbeddingConfig,
    EmbeddingUnavailable,
    get_embedder,
    normalize,
)

CONFIG = EmbeddingConfig("https://embed.example.com/v1/", "test-key", "m-1", DIMS)


def answer(texts: list[str], *, scale: float = 3.0, skip_zero_index: bool = True):
    """An OpenAI-style answer, unnormalized, newest index first, with
    ``"index": 0`` left out like Gemini does."""
    data = []
    for i, _ in enumerate(texts):
        row = [0.0] * DIMS
        row[i % DIMS] = scale
        item = {"object": "embedding", "embedding": row}
        if i or not skip_zero_index:
            item["index"] = i
        data.append(item)
    return {"object": "list", "data": list(reversed(data))}


@pytest.fixture()
def requests(monkeypatch):
    """Record requests; reply with ``answer`` unless a test sets ``reply``."""
    seen: list[httpx.Request] = []
    state: dict = {"reply": None}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if state["reply"] is not None:
            return state["reply"](request)
        return httpx.Response(200, json=answer(json.loads(request.content)["input"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(embed, "_client", lambda: client)
    return seen, state


def test_normalize_scales_rows_and_refuses_zero() -> None:
    rows = normalize(np.array([[3.0, 4.0], [0.0, 2.0]]))
    assert np.allclose(np.linalg.norm(rows, axis=1), 1)
    assert np.allclose(rows[0], [0.6, 0.8])
    with pytest.raises(EmbeddingUnavailable):
        normalize(np.array([[0.0, 0.0]]))


async def test_request_shape_and_unit_vectors(requests) -> None:
    seen, _ = requests
    vectors = await Embedder(CONFIG).embed(["a", "b", "c"])
    assert vectors.shape == (3, DIMS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)
    # Sorted by index, the missing "index": 0 counting as 0.
    assert [int(np.argmax(v)) for v in vectors] == [0, 1, 2]
    request = seen[0]
    assert str(request.url) == "https://embed.example.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "m-1",
        "input": ["a", "b", "c"],
        "dimensions": DIMS,
    }


async def test_batches_and_long_texts_are_cut(requests) -> None:
    seen, _ = requests
    vectors = await Embedder(CONFIG).embed(["x" * 5000] * (BATCH + 1))
    assert vectors.shape == (BATCH + 1, DIMS)
    sizes = [len(json.loads(r.content)["input"]) for r in seen]
    assert sizes == [BATCH, 1]
    assert len(json.loads(seen[0].content)["input"][0]) == MAX_INPUT_CHARS


@pytest.mark.parametrize(
    ("reply", "retry_after"),
    [
        (lambda r: httpx.Response(429, headers={"Retry-After": "7"}), 7.0),
        (lambda r: httpx.Response(500, json={"error": "x"}), None),
        (lambda r: httpx.Response(200, json={"data": "nope"}), None),
        (lambda r: httpx.Response(200, json=answer(["a", "b"])), None),
    ],
)
async def test_errors_raise_unavailable(requests, reply, retry_after) -> None:
    _, state = requests
    state["reply"] = reply
    with pytest.raises(EmbeddingUnavailable) as caught:
        await Embedder(CONFIG).embed_query("question")
    assert caught.value.retry_after == retry_after
    assert "question" not in str(caught.value)


async def test_timeout_raises_unavailable(requests) -> None:
    _, state = requests

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    state["reply"] = slow
    with pytest.raises(EmbeddingUnavailable, match="ReadTimeout"):
        await Embedder(CONFIG).embed_query("q")


async def test_any_client_error_raises_unavailable(requests) -> None:
    """A bad setting (a URL or key httpx cannot send) fails open too."""
    _, state = requests

    def broken(request):
        raise ValueError("cannot send")

    state["reply"] = broken
    with pytest.raises(EmbeddingUnavailable, match="ValueError"):
        await Embedder(CONFIG).embed_query("q")


async def test_refused_input_is_marked(requests) -> None:
    _, state = requests
    state["reply"] = lambda r: httpx.Response(400, json={"error": "bad"})
    with pytest.raises(EmbeddingUnavailable) as caught:
        await Embedder(CONFIG).embed(["q"])
    assert caught.value.refused
    state["reply"] = lambda r: httpx.Response(429)
    with pytest.raises(EmbeddingUnavailable) as caught:
        await Embedder(CONFIG).embed(["q"])
    assert not caught.value.refused


def test_google_key_only_for_google_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "hail_embedding_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "google-key")
    google = "https://generativelanguage.googleapis.com/v1beta/openai"
    monkeypatch.setattr(settings, "hail_embedding_base_url", google)
    assert embed.embedding_config().api_key == "google-key"
    # Never sent to another host.
    monkeypatch.setattr(
        settings, "hail_embedding_base_url", "https://api.openai.com/v1"
    )
    assert get_embedder() is None
    monkeypatch.setattr(settings, "hail_embedding_api_key", "own-key")
    assert embed.embedding_config().api_key == "own-key"
    assert get_embedder().model == settings.hail_embedding_model


def test_other_width_needs_a_migration(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "hail_embedding_api_key", "k")
    monkeypatch.setattr(settings, "hail_embedding_dimensions", 768)
    monkeypatch.setattr(embed, "_warned", set())
    assert get_embedder() is None
    assert "migration" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "generativelanguage.googleapis.com/v1beta/openai",
        "http://[::1/v1",
        "https://api.example.com:80a/v1",
    ],
)
def test_base_url_must_be_http(monkeypatch, caplog, url) -> None:
    monkeypatch.setattr(settings, "hail_embedding_api_key", "k")
    monkeypatch.setattr(settings, "hail_embedding_base_url", url)
    monkeypatch.setattr(embed, "_warned", set())
    assert get_embedder() is None
    assert "http(s) URL" in caplog.text
