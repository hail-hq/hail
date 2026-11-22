# Knowledge base (operations)

Customers upload documents; agents answer calls, texts and email from them. Public docs: [agents.md](../public/agents.md#answer-from-your-documents-knowledge-base). Code: [`core/hailhq/core/knowledge/`](../../core/hailhq/core/knowledge/), [`api/hailhq/api/routes/knowledge.py`](../../api/hailhq/api/routes/knowledge.py), [`voicebot/hailhq/voicebot/knowledge.py`](../../voicebot/hailhq/voicebot/knowledge.py).

## Turn it on for a paying workspace

Nothing is free: every workspace is off until it has an `organization_knowledge` row. Run from the repo root of the main checkout, with the production `DATABASE_URL` from its `.env`:

```bash
set -a; . ./.env; set +a
psql "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -c \
  "INSERT INTO organization_knowledge (organization_id) VALUES ('<org-uuid>');"
```

The defaults are the early-access limits: 10 documents, 10 MB per file (`10485760`), 1,000 pieces. Raise one:

```bash
psql "${DATABASE_URL/postgresql+psycopg:/postgresql:}" -c \
  "UPDATE organization_knowledge SET max_documents = 50, updated_at = now() WHERE organization_id = '<org-uuid>';"
```

Allowed ranges (CHECK constraints): documents 1 to 1,000, file bytes up to 25 MB, pieces up to 100,000. One file gives at most 1,500 pieces whatever the workspace limit: it must be read within the worker's 15-minute lease (about 280 s per 1,000 pieces with int8, 410 s with fp32). The workspace total can be higher. Over 5,000 pieces, calls search in SQL instead of memory.

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

If it fails, run the same command again; it is safe to repeat. Do not turn the workspace on again while it runs. The last step sweeps files no row points to, only while the workspace is off; a failed sweep only logs a warning.

## Before the first workspace is turned on

Apply the Terraform change that grants the API read, write, delete and list on `knowledge-documents/` in the mail bucket, from the repo root of the main checkout after merge:

```bash
cd infra && terragrunt apply
```

Without it, uploads answer 503 and stored files are refused with 403, which fails their documents after 3 tries.

## What runs where

| Step                                  | Process                      | Notes                                                                                      |
| ------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------ |
| Upload link and PUT                   | API                          | `knowledge-documents/<org>/<id>.<attempt>` in the mail bucket; type sniffed at PUT         |
| Ingest (`pending` → `ready`/`failed`) | API, `KnowledgeIngestWorker` | Decode in a child process (no credentials, 30 s CPU, 1 GB, 60 s); chunk; embed on 1 thread |
| Search API, text and email replies    | API, voicebot text worker    | SQL: pgvector cosine + idf over `terms`, fused by rank                                     |
| Calls                                 | Each call process            | Loads pieces + model during the greeting; top 3 pieces per turn, before the model call     |

The worker polls every 5 s (`HAIL_KNOWLEDGE_POLL_SECONDS`, code default; 0 turns it off). Untouched uploads are deleted after 24 hours (upload sweep). A document not ready one day after it was added fails. A workspace with more than 5,000 pieces is searched in SQL during calls instead of in memory.

## Model

`intfloat/multilingual-e5-small` (MIT), its own int8 ONNX file (`onnx/model_qint8_avx512_vnni.onnx`, revision `614241f6`), SentencePiece tokenizer. Baked into the API and voicebot images by sha256; each build runs `python -m hailhq.core.knowledge.selftest` offline (Italian, German and Japanese questions against an English manual). `HAIL_EMBEDDING_MODEL_DIR` (code default `/app/models/embedding`) points elsewhere for a checkout outside Docker.

Measured 2026-11-21 (CPU only, 1 thread):

|                                                             | int8 (shipped)                                | fp32                    |
| ----------------------------------------------------------- | --------------------------------------------- | ----------------------- |
| Memory per process after load                               | about 180 MB more (about 260 to 280 MB total) | about 400 MB more       |
| Question embed, x86 (GitHub runner, AMD EPYC, AVX-512 VNNI) | p50 2.9 ms, p95 3.5 ms                        | p50 8.7 ms, p95 10.1 ms |
| Ingest, 500-token pieces, x86                               | about 280 s per 1,000 pieces                  | about 410 s             |
| Load                                                        | about 0.5 s                                   | about 1 s               |
| Agreement with fp32 (cosine)                                | 0.996 min                                     |                         |

Quality on an invented 26-section manual, 25 questions in 7 languages: e5 found the right ~400-token piece 18 times out of 25, `paraphrase-multilingual-MiniLM-L12-v2` 9 to 11 (it reads only the first 128 tokens), so e5 was chosen. Cosine scores of right pieces were 0.73 to 0.92, off-topic best matches 0.72 to 0.81: the floor (0.72) sits below every right piece and only drops clear junk; keyword search carries IDs. A message made only of filler words ("okay, thank you", "Sorry?") is not searched. Hold-the-line turns ("one second", "wait, what?") are not searched either. Otherwise a call turn needs two non-filler words, three spoken words, or one word with a question mark; a text or email needs two words. Any ID is always searched.

The int8 file is tuned for CPUs with VNNI. On an x86 CPU without it (AVX2 only), int8 math can saturate. Check the VM once:

```bash
grep -o -E 'avx2|avx512_vnni|avx_vnni' /proc/cpuinfo | sort -u
```

If neither VNNI flag shows, switch both Dockerfiles to `onnx/model.onnx` (fp32, sha256 `ca456c06…8665`), at about 220 MB more per process.

## Limits and alerts

- All knowledge data together (both tables with indexes, `pg_total_relation_size`) is capped at 300 MB: new links and documents answer 409, and the worker refuses new pieces.
- The abuse monitor emails `HAIL_SUPPORT_EMAIL` at most once a day while `pg_database_size` is over 200 MB (state in `platform_flags`, key `database_size_alert_day`). Check sizes:

```sql
SELECT pg_size_pretty(pg_database_size(current_database())),
       pg_size_pretty(pg_total_relation_size('knowledge_pieces')),
       pg_size_pretty(pg_total_relation_size('knowledge_documents'));
```
