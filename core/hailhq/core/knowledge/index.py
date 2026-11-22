"""Hybrid search over pieces held in memory, for one call.

A call loads its workspace's pieces once (``hailhq.voicebot.knowledge``) and
asks this index on every caller turn: cosine similarity over the
embeddings, plus BM25 over ``text.terms`` (the same words the SQL search
uses), merged with reciprocal rank fusion (``fuse``). Both lists keep only
pieces that clear a floor (``MIN_SIMILARITY``, ``MIN_BM25``). The meaning
floor drops only clear junk, so most questions bring their best pieces and
the model is told to use them only when they answer (``prompts``). Filler
messages are not searched at all, in calls, texts or email
(``hailhq.voicebot.knowledge.worth_searching``).

``Hit`` and ``fuse`` are shared with the SQL search (``search``).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import numpy as np
from hailhq.core.knowledge.text import query_terms, terms

__all__ = [
    "CANDIDATES",
    "MIN_BM25",
    "MIN_IDF_PIECES",
    "MIN_SIMILARITY",
    "RRF_K",
    "Hit",
    "MemoryIndex",
    "Piece",
    "fuse",
]

# Each list's best pieces taken into the fusion.
CANDIDATES = 20
# Reciprocal rank fusion constant (Cormack et al. 2009).
RRF_K = 60
# Cosine floor for a piece found by meaning only. E5 scores even unrelated
# text high. Measured with the shipped model on an invented manual (25
# questions, 7 languages): right sections 0.77 to 0.90 (the lowest two in
# Italian and French), right 400-token pieces from 0.73, off-topic
# questions' best match 0.72 to 0.81. So the floor sits below every right
# piece; it drops only clear junk, and most off-topic questions still bring
# pieces, which the model is told to ignore when they do not answer
# (docs/operations/knowledge-base.md).
MIN_SIMILARITY = 0.72
# BM25 floor: about one fairly rare word in common.
MIN_BM25 = 1.5
# The idf counts at least this many pieces, so a workspace with a handful of
# pieces still finds its words (with 1 piece, every idf would be near 0).
MIN_IDF_PIECES = 20
_K1 = 1.2
_B = 0.75


@dataclass(frozen=True)
class Piece:
    """A stored piece and the document it came from."""

    id: UUID
    document_id: UUID
    document_name: str
    text: str
    page: int | None = None
    csv_row: int | None = None


@dataclass(frozen=True)
class Hit:
    """A search result: the piece and its fused score (higher is better)."""

    piece: Piece
    score: float


def fuse(ranked: Sequence[Sequence[UUID]], limit: int) -> list[tuple[UUID, float]]:
    """Reciprocal rank fusion of several best-first id lists."""
    scores: dict[UUID, float] = {}
    for ids in ranked:
        for rank, piece_id in enumerate(ids):
            scores[piece_id] = scores.get(piece_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]


class MemoryIndex:
    """Pieces with their unit embeddings (one row each), searchable."""

    def __init__(self, pieces: Sequence[Piece], vectors: np.ndarray) -> None:
        if len(pieces) != len(vectors):
            raise ValueError("one vector per piece")
        self.pieces = list(pieces)
        self._vectors = np.asarray(vectors, dtype=np.float32)
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths = np.zeros(len(self.pieces), dtype=np.float32)
        for i, piece in enumerate(self.pieces):
            counts = Counter(terms(piece.text))
            self._lengths[i] = sum(counts.values())
            for term, tf in counts.items():
                self._postings.setdefault(term, []).append((i, tf))
        self._avg = float(self._lengths.mean()) if len(self.pieces) else 0.0

    def __len__(self) -> int:
        return len(self.pieces)

    def _bm25(self, query: str) -> list[int]:
        n = max(len(self.pieces), MIN_IDF_PIECES)
        scores: dict[int, float] = {}
        for term in query_terms(query):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, tf in postings:
                norm = _K1 * (1 - _B + _B * self._lengths[i] / (self._avg or 1.0))
                scores[i] = scores.get(i, 0.0) + idf * tf * (_K1 + 1) / (tf + norm)
        best = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [i for i, s in best[:CANDIDATES] if s >= MIN_BM25]

    def _cosine(self, query_vector: np.ndarray | None) -> list[int]:
        if query_vector is None or not len(self.pieces):
            return []
        sims = self._vectors @ np.asarray(query_vector, dtype=np.float32)
        top = np.argsort(-sims)[:CANDIDATES]
        return [int(i) for i in top if sims[i] >= MIN_SIMILARITY]

    def search(
        self, query: str, query_vector: np.ndarray | None, limit: int
    ) -> list[Hit]:
        """The best ``limit`` pieces for ``query``. With no vector (the model
        did not load), keyword search alone."""
        by_id = {p.id: p for p in self.pieces}
        ranked = [
            [self.pieces[i].id for i in self._cosine(query_vector)],
            [self.pieces[i].id for i in self._bm25(query)],
        ]
        return [Hit(by_id[pid], score) for pid, score in fuse(ranked, limit)]
