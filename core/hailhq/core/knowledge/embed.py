"""The knowledge base's embedding model: a small multilingual ONNX model run
on the CPU, the same for ingest (passages) and search (questions).

A remote embedding API adds a network round trip to every caller turn, so
the model runs in the process that needs it: each voicebot call process
loads it once in prewarm, the voicebot's text worker and the API load it on
first use. The model files are baked into the Docker images
(``hail_embedding_model_dir``); nothing is downloaded at call time.

Model: ``intfloat/multilingual-e5-small`` (MIT), its own int8 ONNX export
(``onnx/model_qint8_avx512_vnni.onnx``, saved as ``model.onnx``), 384
dimensions, about 100 languages, inputs up to 512 tokens. Chosen over
``paraphrase-multilingual-MiniLM-L12-v2``, which reads only the first 128
tokens of a piece. E5 wants
``query: `` before a question and ``passage: `` before a document piece.
Mean pooling over the attention mask, then L2 normalization, so a dot
product is the cosine similarity. Runtime: onnxruntime (MIT) and
SentencePiece (Apache-2.0), both AGPL-compatible. The model choice and
its benchmark are in docs/operations/knowledge-base.md.

One intra-op thread: the voicebot runs live calls side by side on a few
CPUs, and one short question takes a few milliseconds on one thread.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
import sentencepiece
from hailhq.core.config import settings

__all__ = [
    "DIMS",
    "MAX_TOKENS",
    "Embedder",
    "EmbedderUnavailable",
    "get_embedder",
    "load_embedder",
]

logger = logging.getLogger(__name__)

DIMS = 384
MAX_TOKENS = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "sentencepiece.bpe.model"
# XLM-RoBERTa ids around the SentencePiece ones (fairseq layout): every
# piece id is shifted by one, SentencePiece's unknown (0) is 3.
_BOS, _PAD, _EOS, _UNK = 0, 1, 2, 3
# Passages per model run at ingest. Small, so one batch never holds a CPU
# long while calls run beside it.
BATCH = 8


class EmbedderUnavailable(RuntimeError):
    """The model files are missing or the model failed to load."""


class Embedder:
    """One loaded model. Thread-safe: an onnxruntime session and a
    SentencePiece processor may be used from several threads at once."""

    def __init__(self, model_dir: str | Path, *, threads: int = 1) -> None:
        folder = Path(model_dir)
        model, vocab = folder / MODEL_FILE, folder / TOKENIZER_FILE
        if not model.is_file() or not vocab.is_file():
            raise EmbedderUnavailable(f"no embedding model in {folder}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        # No arena: measured about 450 MB lower peak at ingest, no slower.
        options.enable_cpu_mem_arena = False
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(model), options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}
        # SentencePiece, not Hugging Face's tokenizer.json: the same ids
        # (checked on 112 multilingual texts; only a trailing space differs)
        # for about 280 MB less memory per process.
        self._sp = sentencepiece.SentencePieceProcessor(model_file=str(vocab))

    def count_tokens(self, text: str) -> int:
        """Model tokens in ``text``, without the special ones."""
        return len(self._sp.encode(text))

    def _ids(self, text: str) -> list[int]:
        pieces = [p + 1 if p else _UNK for p in self._sp.encode(text)]
        # Keep the first tokens that fit with the two special ones.
        return [_BOS, *pieces[: MAX_TOKENS - 2], _EOS]

    def _run(self, texts: list[str]) -> np.ndarray:
        rows = [self._ids(t) for t in texts]
        width = max(len(r) for r in rows)
        ids = np.full((len(rows), width), _PAD, dtype=np.int64)
        mask = np.zeros((len(rows), width), dtype=np.int64)
        for i, row in enumerate(rows):
            ids[i, : len(row)] = row
            mask[i, : len(row)] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feed)[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.maximum(norms, 1e-12)).astype(np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        """One question, as a unit vector of ``DIMS`` floats."""
        return self._run([QUERY_PREFIX + text])[0]

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        """Document pieces, one unit vector per row, ``BATCH`` at a time."""
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        out = [
            self._run([PASSAGE_PREFIX + t for t in texts[i : i + BATCH]])
            for i in range(0, len(texts), BATCH)
        ]
        return np.concatenate(out)


_lock = threading.Lock()
_embedder: Embedder | None = None


def load_embedder() -> Embedder:
    """The process's model, loaded on first call. Raises
    ``EmbedderUnavailable`` when it cannot load (and tries again next time)."""
    global _embedder
    if _embedder is not None:
        return _embedder
    with _lock:
        if _embedder is None:
            try:
                _embedder = Embedder(settings.hail_embedding_model_dir)
            except EmbedderUnavailable:
                raise
            except Exception as exc:
                raise EmbedderUnavailable(type(exc).__name__) from exc
    return _embedder


def get_embedder() -> Embedder | None:
    """The process's model, or None (with a warning) when it cannot load.
    For callers that fail open."""
    try:
        return load_embedder()
    except EmbedderUnavailable as exc:
        logger.warning("knowledge base: embedding model unavailable (%s)", exc)
        return None
