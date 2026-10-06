# Knowledge base: pgvector RAG (lesson 6.1)

The agent's `answer_faq` will search a practice's knowledge base by meaning:
services, prices, insurance, hours, parking. This note covers how the base is
built and searched, and what the 20-question check measured.

```bash
docker compose up -d postgres                       # pgvector/pgvector:pg16-trixie
alembic upgrade head                                # documents, chunks, HNSW + GIN
python -m scripts.ingest --practice-id 4            # kb/sunshine-dental → rows (idempotent)
python -m experiments.rag_eval.run --practice-id 4  # 20-question relevance check
curl -s localhost:8000/rag/search -H 'content-type: application/json' \
  -d '{"practice_id": 4, "query": "Do you do Invisalign?", "k": 3}'
```

## Pieces

| Piece | Where | Decision |
|---|---|---|
| Storage | `documents`, `chunks` (migration `c4e6a8b0d2f3`) | `chunks.embedding vector(1024)` with an **HNSW** index (`vector_cosine_ops`). `chunks.tsv` is a **generated** `tsvector` with a **GIN** index, so keyword search can never go stale. Both live only in the migration: SQLite (the unit-test DB) can't create them, and the ORM never writes `tsv`. |
| Image | `docker-compose.yaml` | `pgvector/pgvector:pg16-trixie`, not `:pg16`. The old `postgres:16` image had moved to Debian trixie (glibc 2.41). `:pg16` is bookworm (2.36), and Postgres flagged a collation mismatch on the existing data. Matching the OS avoids reindexing. |
| Embeddings | `app/rag/embed.py` | **Voyage `voyage-4-lite`**, 1024 dims, `input_type` document vs query (asymmetric: a short question and the passage answering it are trained to land close). Plain httpx, no SDK. Batched (≤128 texts, ≤8K tokens). In-process LRU cache for queries. |
| Chunking | `app/rag/chunk.py` | Hand-written, ~70 lines. Split on headings, pack whole sections up to 500 tokens, **balanced** (decide the chunk count first, then aim for equal sizes), overlap only inside an oversized section. Every chunk starts with its doc title. Result: 60 chunks, 193–492 tokens, median 331. |
| Content | `kb/sunshine-dental/` | 31 docs (~13K words) for practice 4, written from one fact sheet (`_FACTS.md`, not ingested). Hours and providers match the scheduler and the FHIR seed. Every price and CDT code was checked against the sheet. |
| Ingest | `scripts/ingest.py` | Idempotent. Doc hash = text + chunker version + model, so unchanged docs are skipped. Chunk hash = model + text, so an edited doc re-embeds only its changed chunks. `--prune` deletes docs whose file is gone. One transaction per doc. |
| Search | `app/rag/search.py`, `POST /rag/search` | `vector` (cosine `<=>`), `keyword` (`ts_rank_cd`), `hybrid` (**reciprocal rank fusion**, k=60). Every query filters on `documents.practice_id`. |

## Decisions worth defending

**Why hybrid at all.** The two searches fail in opposite places:
- **Embeddings** blur exact tokens: "D4341", "Delta", "Albany", "$50".
- **Keywords** miss paraphrase: "put me to sleep" vs sedation, "Invisalign" vs clear aligners.

RRF combines them using **ranks only**: score = Σ 1/(60 + rank). A cosine similarity and a ts_rank are on different scales, and any weighted sum of them needs tuning that breaks when the corpus changes.

**Keyword search uses OR, not AND.** `plainto_tsquery('do you take delta dental')` means `take & delta & dental`, and a caller's question rarely has every word in one chunk. Rewriting `&` to `|` keeps stemming and stop-word removal while ranking chunks by how many query words they contain.

**Tenant isolation, and HNSW with a filter.** HNSW returns its `ef_search` nearest neighbors first and applies `WHERE practice_id = …` afterwards. With many practices in one table, a small practice could get back fewer than k results. pgvector ≥ 0.8's `SET LOCAL hnsw.iterative_scan = relaxed_order` keeps scanning until the filter is satisfied. At 60 rows the planner just scans the table; the setting matters at thousands of practices.

