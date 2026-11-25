# Knowledge base (operations)

Customers upload documents; agents answer calls, texts and email from them. Public docs: [agents.md](../public/agents.md#answer-from-your-documents-knowledge-base). Code: [`core/hailhq/core/knowledge/`](../../core/hailhq/core/knowledge/), [`api/hailhq/api/routes/knowledge.py`](../../api/hailhq/api/routes/knowledge.py), [`voicebot/hailhq/voicebot/knowledge.py`](../../voicebot/hailhq/voicebot/knowledge.py).

## Turn it on for a paying workspace

Nothing is free: every workspace is off until it has an `organization_knowledge` row. Run from the repo root of the main checkout, with the production `DATABASE_URL` from its `.env`:

```bash
set -a; . ./.env; set +a
psql "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -c \
  "INSERT INTO organization_knowledge (organization_id) VALUES ('<org-uuid>');"
```

The defaults are the early-access limits: 10 documents, 20 MB per file (`20971520`), 1,000 pieces. Raise one:

```bash
psql "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -c \
  "UPDATE organization_knowledge SET max_documents = 50, updated_at = now() WHERE organization_id = '<org-uuid>';"
```

Planned: when paid plans ship, the limits move from a document count to total size (Pro 100 MB, Business 1 GB, 20 MB per file). The table holds no total size yet; the per-file default is already 20 MB.

Allowed ranges (CHECK constraints): documents 1 to 1,000, file bytes up to 25 MB, pieces up to 100,000. One file gives at most 1,500 pieces whatever the workspace limit. The workspace total can be higher. Over 5,000 pieces, calls search in SQL instead of memory.

Turn it off (agents stop using it at once; adding and search answer 403; listing, reading and deleting still work):

```bash
psql "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -c \
  "DELETE FROM organization_knowledge WHERE organization_id = '<org-uuid>';"
```

Delete everything (files, pieces and the limits row), for example when a plan ends. One function does every full delete (`delete_org_knowledge`; account closure already runs it). By hand, on the VM, from the deploy directory (where `docker-compose.prod.yml` is):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm api python -m hailhq.core.knowledge.access delete '<org-uuid>'
```

If it fails, run the same command again before turning the workspace on; it is safe to repeat. The last step sweeps files no row points to, only while the workspace is off.

## Before the first workspace is turned on

Apply the Terraform change that grants the API read, write, delete and list on `knowledge-documents/` in the mail bucket, from the repo root of the main checkout after merge:

```bash
cd infra && terragrunt apply
```

Without it, uploads answer 503 and stored files are refused with 403, which fails their documents after 3 tries.

## What runs where

| Step                                  | Process                      | Notes                                                                                    |
| ------------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------- |
| Upload link and PUT                   | API                          | `knowledge-documents/<org>/<id>.<attempt>` in the mail bucket; type sniffed at PUT       |
| Ingest (`pending` → `ready`/`failed`) | API, `KnowledgeIngestWorker` | Decode in a child process (no credentials, 30 s CPU, 1 GB, 60 s); chunk; store pieces    |
| Embedding pass                        | API, `KnowledgeIngestWorker` | Every tick: pieces with no vector of the configured model, 100 per API request           |
| Search API, text and email replies    | API, voicebot text worker    | SQL: pgvector cosine (HNSW) + idf over `terms`, fused by rank                            |
| Calls                                 | Each call process            | Loads pieces + vectors during the greeting; top 3 pieces per turn, before the model call |

The worker polls every 5 s (`HAIL_KNOWLEDGE_POLL_SECONDS`, code default; 0 turns it off). Untouched uploads are deleted after 24 hours (upload sweep). A document not ready one day after it was added fails. A workspace with more than 5,000 pieces is searched in SQL during calls instead of in memory.

## Embedding model

A hosted, OpenAI-compatible `/embeddings` API ([`embed.py`](../../core/hailhq/core/knowledge/embed.py)). Settings (code defaults, nothing in `.env`):

- `HAIL_EMBEDDING_BASE_URL`: `https://generativelanguage.googleapis.com/v1beta/openai` (Gemini). Requests go to `<base>/embeddings`.
- `HAIL_EMBEDDING_API_KEY`: empty, so `GOOGLE_API_KEY` is used while the base URL is Google's. A Google key is never sent to another host. No key at all: keyword search only, one warning per process.
- `HAIL_EMBEDDING_MODEL`: `gemini-embedding-001`.
- `HAIL_EMBEDDING_DIMENSIONS`: `1536`, the width of `knowledge_pieces.embedding` (`halfvec(1536)`).
- `HAIL_EMBEDDING_MIN_SIMILARITY`: `0.5`, the cosine floor for a piece found by meaning only. Set it with the model (table below).

The request is `{"model", "input", "dimensions"}`. Every vector is L2-normalized in our code, whatever the provider (Gemini returns unnormalized vectors below 3,072 dimensions). Gemini's OpenAI-compatible endpoint takes at most 100 texts per request and refuses any other field (a task type such as `RETRIEVAL_QUERY` answers 400), so none is sent. Input over 2,048 tokens is cut by Gemini without an error; pieces stay far under it (about 500 estimated tokens, CSV rows at most 2,000 characters).

Measured 2026-11-24 with 6 invented manual sections and 14 questions in 7 languages, 1536 dimensions:

| Model                    | Endpoint                                                     | Right piece first | Right piece cosine | Off-topic best | Floor to set |
| ------------------------ | ------------------------------------------------------------ | ----------------- | ------------------ | -------------- | ------------ |
| `gemini-embedding-001`   | `generativelanguage.googleapis.com/v1beta/openai/embeddings` | 9 of 10           | 0.61 to 0.82       | 0.45 to 0.48   | 0.5          |
| `text-embedding-3-small` | `api.openai.com/v1/embeddings`                               | 10 of 10          | 0.29 to 0.70       | 0.05 to 0.17   | 0.2          |
| `text-embedding-3-large` | `api.openai.com/v1/embeddings`                               | 9 of 10           | 0.33 to 0.66       | 0.03 to 0.21   | 0.2          |

One request takes 0.3 to 0.5 s from Europe. A question in one language finds an English piece with no word in common.

### Where the latency goes in a call

LiveKit's preemptive generation (on by default) runs the reply on the caller's words as soon as speech-to-text has them, while the session still waits to confirm the end of the turn. The question's embedding request starts there, in its own task keyed by the text. The confirmed turn with the same words reuses it, even when the early reply was cancelled. A turn waits at most 1 s for the vector, then searches by keywords alone.

### Changing the model or the provider

1. Add `HAIL_EMBEDDING_BASE_URL`, `HAIL_EMBEDDING_API_KEY` (unless Gemini), `HAIL_EMBEDDING_MODEL` and `HAIL_EMBEDDING_MIN_SIMILARITY` with the new values to `.env.example` and `.env` (same lines in both), copy `.env` to the server, and restart the API and the voicebot.
2. Searches compare only vectors of the new model (`knowledge_pieces.embedding_model`). Until a piece is re-embedded, keywords still find it.
3. The ingest worker re-embeds every piece, 100 per request, at most 1,000 per tick. Watch it:

```sql
SELECT embedding_model, count(*) FROM knowledge_pieces GROUP BY 1;
```

Another width needs a migration (column type and HNSW index) plus a re-embed; until then the API logs a warning and searches by keywords only.

### Failures

An API error or timeout never breaks a call, a text or an email reply: that search uses keywords and logs a warning (status code or exception type, never text). Ingest never fails a document for it: pieces are `ready` and keyword-searchable at once; the embedding pass waits (the server's `Retry-After`, or 30 s doubling up to 10 minutes) and tries again.

A batch the API refuses (400, 413, 422) is checked with a short probe text. A refused probe means the setting (key, model, width): the pass waits. Otherwise each piece is sent alone, and a piece refused alone gets `embedding_refused` set to the model and stays keyword-only for a day (then it is tried again) or until the model changes. Each batch takes every workspace's pieces in turn. To try refused pieces again under the same model:

```sql
UPDATE knowledge_pieces SET embedding_refused = NULL, embedding_refused_at = NULL
WHERE embedding_refused IS NOT NULL;
```

The embedding pass never checks the size cap: every piece's vector is counted when its document is admitted, and the cap counts live rows only, so new vectors and a model change always finish.

Search filters on the organization and the model; the HNSW index filters after its scan, so each nearest-piece query sets `hnsw.iterative_scan = strict_order` (pgvector 0.8.0 or later). Local Docker and CI use `pgvector/pgvector:0.8.0-pg16`.

A message made only of filler words ("okay, thank you", "Sorry?") is not searched. Hold-the-line turns ("one second", "wait, what?") are not searched either. Otherwise a call turn needs two non-filler words, three spoken words, or one word with a question mark; a text or email needs two words. Any ID is always searched.

## Limits and alerts

- All knowledge data together is capped at 300 MB of live data, counted by Hail from the current rows, not the tables' size on disk: each piece's text and search words, plus 6 KB for its vector (about 3 KB of `halfvec(1536)` and as much in the HNSW index, counted whether or not the vector is written yet), plus 256 bytes of row and index overhead. Each document keeps its total in `knowledge_documents.stored_bytes`. Dead rows and table bloat do not count, so replacing vectors (a model change) never moves it. Past it, new links and documents answer 409, and the worker refuses new pieces.
- The abuse monitor emails `HAIL_SUPPORT_EMAIL` at most once a day while `pg_database_size` is over 200 MB (state in `platform_flags`, key `database_size_alert_day`). Check both:

```sql
SELECT pg_size_pretty(pg_database_size(current_database())) AS database,
       pg_size_pretty((SELECT coalesce(sum(stored_bytes), 0)
                       FROM knowledge_documents)) AS knowledge_live;
```
