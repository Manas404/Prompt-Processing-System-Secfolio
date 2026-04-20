import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

import redis as redis_lib
from fastapi import FastAPI, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, init_db
from app.models import PromptRequest, PromptResponse
from app.schemas import (
    PromptSubmitRequest, BulkPromptRequest,
    SubmitResponse, BulkSubmitResponse,
    PromptRequestSchema, QueueStatsSchema, HealthSchema, CacheStatsSchema,
    StatusEnum,
)
from app.services import CacheService, RateLimitService
from app.tasks import celery_app, process_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s – %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Distributed prompt processing with semantic caching and rate limiting.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    init_db()


# ── 1. Submit a single prompt ──────────────────────────────────────────────────

@app.post("/api/v1/prompts", response_model=SubmitResponse, status_code=status.HTTP_202_ACCEPTED)
def submit_prompt(payload: PromptSubmitRequest, db: Session = Depends(get_db)):
    """
    Accept a prompt, persist it, and enqueue for async processing.
    Returns immediately with a request_id for polling.
    """
    model = payload.model or settings.DEFAULT_MODEL

    req = PromptRequest(
        prompt=payload.prompt,
        model=model,
        provider=payload.provider.value,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        priority=payload.priority,
        status="queued",
    )
    db.add(req)
    db.flush()  # Get the ID without committing

    # Dispatch to Celery
    task = process_prompt.apply_async(
        args=[str(req.id)],
        priority=payload.priority,
    )
    req.task_id = task.id
    db.commit()

    logger.info("Submitted request %s → task %s", req.id, task.id)
    return SubmitResponse(
        request_id=req.id,
        status=StatusEnum.queued,
        message="Prompt queued for processing",
        estimated_wait_seconds=_estimate_wait(db),
    )


# ── 2. Bulk submit ─────────────────────────────────────────────────────────────

@app.post("/api/v1/prompts/bulk", response_model=BulkSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
def submit_bulk(payload: BulkPromptRequest, db: Session = Depends(get_db)):
    """Submit up to 100 prompts in a single request."""
    ids = []
    for item in payload.requests:
        model = item.model or settings.DEFAULT_MODEL
        req = PromptRequest(
            prompt=item.prompt,
            model=model,
            provider=item.provider.value,
            max_tokens=item.max_tokens,
            temperature=item.temperature,
            priority=item.priority,
            status="queued",
        )
        db.add(req)
        db.flush()
        task = process_prompt.apply_async(args=[str(req.id)], priority=item.priority)
        req.task_id = task.id
        ids.append(req.id)

    db.commit()
    return BulkSubmitResponse(
        submitted=len(ids),
        request_ids=ids,
        message=f"Queued {len(ids)} prompts for processing",
    )


# ── 3. Get status / result ─────────────────────────────────────────────────────

@app.get("/api/v1/prompts/{request_id}", response_model=PromptRequestSchema)
def get_prompt(request_id: UUID, db: Session = Depends(get_db)):
    """Poll for status and retrieve result when completed."""
    req = db.query(PromptRequest).filter(PromptRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


# ── 4. List prompts ────────────────────────────────────────────────────────────

@app.get("/api/v1/prompts", response_model=List[PromptRequestSchema])
def list_prompts(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(PromptRequest)
    if status:
        q = q.filter(PromptRequest.status == status)
    return q.order_by(PromptRequest.created_at.desc()).offset(offset).limit(limit).all()


# ── 5. Cancel a queued prompt ──────────────────────────────────────────────────

@app.delete("/api/v1/prompts/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_prompt(request_id: UUID, db: Session = Depends(get_db)):
    req = db.query(PromptRequest).filter(PromptRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status not in ("pending", "queued"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a task in '{req.status}' state")
    if req.task_id:
        celery_app.control.revoke(req.task_id, terminate=True)
    req.status = "failed"
    db.commit()


# ── 6. Queue stats ─────────────────────────────────────────────────────────────

@app.get("/api/v1/queue/stats", response_model=QueueStatsSchema)
def queue_stats():
    inspect = celery_app.control.inspect(timeout=2.0)
    active = inspect.active() or {}
    scheduled = inspect.scheduled() or {}
    reserved = inspect.reserved() or {}

    return QueueStatsSchema(
        pending_tasks=sum(len(v) for v in reserved.values()),
        active_tasks=sum(len(v) for v in active.values()),
        scheduled_tasks=sum(len(v) for v in scheduled.values()),
        failed_tasks=0,  # Fetch from DB if needed
        workers_online=len(active),
    )


# ── 7. Cache stats ─────────────────────────────────────────────────────────────

@app.get("/api/v1/cache/stats", response_model=CacheStatsSchema)
def cache_stats(db: Session = Depends(get_db)):
    return CacheService(db).stats()


# ── 8. Health check ────────────────────────────────────────────────────────────

@app.get("/api/v1/health", response_model=HealthSchema)
def health(db: Session = Depends(get_db)):
    db_status = "ok"
    redis_status = "ok"
    celery_status = "ok"

    try:
        db.execute(db.bind.text("SELECT 1"))
    except Exception:
        db_status = "error"

    try:
        r = redis_lib.from_url(settings.REDIS_URL)
        r.ping()
    except Exception:
        redis_status = "error"

    try:
        inspect = celery_app.control.inspect(timeout=1.0)
        if not inspect.ping():
            celery_status = "no workers"
    except Exception:
        celery_status = "error"

    overall = "ok" if all(s == "ok" for s in [db_status, redis_status]) else "degraded"

    return HealthSchema(
        status=overall,
        version=settings.APP_VERSION,
        database=db_status,
        redis=redis_status,
        celery=celery_status,
        timestamp=datetime.utcnow(),
    )


# ── Rate limit status ──────────────────────────────────────────────────────────

@app.get("/api/v1/rate-limit/{provider}")
def rate_limit_status(provider: str, db: Session = Depends(get_db)):
    return RateLimitService(db).get_status(provider)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _estimate_wait(db: Session) -> float:
    """Rough estimate: queued tasks / (300 rpm / 60)"""
    queued = db.query(PromptRequest).filter(PromptRequest.status == "queued").count()
    return round(queued / (settings.RATE_LIMIT_RPM / 60), 1)
