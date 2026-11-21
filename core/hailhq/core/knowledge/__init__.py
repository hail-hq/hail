"""The knowledge base: documents a workspace uploads, cut into pieces and
embedded, that its agents answer calls, texts and email from.

* ``text``: search words for every language, ID normalization, chunking.
* ``decode``: reads one file in a credential-free child process.
* ``embed``: the local ONNX embedding model (same for ingest and query).
* ``index``: the in-memory hybrid search a call uses.
* ``search``: the same hybrid search in SQL (pgvector + full text).
* ``access``: per-workspace limits and the global size cap.
* ``ingest``: the worker that turns an uploaded file into pieces.

See docs/operations/knowledge-base.md.
"""
