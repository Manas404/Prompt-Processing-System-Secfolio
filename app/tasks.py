import logging
from datetime import datetime
from uuid import UUID

from celery import Celery
from celery.utils.log import get_task_logger

from app.config import settings

# ── Celery app ─────────────────────────────────────────────────────────────────
celery_app = Celery(
    "prompt_processor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Durability: tasks are acknowledged AFTER completion (not on receipt)
    task_acks_late=True,
    # Crash recovery: requeue tasks from crashed workers
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,       # Fair dispatch; prevents starvation
    task_track_started=True,
    result_expires=3600,                # Keep results for 1 hour
    # Priority queue support
    task_queue_max_priority=10,
    task_default_priority=5,
    # Retry config
    task_max_retries=settings.CELERY_TASK_MAX_RETRIES,
)

# Scheduled tasks (requires celery beat)
celery_app.conf.beat_schedule = {
    "evict-expired-cache": {
        "task": "app.tasks.evict_expired_cache",
        "schedule": 3600.0,  # every hour
    },
}

logger = get_task_logger(__name__)


# ── Main processing task ───────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.tasks.process_prompt",
    max_retries=settings.CELERY_TASK_MAX_RETRIES,
    default_retry_delay=settings.CELERY_TASK_RETRY_BACKOFF,
    acks_late=True,
)
def process_prompt(self, request_id: str) -> dict:
    """
    Core Celery task.
    1. Load request from DB
    2. Check semantic cache
    3. Enforce rate limit
    4. Call LLM provider
    5. Store response + cache entry
    """
    from app.database import SessionLocal
    from app.models import PromptRequest, PromptResponse
    from app.services import CacheService, RateLimitService, RateLimitExceeded, ProviderService

    db = SessionLocal()
    try:
        req = db.query(PromptRequest).filter(PromptRequest.id == UUID(request_id)).first()
        if not req:
            logger.error("Request %s not found", request_id)
            return {"status": "error", "message": "Request not found"}

        # Mark as processing
        req.status = "processing"
        db.commit()

        # ── 1. Semantic cache lookup ───────────────────────────────────────────
        cache_svc = CacheService(db)
        cached = cache_svc.get(req.prompt, req.model, req.provider)
        if cached:
            cached_content, cache_id = cached
            response = PromptResponse(
                request_id=req.id,
                content=cached_content,
                from_cache=True,
                cache_hit_id=cache_id,
                latency_ms=0,
            )
            db.add(response)
            req.status = "cached"
            req.completed_at = datetime.utcnow()
            db.commit()
            logger.info("Served request %s from cache", request_id)
            return {"status": "cached", "request_id": request_id}

        # ── 2. Rate limit check ────────────────────────────────────────────────
        rl_svc = RateLimitService(db)
        try:
            rl_svc.check_and_consume(req.provider)
        except RateLimitExceeded as exc:
            logger.warning("Rate limit for %s – retrying in %.1fs", exc.provider, exc.retry_after)
            raise self.retry(
                exc=exc,
                countdown=exc.retry_after,
                max_retries=20,  # allow more retries for rate limiting
            )

        # ── 3. LLM call ───────────────────────────────────────────────────────
        provider_svc = ProviderService()
        llm_resp = provider_svc.complete(
            prompt=req.prompt,
            provider=req.provider,
            model=req.model,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
        )

        # ── 4. Persist response ───────────────────────────────────────────────
        response = PromptResponse(
            request_id=req.id,
            content=llm_resp.content,
            tokens_used=llm_resp.tokens_used,
            cost_usd=llm_resp.cost_usd,
            latency_ms=llm_resp.latency_ms,
            from_cache=False,
        )
        db.add(response)

        # ── 5. Store in cache ─────────────────────────────────────────────────
        cache_svc.set(
            prompt=req.prompt,
            response=llm_resp.content,
            model=req.model,
            provider=req.provider,
            tokens_used=llm_resp.tokens_used,
        )

        req.status = "completed"
        req.completed_at = datetime.utcnow()
        db.commit()
        logger.info(
            "Completed request %s in %dms using %d tokens ($%.5f)",
            request_id, llm_resp.latency_ms, llm_resp.tokens_used, llm_resp.cost_usd
        )
        return {
            "status": "completed",
            "request_id": request_id,
            "tokens_used": llm_resp.tokens_used,
            "cost_usd": llm_resp.cost_usd,
        }

    except Exception as exc:
        db.rollback()
        # Mark failed only on final retry
        if self.request.retries >= self.max_retries:
            try:
                req = db.query(PromptRequest).filter(PromptRequest.id == UUID(request_id)).first()
                if req:
                    req.status = "failed"
                    db.commit()
            except Exception:
                pass
        logger.error("Task failed for request %s: %s", request_id, exc, exc_info=True)
        raise self.retry(
            exc=exc,
            countdown=settings.CELERY_TASK_RETRY_BACKOFF ** (self.request.retries + 1),
        )
    finally:
        db.close()


# ── Maintenance tasks ──────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.evict_expired_cache")
def evict_expired_cache():
    from app.database import SessionLocal
    from app.services import CacheService
    db = SessionLocal()
    try:
        n = CacheService(db).evict_expired()
        return {"evicted": n}
    finally:
        db.close()


@celery_app.task(name="app.tasks.requeue_stuck_tasks")
def requeue_stuck_tasks():
    """Requeue tasks stuck in 'processing' for more than 10 minutes (worker crash recovery)."""
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import PromptRequest
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        stuck = (
            db.query(PromptRequest)
            .filter(
                PromptRequest.status == "processing",
                PromptRequest.updated_at < cutoff,
            )
            .all()
        )
        for req in stuck:
            req.status = "queued"
            task = process_prompt.apply_async(
                args=[str(req.id)],
                priority=req.priority,
            )
            req.task_id = task.id
        db.commit()
        logging.getLogger(__name__).info("Requeued %d stuck tasks", len(stuck))
        return {"requeued": len(stuck)}
    finally:
        db.close()
