"""Hybrid search in SQL: pgvector plus Postgres full text, fused by rank.

Used by ``POST /v1/knowledge/search``, the text and email reply agents, and
calls whose workspace is too large to hold in memory
(``access.MEMORY_MAX_PIECES``). Same shape as the in-memory search
(``index``):

* Meaning: the ``CANDIDATES`` nearest pieces by cosine distance
  (``<=>``) among those embedded by the current model, kept when the
  similarity clears ``index.min_similarity``. The HNSW index
  (``knowledge_pieces_embedding_idx``) filters after its scan, so the
  statement turns on pgvector 0.8.0's ``hnsw.iterative_scan``: the scan
  goes on until enough rows pass the organization and model filters.
  Pieces with no current vector (just added, or of an earlier model) are
  found by words only.
* Words: ``knowledge_pieces.terms`` holds each piece's ``text.terms`` as
  lexemes (``array_to_tsvector``: no parser, so Chinese bigrams and IDs
  are kept as they are). A piece scores the sum of the inverse document
  frequency (the BM25 idf, counting at least ``index.MIN_IDF_PIECES``
  pieces) of the question's words it holds; kept when it clears
  ``MIN_KEYWORD_SCORE``.
* The two lists are merged with reciprocal rank fusion (``index.fuse``).

Every statement filters on the organization id.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import numpy as np
from hailhq.core.knowledge.embed import DIMS
from hailhq.core.knowledge.index import (
    CANDIDATES,
    MIN_IDF_PIECES,
    Hit,
    Piece,
    fuse,
    min_similarity,
)
from hailhq.core.knowledge.text import query_terms
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["MIN_KEYWORD_SCORE", "search_pieces", "vector_literal"]

# About one fairly rare word in common (no term counts in SQL).
MIN_KEYWORD_SCORE = 1.5

# strict_order: results stay in exact distance order.
_ITERATIVE_SCAN = text("SET LOCAL hnsw.iterative_scan = strict_order")

_NEAREST = text(f"""
    SELECT id, 1 - (embedding <=> CAST(:vector AS halfvec({DIMS}))) AS similarity
    FROM knowledge_pieces
    WHERE organization_id = :org AND embedding_model = :model
    ORDER BY embedding <=> CAST(:vector AS halfvec({DIMS}))
    LIMIT :limit
    """)

_KEYWORDS = text("""
    WITH q AS (
        SELECT CAST(quote_literal(t) AS tsquery) AS tq
        FROM unnest(CAST(:terms AS text[])) AS t
    ),
    total AS (
        SELECT greatest(count(*), :min_pieces) AS n
        FROM knowledge_pieces WHERE organization_id = :org
    ),
    df AS (
        SELECT q.tq, (
            SELECT count(*) FROM knowledge_pieces p
            WHERE p.organization_id = :org AND p.terms @@ q.tq
        ) AS n
        FROM q
    )
    SELECT p.id,
           sum(ln(1 + (total.n - df.n + 0.5) / (df.n + 0.5))) AS score
    FROM knowledge_pieces p
    JOIN df ON df.n > 0 AND p.terms @@ df.tq
    CROSS JOIN total
    WHERE p.organization_id = :org
    GROUP BY p.id
    ORDER BY score DESC
    LIMIT :limit
    """)

_PIECES = text("""
    SELECT p.id, p.document_id, d.name, p.text, p.page, p.csv_row
    FROM knowledge_pieces p
    JOIN knowledge_documents d ON d.id = p.document_id
    WHERE p.organization_id = :org
      AND d.organization_id = :org
      AND p.id = ANY(CAST(:ids AS uuid[]))
    """)


def vector_literal(vector: Sequence[float] | np.ndarray) -> str:
    """pgvector's text form of a vector."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


async def search_pieces(
    db: AsyncSession,
    organization_id: UUID,
    query: str,
    query_vector: np.ndarray | None,
    limit: int,
    *,
    model: str | None = None,
) -> list[Hit]:
    """The workspace's best ``limit`` pieces for ``query``. ``query_vector``
    was made by ``model`` and is compared only with pieces of that model.
    With no vector (the embedding API failed or is not set up), keyword
    search alone."""
    ranked: list[list[UUID]] = []
    if query_vector is not None and model:
        await db.execute(_ITERATIVE_SCAN)
        rows = await db.execute(
            _NEAREST,
            {
                "vector": vector_literal(query_vector),
                "org": organization_id,
                "model": model,
                "limit": CANDIDATES,
            },
        )
        floor = min_similarity()
        ranked.append([r.id for r in rows if r.similarity >= floor])
    words = query_terms(query)
    if words:
        rows = await db.execute(
            _KEYWORDS,
            {
                "terms": words,
                "org": organization_id,
                "limit": CANDIDATES,
                "min_pieces": MIN_IDF_PIECES,
            },
        )
        ranked.append([r.id for r in rows if r.score >= MIN_KEYWORD_SCORE])
    best = fuse(ranked, limit)
    if not best:
        return []
    rows = await db.execute(
        _PIECES, {"org": organization_id, "ids": [pid for pid, _ in best]}
    )
    pieces = {
        r.id: Piece(
            id=r.id,
            document_id=r.document_id,
            document_name=r.name,
            text=r.text,
            page=r.page,
            csv_row=r.csv_row,
        )
        for r in rows
    }
    return [Hit(pieces[pid], score) for pid, score in best if pid in pieces]
