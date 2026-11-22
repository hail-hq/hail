"""The knowledge base during a call: load once, search every turn.

When a call starts, ``CallKnowledge.start`` checks in the database that the
agent has ``knowledge`` on and that its workspace has the knowledge base
turned on (never trusting the dispatch metadata), then loads the
workspace's pieces and embeddings and the embedding model, in the
background while the greeting plays. A workspace with more than
``access.MEMORY_MAX_PIECES`` pieces is searched in SQL instead of memory.

On each caller turn ``SpeechSanitizingAgent.llm_node`` asks ``context_for``
with the latest caller transcript, before the model call (not as a tool):
hybrid search (cosine + BM25, fused by rank) and the top
``CALL_PIECES`` pieces, wrapped as untrusted reference material
(``prompts.knowledge_section``), are added to that one model call only, so
pieces never pile up in the chat history. A turn never waits for the load:
until it ends, turns go without the knowledge base.

The model loads in the call's own process, only for calls that use the
knowledge base: it holds about 460 to 500 MB once loaded, too much to keep
in every idle call process (docs/operations/knowledge-base.md).

Fails open: any error in the load or a search logs a warning (no text, no
names) and the call goes on without the knowledge base.
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
from hailhq.core.knowledge.embed import Embedder, get_embedder
from hailhq.core.knowledge.index import Hit, MemoryIndex, Piece
from hailhq.core.knowledge.search import search_pieces
from hailhq.core.knowledge.text import query_terms, terms
from hailhq.core.models import KnowledgeDocument, KnowledgePiece
from hailhq.core.prompts import knowledge_section
from sqlalchemy import Text, cast, func, select

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
        excuse me let think one moment second wait really umm mhm hm eh huh
        sì grazie mille prego va bene perfetto ciao
        sí gracias muchas muy bien vale bueno claro adiós hola
        oui non merci beaucoup très bien d'accord bon salut
        ja nein danke dank vielen schön bitte gut genau tschüss hallo
        sim não obrigado obrigada muito tudo bem tchau olá""")
)
# Latin, full-width, Spanish, Arabic, Greek and Armenian marks. Greek
# writes its question mark as ";" (U+037E normalizes to it), so ";" counts
# only in Greek text.
# Words that ask the agent to repeat itself ("What?", "¿Qué?"), never a
# question about the business: not counted.
_REPEAT = frozenset(terms("what was qué quoi cosa quê como wie"))
_QUESTION_MARKS = "?？¿؟\u037e՞"


def _asks(text: str) -> bool:
    return any(q in text for q in _QUESTION_MARKS) or (
        ";" in text and any("GREEK" in unicodedata.name(c, "") for c in text)
    )


def worth_searching(text: str, *, call: bool = False) -> bool:
    """Whether a message is worth a search. Any ID (a word with a digit) is;
    a message of only filler words ("okay, thank you", "I'm fine",
    "Wait, what?") is not. Otherwise a text or email needs two search
    words. A call turn needs two words that are not filler, or three spoken
    words, or one word with a question mark ("Price?", "价格？")."""
    words = query_terms(text)
    if any(any(c.isdigit() for c in w) for w in words):
        return True
    meaning = [w for w in words if w not in _FILLER and w not in _REPEAT]
    if not meaning:
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
                                cast(KnowledgePiece.embedding, Text).label("embedding"),
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
            # No model: the call keeps keyword search, like texts and email.
            embedder = await asyncio.to_thread(get_embedder)
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
        try:
            vector = (
                await asyncio.to_thread(self._embedder.embed_query, text)
                if self._embedder is not None
                else None
            )
            if self._index is not None:
                hits = await asyncio.to_thread(
                    self._index.search, text, vector, CALL_PIECES
                )
            else:
                async with session_scope() as db:
                    hits = await search_pieces(
                        db, self.organization_id, text, vector, CALL_PIECES
                    )
            section = pieces_section(hits)
        except Exception as exc:
            logger.warning("knowledge search failed (%s)", type(exc).__name__)
            return None
        self._last = (text, section)
        return section


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
    vectors = np.asarray(
        [r.embedding.strip("[]").split(",") for r in rows], dtype=np.float32
    )
    return MemoryIndex(pieces, vectors)


async def reply_knowledge(
    organization_id: UUID, agent_id: UUID, message: str
) -> str | None:
    """The reference block for a text or email reply to ``message``: SQL
    hybrid search, run in the text worker's process (the model loads there
    once, on first use). None when the agent or workspace does not use the
    knowledge base, nothing matches, or anything fails (logged)."""
    query = message.strip()[:_QUERY_CHARS]
    if not worth_searching(query):
        return None
    try:
        async with session_scope() as db:
            if not await agent_uses_knowledge(db, organization_id, agent_id):
                return None
        embedder = await asyncio.to_thread(get_embedder)
        vector = (
            await asyncio.to_thread(embedder.embed_query, query) if embedder else None
        )
        async with session_scope() as db:
            hits = await search_pieces(db, organization_id, query, vector, REPLY_PIECES)
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
