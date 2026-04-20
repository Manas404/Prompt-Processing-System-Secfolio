# Prompt Processing System

A production-grade distributed system for processing LLM prompt requests at scale.

[![CI/CD](https://github.com/yourusername/prompt-processing-system/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/yourusername/prompt-processing-system/actions)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green.svg)](https://fastapi.tiangolo.com)

---

## Overview

This system solves the core problem of handling **high-volume, unreliable LLM API calls** reliably:

| Problem | Solution |
|---|---|
| Slow synchronous calls block users | Async queue with instant 202 response |
| Provider rate limits (300 rpm) | Token bucket algorithm in PostgreSQL |
| Identical prompts cost money | Semantic cache (pgvector cosine similarity) |
| Worker crashes lose tasks | Celery `acks_late=True` + crash recovery task |
| Single point of failure | Horizontally scalable workers |

---

## Architecture

```
Client
  │
  ▼
FastAPI (REST API)          ← validates, stores, returns 202 immediately
  │
  ├─► PostgreSQL             ← persists requests, responses, cache, rate limits
  │
  └─► Redis (Celery broker)  ← durable task queue
          │
          ▼
    Celery Workers (N)
          │
          ├─ 1. Check SemanticCache (pgvector similarity)
          ├─ 2. Check RateLimit (token bucket)
          ├─ 3. Call LLM Provider (Anthropic / OpenAI)
          ├─ 4. Store response in DB
          └─ 5. Store in SemanticCache for future hits
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/yourusername/prompt-processing-system.git
cd prompt-processing-system

# 2. Configure environment
make env          # copies .env.example → .env
# Edit .env and add your ANTHROPIC_API_KEY or OPENAI_API_KEY

# 3. Start everything
make up

# 4. Verify
make test-health
```

Services:
- **API**: http://localhost:8000
- **Swagger Docs**: http://localhost:8000/docs
- **Flower (queue monitor)**: http://localhost:5555

---

## API Reference

### Submit a prompt
```bash
curl -X POST http://localhost:8000/api/v1/prompts \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain the CAP theorem",
    "provider": "anthropic",
    "max_tokens": 500,
    "priority": 3
  }'

# Response (202):
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "message": "Prompt queued for processing",
  "estimated_wait_seconds": 0.2
}
```

### Poll for result
```bash
curl http://localhost:8000/api/v1/prompts/550e8400-e29b-41d4-a716-446655440000

# Response when complete:
{
  "id": "550e8400-...",
  "status": "completed",
  "response": {
    "content": "The CAP theorem states...",
    "tokens_used": 312,
    "cost_usd": 0.000078,
    "latency_ms": 842,
    "from_cache": false
  }
}
```

### Other endpoints
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/prompts` | Submit single prompt |
| `POST` | `/api/v1/prompts/bulk` | Submit up to 100 prompts |
| `GET` | `/api/v1/prompts/{id}` | Get status/result |
| `GET` | `/api/v1/prompts` | List prompts (with filters) |
| `DELETE` | `/api/v1/prompts/{id}` | Cancel queued prompt |
| `GET` | `/api/v1/queue/stats` | Celery queue statistics |
| `GET` | `/api/v1/cache/stats` | Semantic cache statistics |
| `GET` | `/api/v1/rate-limit/{provider}` | Rate limit status |
| `GET` | `/api/v1/health` | Health check |

---

## Key Design Decisions

### 1. Celery + Redis for task queue
**Why not SQS / RabbitMQ?** Celery+Redis is self-hosted, battle-tested, and ships Flower for free. `acks_late=True` ensures tasks are not lost if a worker crashes mid-execution — the broker redelivers them to another worker automatically.

### 2. PostgreSQL + pgvector for semantic cache
**Why not Pinecone / Weaviate?** A self-hosted vector store avoids additional SaaS costs and keeps everything in one ACID database. pgvector's `<=>` cosine distance operator lets us find semantically similar prompts in a single SQL query.

### 3. Token bucket in PostgreSQL
**Why not Redis for rate limiting?** Using `SELECT FOR UPDATE` in PostgreSQL gives us atomic read-modify-write without a separate Redis Lua script. It also means rate limit state survives Redis restarts and is visible in standard SQL queries.

### 4. FastAPI (async)
**Why not Django/Flask?** FastAPI's native async support means the API never blocks on I/O. It returns a 202 immediately — the heavy work happens in Celery workers, not the HTTP thread.

---

## Running Tests

```bash
make test          # full suite with coverage report
make test-fast     # quick run, stops at first failure
make test-unit     # unit tests only (no integration)
```

---

## Scaling

```bash
# Scale to 4 Celery workers
make scale-workers N=4

# Or with docker-compose directly
docker-compose up -d --scale worker=4
```

The system is stateless at the API and worker layers — scale both independently.

---

## Project Structure

```
prompt-processing-system/
├── app/
│   ├── main.py              # FastAPI routes (9 endpoints)
│   ├── models.py            # SQLAlchemy ORM (4 tables)
│   ├── tasks.py             # Celery task definitions
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── config.py            # Settings (pydantic-settings)
│   ├── database.py          # Connection pooling
│   └── services/
│       ├── cache_service.py       # Semantic caching
│       ├── rate_limit_service.py  # Token bucket
│       └── provider_service.py   # Anthropic + OpenAI
├── tests/
│   └── test_api.py          # Pytest suite (30+ tests)
├── scripts/
│   ├── start.sh             # Quick start script
│   └── init-db.sql          # DB initialization
├── .github/workflows/
│   └── ci-cd.yml            # GitHub Actions CI/CD
├── docker-compose.yml       # Full local stack
├── Dockerfile               # Multi-stage production build
├── Makefile                 # 25+ developer commands
├── requirements.txt
└── .env.example
```

---

## License

MIT
