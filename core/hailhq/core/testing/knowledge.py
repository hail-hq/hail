"""Test helpers for the knowledge base: a fake embedder and a sample
document. The hosted embedding API is never called in tests."""

from __future__ import annotations

import hashlib

import numpy as np
from hailhq.core.knowledge.embed import DIMS, EmbeddingUnavailable
from hailhq.core.knowledge.text import terms


class FakeEmbedder:
    """Bag of words hashed into ``DIMS`` buckets: texts sharing words point
    the same way. Same interface as ``embed.Embedder``. ``fail`` makes
    every request raise ``EmbeddingUnavailable``; a request with a text
    holding ``refuse`` is refused like an HTTP 400; ``calls`` counts
    requests."""

    def __init__(self, model: str = "fake-embedding") -> None:
        self.model = model
        self.fail = False
        self.refuse: str | None = None
        self.calls = 0

    def one(self, text: str) -> np.ndarray:
        vector = np.zeros(DIMS, dtype=np.float32)
        for word in terms(text):
            bucket = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
            vector[bucket % DIMS] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise EmbeddingUnavailable("HTTP 503")

    async def embed_query(self, text: str, *, timeout: float = 0) -> np.ndarray:
        self._check()
        return self.one(text)

    async def embed(self, texts: list[str], *, timeout: float = 0) -> np.ndarray:
        self._check()
        if self.refuse and any(self.refuse in t for t in texts):
            raise EmbeddingUnavailable("HTTP 400", refused=True)
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        return np.stack([self.one(t) for t in texts])


# An invented product manual (no real company or product).
NORTHGATE_MANUAL = """# Northgate Solar NS-5000 hybrid inverter

## Installation
Mount the NS-5000 on a vertical wall away from direct sunlight. Keep 30 cm
of free space on every side for cooling. Use the wall bracket NS-WB-02.

## Error codes
E-204 means the grid voltage is out of range. Check the AC breaker and wait
five minutes; the inverter reconnects by itself. E-310 means the battery
temperature is too high.

## Battery
The NS-BAT-48V-100 battery stores 4.8 kWh. Charge it fully before first use.

## Warranty
The inverter has a ten year warranty from the date of purchase. Register
the serial number within 90 days.
"""
