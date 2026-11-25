"""The knowledge base's embeddings: one hosted, OpenAI-compatible
``/embeddings`` API, the same for ingest (document pieces) and search
(questions).

The provider is a setting: any service that takes the OpenAI request
``{"model", "input", "dimensions"}`` and answers ``{"data": [{"index",
"embedding"}]}`` works (Gemini's OpenAI-compatible endpoint, OpenAI,
OpenRouter). Settings, all code defaults (``hailhq.core.config``):

* ``hail_embedding_base_url``: the API root; requests go to
  ``<base>/embeddings``. Default: Gemini's OpenAI-compatible root.
* ``hail_embedding_api_key``: the Bearer token. Empty: the Google API key,
  only while the base URL is Google's (a Google key is never sent to
  another host). No key at all: the knowledge base runs on keyword search
  alone, with one warning per process.
* ``hail_embedding_model``: default ``gemini-embedding-001``.
* ``hail_embedding_dimensions``: default 1536. ``knowledge_pieces.embedding``
  is ``halfvec(1536)``, so another width needs a migration and a re-embed;
  until then embeddings are off (logged).

Every vector is L2-normalized here, whatever the provider: Gemini returns
unnormalized vectors below 3072 dimensions, and the searches take a dot
product as the cosine. Each piece stores the model that embedded it
(``knowledge_pieces.embedding_model``); searches compare only vectors of
the current model, and the ingest worker re-embeds the rest
(``ingest.KnowledgeIngestWorker``).

Fails open: any error or timeout raises ``EmbeddingUnavailable``, which
every caller turns into keyword search (a call, a text, an email reply,
the search API) or a later retry (ingest).
Nothing here logs text or the key.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import numpy as np
from hailhq.core.config import settings
from hailhq.core.urls import join_url

__all__ = [
    "BATCH",
    "DIMS",
    "MAX_INPUT_CHARS",
    "Embedder",
    "EmbeddingConfig",
    "EmbeddingUnavailable",
    "embedding_config",
    "get_embedder",
    "normalize",
]

logger = logging.getLogger(__name__)

# The width of ``knowledge_pieces.embedding`` (the knowledge base migration).
DIMS = 1536
# Texts per request: Gemini's OpenAI-compatible endpoint takes at most 100.
BATCH = 100
# Characters of one text sent: pieces are about 500 tokens and CSV cards at
# most 2,000 characters, so this only guards the model's input limit
# (2,048 tokens for gemini-embedding-001, which cuts longer input quietly).
MAX_INPUT_CHARS = 3000
# One question (a text, an email, the search API). Calls wait less
# (``hailhq.voicebot.knowledge``).
QUERY_TIMEOUT = 3.0
# One batch of document pieces.
BATCH_TIMEOUT = 30.0
_GOOGLE_HOST = "generativelanguage.googleapis.com"


# Answers that refuse the input itself (bad or too long); retrying the same
# texts never helps. 401, 403, 404 and 429 are the account or the setting.
_REFUSED = frozenset({400, 413, 422})


class EmbeddingUnavailable(RuntimeError):
    """The API failed, timed out or answered something unusable. The
    message names the cause (a status code or an exception type), never
    text. ``retry_after`` is the server's Retry-After, in seconds.
    ``refused``: the API refused these texts (a 400, 413 or 422, or a zero
    vector), so the same texts will fail again."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        refused: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.refused = refused


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dims: int


def embedding_config() -> EmbeddingConfig | None:
    """The configured embedding API, or None when it cannot be used (no
    key, or a width the column cannot hold). The reason is logged once."""
    base_url = settings.hail_embedding_base_url
    api_key = settings.hail_embedding_api_key
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        _warn_once(
            "knowledge base: HAIL_EMBEDDING_BASE_URL is not a URL; keywords only"
        )
        return None
    if not api_key and host == _GOOGLE_HOST:
        api_key = settings.google_api_key
    model, dims = settings.hail_embedding_model, settings.hail_embedding_dimensions
    if not api_key or not model:
        _warn_once(
            "knowledge base: no embedding API key (HAIL_EMBEDDING_API_KEY, or "
            "GOOGLE_API_KEY for the default Gemini URL); search uses keywords only"
        )
        return None
    if dims != DIMS:
        _warn_once(
            f"knowledge base: HAIL_EMBEDDING_DIMENSIONS is {dims} but "
            f"knowledge_pieces holds {DIMS}; a migration and a re-embed are "
            "needed first. Search uses keywords only"
        )
        return None
    return EmbeddingConfig(base_url, api_key, model, dims)


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logger.warning(message)


def normalize(rows: np.ndarray) -> np.ndarray:
    """Each row scaled to length 1, as float32. A zero row is refused."""
    rows = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0):
        raise EmbeddingUnavailable("a zero or invalid vector", refused=True)
    return rows / norms


# One HTTP client per event loop (a call process, the API, the text
# worker), so later requests reuse the TLS connection.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def _client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120)
        )
        _clients[loop] = client
    return client


class Embedder:
    """The configured embedding API. ``model`` is the id stored with each
    piece it embeds."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._url = join_url(config.base_url, "embeddings")
        self.model = config.model

    async def _request(self, texts: list[str], timeout: float) -> np.ndarray:
        body = {
            "model": self._config.model,
            "input": [t[:MAX_INPUT_CHARS] for t in texts],
            "dimensions": self._config.dims,
        }
        try:
            response = await _client().post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=timeout,
            )
        except Exception as exc:
            # Any failure, also a bad URL or key setting (httpx.InvalidURL,
            # UnicodeEncodeError), never only network errors.
            raise EmbeddingUnavailable(type(exc).__name__) from None
        if response.status_code != 200:
            retry_after = None
            try:
                retry_after = float(response.headers.get("retry-after", ""))
            except ValueError:
                pass
            raise EmbeddingUnavailable(
                f"HTTP {response.status_code}",
                retry_after=retry_after,
                refused=response.status_code in _REFUSED,
            )
        try:
            # Gemini leaves out ``"index": 0`` (a protobuf default).
            data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
            rows = np.asarray([d["embedding"] for d in data], dtype=np.float32)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise EmbeddingUnavailable("an unreadable answer") from None
        if rows.shape != (len(texts), self._config.dims):
            raise EmbeddingUnavailable(f"vectors of shape {rows.shape}")
        return normalize(rows)

    async def embed(
        self, texts: list[str], *, timeout: float = BATCH_TIMEOUT
    ) -> np.ndarray:
        """Unit vectors for ``texts``, one row each, ``BATCH`` per request."""
        if not texts:
            return np.zeros((0, self._config.dims), dtype=np.float32)
        parts = [
            await self._request(texts[i : i + BATCH], timeout)
            for i in range(0, len(texts), BATCH)
        ]
        return np.concatenate(parts)

    async def embed_query(
        self, text: str, *, timeout: float = QUERY_TIMEOUT
    ) -> np.ndarray:
        """One question, as a unit vector."""
        return (await self._request([text], timeout))[0]


def get_embedder() -> Embedder | None:
    """The configured embedding API, or None (logged once) when there is no
    key or the width does not fit. Cheap: reads settings only."""
    config = embedding_config()
    return Embedder(config) if config is not None else None