**Failing soft on a live call.** On a call, query embedding gets one try with a 2-second timeout. If it fails, hybrid falls back to keyword-only, and the response says so (`degraded`). Ingest is offline, so it retries: 5xx with backoff, and 429 by waiting out the minute. Voyage's limits are per minute and it sends no `Retry-After`.

**Rate limits are real.** A Voyage account without a payment method gets 3 requests and 10K tokens per minute. The first ingest sent all ~20K tokens in one batch, which can never succeed on that tier, and its 1-2-4 s retries burned the 3 requests. Hence the ≤8K-token batches, the 60-second wait on 429, and `embed_queries` (the eval embeds all 20 questions in one request).

**Privacy.** The KB text is public. Callers' questions are not ("I'm pregnant, is a cleaning safe?"), so in production Voyage needs a BAA, like Deepgram and Anthropic.

## The 20-question check

`experiments/rag_eval/questions.json` holds 20 questions, written **before** the docs existed and phrased the way callers ask. Several share no words with their answer (Invisalign, "put me to sleep", installments). A hit means a chunk from the expected doc that contains the deciding fact (a price, a code, a name), within the top 3. DoD: hybrid ≥ 17/20.

### Result (2026-10-05, voyage-4-lite, 60 chunks)

| Mode | Hit@3 | MRR@10 |
|---|---|---|
| vector | **20/20** | **0.867** |
| keyword (OR, `ts_rank_cd`) | 10/20 | 0.515 |
| hybrid (RRF, equal weights) | 18/20 ✅ DoD | 0.781 |

The DoD is met. **But hybrid didn't help: it lost 2 questions that vector alone got at rank 1.**

| # | Question | vector | keyword | hybrid |
|---|---|---|---|---|
| 12 | Can I pay in installments for a big treatment? | 1 | 10 | 4 ✗ |
| 16 | I'm pregnant. Is it safe to get my teeth cleaned? | 1 | no match | 5 ✗ |

- **#12:** keyword ranked the financing chunk 10th, and equal-weight RRF counts that weak vote fully.
- **#16:** "pregnant" and "pregnancy" stem to different lexemes, so keyword found nothing, and the right chunk got no keyword vote while its competitors did.

Keyword alone failed on exactly the questions written to test paraphrase: Invisalign (zero matches), "put me to sleep", "installments".

### Where keyword does help: exact tokens (post-hoc probe, not the DoD set)

These 8 queries were written *after* seeing the results above, to test keyword's strong suit. They're rank of the right doc:

| Probe | vector | keyword | hybrid |
|---|---|---|---|
| D4341 | 1 | 1 | 1 |
| D9248 | — | 2 | 4 |
| Guardian DentalGuard Preferred | 3 | 1 | 1 |
| Sunbit | 1 | 1 | 1 |
| Little Teeth Pediatric Dentistry | 1 | — | 5 |
| HART bus | 2 | 1 | 1 |
| D6065 implant crown | 2 | 2 | 2 |
| Cigna DHMO | 1 | 1 | 1 |

The embedding blurs a bare procedure code (D9248, which vector misses entirely) and long proper names. Keyword can also fail on a name made of common words ("Little Teeth…" loses to docs that say "teeth" more often).

### Exploratory: keyword weight in RRF

This weight was chosen on the same 28 queries, so it is **a hypothesis, not a result**.

| Keyword weight | 20 questions hit@3 | 8 probes hit@3 |
|---|---|---|
| 1.0 (plain RRF) | 18 | 6 |
| 0.5 | 20 | 7 |
| 0.25 | 20 | 7 |
| 0 (vector only) | 20 | 7 |

### What I'd take from it

1. **Callers paraphrase.** For a voice agent, the questions look like the 20, not like the probes. Nobody calls and says "D9248". Embeddings carry this workload.
2. **Plain RRF assumes both lists are about equally good.** Here one list is much weaker, so fusing them averages away the better one. Fusion helps when the retrievers are comparably strong and fail on *different* queries.
3. **Keyword still earns its place in two ways:**
   - **Fallback.** When Voyage is down or slow, hybrid degrades to keyword-only: 10/20 beats nothing.
   - **Exact names and codes.** Possibly as a down-weighted vote (≈0.5), to be validated on a fresh question set.
4. **The eval is small.** 20 questions over 60 chunks means one question is 5 points. A real deployment needs more questions, from real call transcripts, and a second practice's KB to check the result generalizes.
