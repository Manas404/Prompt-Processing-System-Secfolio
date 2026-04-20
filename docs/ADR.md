# Architecture Decision Records (ADR)

---

## ADR-001: Task Queue — Celery + Redis

**Status:** Accepted

**Context:**
We need durable, distributed task execution that survives worker crashes and supports retry logic with exponential backoff.

**Decision:**
Use Celery with Redis as the broker and result backend.

**Rationale:**
- `acks_late=True` means a task is only acknowledged (removed from the queue) after it completes successfully. If a worker dies mid-execution, the broker re-delivers the task to another worker.
- `task_reject_on_worker_lost=True` ensures crashed-worker tasks go back to the queue.
- Redis persistence (`appendonly yes`) means even broker crashes don't lose enqueued tasks.
- Celery supports priority queues natively — high-priority prompts jump the queue.
- Flower gives us a real-time monitoring UI with zero extra code.

**Trade-offs:**
- Redis is an additional service to operate. Mitigation: Docker Compose makes this trivial.
- For very high throughput (>10K tasks/sec) RabbitMQ has better routing. At 300 RPM this is not a concern.

---

## ADR-002: Semantic Cache — PostgreSQL + pgvector

**Status:** Accepted

**Context:**
Identical or semantically equivalent prompts should not incur LLM API costs. We need both exact-match deduplication and near-duplicate detection.

**Decision:**
Store prompt embeddings in PostgreSQL using the pgvector extension. Use SHA-256 for exact matching and cosine similarity (`<=>` operator) for semantic matching.

**Rationale:**
- Avoids an additional SaaS vector database (Pinecone ~$70/mo, Weaviate requires ops).
- pgvector's IVFFlat index gives sub-10ms similarity search up to ~1M vectors.
- All data stays in one ACID database — cache entries are consistent with request records.
- Cosine similarity threshold of 0.92 balances recall vs. false positives empirically.

**Two-tier caching strategy:**
1. **Exact match (SHA-256 hash):** O(1) lookup, handles identical prompts.
2. **Semantic match (vector cosine):** handles paraphrased/reformulated prompts.

**Trade-offs:**
- Embedding generation requires an OpenAI API call (adds ~100ms). Mitigation: embedding calls are async and failures gracefully fall through to LLM call.
- pgvector requires PostgreSQL 14+. This is a reasonable constraint.

---

## ADR-003: Rate Limiting — Token Bucket in PostgreSQL

**Status:** Accepted

**Context:**
The LLM provider enforces 300 requests/minute. We need distributed rate limiting that works across multiple API instances and worker processes.

**Decision:**
Implement the token bucket algorithm with state stored in PostgreSQL, using `SELECT FOR UPDATE` for atomic access.

**Rationale:**
- **Distributed by default:** All API instances and workers share the same bucket state.
- **Atomic:** `SELECT FOR UPDATE` prevents race conditions without Lua scripts.
- **Durable:** Survives Redis restarts (unlike Redis-based rate limiters).
- **Observable:** Bucket state is a plain SQL row — easy to inspect and debug.

**Algorithm:**
```
tokens = min(max_tokens, tokens + elapsed_seconds × refill_rate)
if tokens < 1:
    raise RateLimitExceeded(retry_after = (1 - tokens) / refill_rate)
tokens -= 1
```

**Trade-offs:**
- Higher latency than a Redis INCR (~5ms vs ~0.5ms). At 300 RPM this is negligible.
- PostgreSQL lock contention at very high RPS. Mitigation: partition buckets by provider if needed.

---

## ADR-004: API Framework — FastAPI

**Status:** Accepted

**Context:**
The API must return immediately (202 Accepted) and never block on slow LLM calls.

**Decision:**
Use FastAPI with Uvicorn (ASGI).

**Rationale:**
- Native async/await — no threads blocked on I/O.
- Pydantic v2 for request validation — detailed errors, zero boilerplate.
- Auto-generated OpenAPI docs at `/docs` — free client SDKs and documentation.
- 10x faster than Django REST Framework in benchmarks (TechEmpower).

**Trade-offs:**
- Smaller ecosystem than Django. Mitigation: not relevant for an API-only service.

---

## ADR-005: Crash Recovery

**Status:** Accepted

**Context:**
Worker processes can crash (OOM, deployment, hardware failure). Tasks in `processing` state would be orphaned.

**Decision:**
Two-layer recovery:

1. **Celery native:** `acks_late=True` + `task_reject_on_worker_lost=True` handle mid-execution crashes automatically.
2. **Scheduled sweep:** `requeue_stuck_tasks` Celery Beat task runs every 10 minutes and requeues any `PromptRequest` stuck in `processing` for more than 10 minutes.

**Rationale:**
Layer 1 covers 99% of crashes. Layer 2 is a safety net for edge cases (e.g., database connection lost before ack).

---

## ADR-006: Database Schema

**Status:** Accepted

Four tables with clear separation of concerns:

| Table | Purpose |
|---|---|
| `prompt_requests` | Source of truth for all requests and their lifecycle status |
| `prompt_responses` | Decoupled from requests to allow partial results and cache hits |
| `semantic_cache` | Stores embeddings + responses for reuse |
| `rate_limit_buckets` | Token bucket state per provider |
| `request_metadata` | Optional user/tag data, separated to keep core tables lean |
