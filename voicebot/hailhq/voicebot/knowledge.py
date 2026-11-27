"""The knowledge base during a call: load once, search every turn.

When a call starts, ``CallKnowledge.start`` checks in the database that the
agent has ``knowledge`` on and that its workspace has the knowledge base
turned on (never trusting the dispatch metadata), then loads the
workspace's pieces and the vectors of the configured embedding model, in
the background while the greeting plays. A workspace with more than
``access.MEMORY_MAX_PIECES`` pieces is searched in SQL instead of memory.

On each caller turn ``SpeechSanitizingAgent.llm_node`` asks ``context_for``
with the latest caller transcript, before the model call (not as a tool):
hybrid search (cosine + BM25, fused by rank) and the top
``CALL_PIECES`` pieces, wrapped as untrusted reference material
(``prompts.knowledge_section``), are added to that one model call only, so
pieces never pile up in the chat history. A turn never waits for the load:
until it ends, turns go without the knowledge base.

The question's vector comes from the hosted embedding API (0.2 to 0.5 s).
LiveKit's preemptive generation (on by default) runs ``llm_node`` on the
caller's words as soon as the speech-to-text has them, while the session
still waits to confirm the end of the turn, so the request starts during
that wait. Each request runs in its own task, keyed by the text
(``_vector_for``): a cancelled early reply does not cancel it, and the
confirmed turn with the same words reuses it; other words get a new
request. A turn waits at most ``CALL_EMBED_WAIT`` for it, then searches by
keywords alone.

Fails open: any error in the load, the embedding API or a search logs a
warning (no text, no names) and the call goes on, with keyword search or
without the knowledge base.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import Any
from uuid import UUID

import numpy as np
from hailhq.core.db import session_scope
from hailhq.core.knowledge.access import MEMORY_MAX_PIECES, agent_uses_knowledge
from hailhq.core.knowledge.embed import (
    DIMS,
    QUERY_TIMEOUT,
    Embedder,
    EmbeddingUnavailable,
    get_embedder,
)
from hailhq.core.knowledge.index import Hit, MemoryIndex, Piece
from hailhq.core.knowledge.search import search_pieces
from hailhq.core.knowledge.text import query_terms, terms
from hailhq.core.models import KnowledgeDocument, KnowledgePiece
from hailhq.core.prompts import knowledge_section
from sqlalchemy import Text, case, cast, func, select

__all__ = [
    "CALL_PIECES",
    "REPLY_PIECES",
    "CallKnowledge",
    "last_user_text",
    "reply_knowledge",
    "with_knowledge",
    "worth_searching",
]

logger = logging.getLogger("hailhq.voicebot")

# Pieces added to a call's turn: few, since every token slows the reply.
CALL_PIECES = 3
# Pieces added to a text or email reply, which has more time.
REPLY_PIECES = 5
# The most of a message used as the search question.
_QUERY_CHARS = 1000
# Longest a call turn waits for its question's vector before it searches
# by keywords alone. The request itself may run on (``QUERY_TIMEOUT``) and
# serve the same words again.
CALL_EMBED_WAIT = 1.0
# Question vectors kept per call, newest last.
_CACHED_VECTORS = 8


# Words that make up filler turns ("okay, thank you", "no, that's all"). A
# message made only of these is not searched. A few of the most spoken
# languages.
# Written as people say them; folded the way search words are (accents
# off, lower case, "that's" is "that" and "s").
_FILLER = frozenset(
    terms("""yes yeah yep no nope ok okay alright right sure fine good great
        thanks thank-you you very much so please hello hi hey bye goodbye
        that's all it is oh ah um uh uh-huh mm-hmm hmm perfect i see got too
        have a nice day i'm we're you're i've i'll i'd don't sorry pardon
        excuse me let think umm mhm hm eh huh
        sì grazie mille prego va bene perfetto ciao
        sí gracias muchas muy bien vale bueno claro adiós hola
        oui non merci beaucoup très bien d'accord bon salut
        ja nein danke dank vielen schön bitte gut genau tschüss hallo
        sim não obrigado obrigada muito tudo bem tchau olá""")
)
# Words that only hold the line or ask for a repeat ("one second", "wait,
# what?", "¿Qué?"). A turn whose other words are all filler is not
# searched; in any other turn they count as words ("Wait times").
_HOLD = frozenset(
    terms(
        "one second sec wait really moment hold on hang just bear with give "
        "what was qué quoi cosa quê como wie"
    )
)
# Latin, full-width, Spanish, Arabic, Greek and Armenian marks. Greek
# writes its question mark as ";" (U+037E normalizes to it), so ";" counts
# only in Greek text.
_QUESTION_MARKS = "?？¿؟\u037e՞"


def _asks(text: str) -> bool:
    return any(q in text for q in _QUESTION_MARKS) or (
        ";" in text and any("GREEK" in unicodedata.name(c, "") for c in text)
    )


def worth_searching(text: str, *, call: bool = False) -> bool:
    """Whether a message is worth a search. Any ID (a word with a digit) is;
    a message of only filler and hold words ("okay, thank you", "I'm fine",
    "Wait, what?", "One second") is not. Otherwise a text or email needs two
    search words. A call turn needs two non-filler words, or three spoken
    words, or one word with a question mark ("Price?", "价格？")."""
    words = query_terms(text)
    if any(any(c.isdigit() for c in w) for w in words):
        return True
    meaning = [w for w in words if w not in _FILLER]
    if all(w in _HOLD for w in meaning):
        return False
    if not call:
        return len(words) >= 2
    spoken = [w for w in text.split() if any(c.isalnum() for c in w)]
    return len(meaning) >= 2 or len(spoken) >= 3 or _asks(text)


def last_user_text(chat_ctx: Any) -> str:
    """The text of the latest caller message in ``chat_ctx``."""
    for item in reversed(list(getattr(chat_ctx, "items", []))):
        if getattr(item, "type", None) == "message" and item.role == "user":
            return (item.text_content or "").strip()
    return ""


def pieces_section(hits: list[Hit]) -> str | None:
    return knowledge_section(
        [
            (h.piece.document_name, h.piece.page, h.piece.csv_row, h.piece.text)
            for h in hits
        ]
    )


class CallKnowledge:
    """One call's knowledge base. ``start`` it once; ``context_for`` is
    safe to call at any time and never raises."""

    def __init__(self, organization_id: UUID, agent_id: UUID) -> None:
        self.organization_id = organization_id
        self.agent_id = agent_id
        self._task: asyncio.Task | None = None
        self._embedder: Embedder | None = None
        self._index: MemoryIndex | None = None
        self._in_sql = False
        self._last: tuple[str, str | None] | None = None
        self._vectors: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._load(), name="knowledge-load")

    @property
    def ready(self) -> bool:
        return (
            self._task is not None
            and self._task.done()
            and (self._index is not None or self._in_sql)
        )

    async def _load(self) -> None:
        try:
            async with session_scope() as db:
                if not await agent_uses_knowledge(
                    db, self.organization_id, self.agent_id
                ):
                    return
                embedder = get_embedder()
                model = embedder.model if embedder is not None else None
                count = (
                    await db.execute(
                        select(func.count())
                        .select_from(KnowledgePiece)
                        .where(KnowledgePiece.organization_id == self.organization_id)
                    )
                ).scalar_one()
                if not count:
                    return
                rows = []
                if count <= MEMORY_MAX_PIECES:
                    rows = (
                        await db.execute(
                            select(
                                KnowledgePiece.id,
                                KnowledgePiece.document_id,
                                KnowledgeDocument.name,
                                KnowledgePiece.text,
                                KnowledgePiece.page,
                                KnowledgePiece.csv_row,
                                # Text: parsed in a thread, not on the loop.
                                # Only vectors of the configured model.
                                case(
                                    (
                                        KnowledgePiece.embedding_model == model,
                                        cast(KnowledgePiece.embedding, Text),
                                    ),
                                    else_=None,
                                ).label("embedding"),
                            )
                            .join(
                                KnowledgeDocument,
                                KnowledgeDocument.id == KnowledgePiece.document_id,
                            )
                            .where(
                                KnowledgePiece.organization_id == self.organization_id,
                                KnowledgeDocument.organization_id
                                == self.organization_id,
                            )
                        )
                    ).all()
            # No embedding API: the call keeps keyword search, like texts
            # and email.
            if rows:
                self._index = await asyncio.to_thread(_build_index, rows)
            else:
                self._in_sql = True
            self._embedder = embedder
            logger.info(
                "knowledge base loaded: %d pieces%s",
                count,
                " (SQL)" if self._in_sql else "",
            )
        except Exception as exc:
            self._index, self._in_sql = None, False
            logger.warning(
                "knowledge base not loaded (%s); the call goes on without it",
                type(exc).__name__,
            )

    async def context_for(self, text: str) -> str | None:
        """The reference block for the caller's ``text``, or None: not
        loaded yet, nothing worth searching, nothing found, or an error."""
        if not self.ready or not text or not worth_searching(text, call=True):
            return None
        if self._last is not None and self._last[0] == text:
            # The same turn again (a tool round, or the preemptive reply
            # and the real one): no second search.
            return self._last[1]
        vector = await self._vector_for(text)
        try:
            if self._index is not None:
                hits = await asyncio.to_thread(
                    self._index.search, text, vector, CALL_PIECES
                )
            else:
                async with session_scope() as db:
                    hits = await search_pieces(
                        db,
                        self.organization_id,
                        text,
                        vector,
                        CALL_PIECES,
                        model=self._embedder.model if self._embedder else None,
                    )
            section = pieces_section(hits)
        except Exception as exc:
            logger.warning("knowledge search failed (%s)", type(exc).__name__)
            return None
        if vector is not None or self._embedder is None:
            # A keyword-only answer is not kept: the same words again may
            # have their vector by then.
            self._last = (text, section)
        return section

    async def _vector_for(self, text: str) -> np.ndarray | None:
        """The question's vector, or None (keyword search) on an error or
        after ``CALL_EMBED_WAIT``. One request per distinct text: an early
        (preemptive) turn and the confirmed one with the same words share
        it, and cancelling the early turn leaves it running."""
        if self._embedder is None:
            return None
        task = self._vectors.get(text)
        if (
            task is not None
            and task.done()
            and (task.cancelled() or task.exception() is not None)
        ):
            task = None  # Failed before: ask again.
        if task is None:
            task = asyncio.create_task(
                self._embedder.embed_query(text, timeout=QUERY_TIMEOUT),
                name="knowledge-embed",
            )
            task.add_done_callback(_consume)
            self._vectors[text] = task
            while len(self._vectors) > _CACHED_VECTORS:
                self._vectors.pop(next(iter(self._vectors)))
        try:
            return await asyncio.wait_for(asyncio.shield(task), CALL_EMBED_WAIT)
        except asyncio.TimeoutError:
            logger.warning(
                "knowledge: no question vector within %.1f s; keyword search",
                CALL_EMBED_WAIT,
            )
        except EmbeddingUnavailable as exc:
            logger.warning("knowledge: embedding API failed (%s); keyword search", exc)
        except Exception as exc:
            logger.warning(
                "knowledge: question vector failed (%s); keyword search",
                type(exc).__name__,
            )
        return None


def _consume(task: asyncio.Task) -> None:
    """Read a finished embedding task's error, so a request nobody awaits
    any more never logs "exception was never retrieved"."""
    if not task.cancelled():
        task.exception()


def _build_index(rows) -> MemoryIndex:
    pieces = [
        Piece(
            id=r.id,
            document_id=r.document_id,
            document_name=r.name,
            text=r.text,
            page=r.page,
            csv_row=r.csv_row,
        )
        for r in rows
    ]
    # A zero row for a piece with no vector of the configured model (the
    # query left it out): found by keywords only.
    vectors = np.zeros((len(rows), DIMS), dtype=np.float32)
    for i, r in enumerate(rows):
        if r.embedding:
            vectors[i] = np.asarray(r.embedding.strip("[]").split(","), np.float32)
    return MemoryIndex(pieces, vectors)


async def reply_knowledge(
    organization_id: UUID, agent_id: UUID, message: str
) -> str | None:
    """The reference block for a text or email reply to ``message``: SQL
    hybrid search. Keyword search alone when the embedding API fails
    (logged). None when the agent or workspace does not use the knowledge
    base, nothing matches, or anything else fails (logged)."""
    query = message.strip()[:_QUERY_CHARS]
    if not worth_searching(query):
        return None
    try:
        async with session_scope() as db:
            if not await agent_uses_knowledge(db, organization_id, agent_id):
                return None
        embedder = get_embedder()
        vector = None
        if embedder is not None:
            try:
                vector = await embedder.embed_query(query)
            except Exception as exc:
                logger.warning(
                    "knowledge: embedding API failed (%s); keyword search",
                    (
                        exc
                        if isinstance(exc, EmbeddingUnavailable)
                        else type(exc).__name__
                    ),
                )
        async with session_scope() as db:
            hits = await search_pieces(
                db,
                organization_id,
                query,
                vector,
                REPLY_PIECES,
                model=embedder.model if embedder else None,
            )
        return pieces_section(hits)
    except Exception as exc:
        logger.warning("knowledge search for a reply failed (%s)", type(exc).__name__)
        return None


def with_knowledge(messages: list[dict[str, Any]], section: str | None) -> list[dict]:
    """``messages`` with the reference block as a second system message,
    right after the instructions (every provider takes it there)."""
    if not section:
        return messages
    return [messages[0], {"role": "system", "content": section}, *messages[1:]]
