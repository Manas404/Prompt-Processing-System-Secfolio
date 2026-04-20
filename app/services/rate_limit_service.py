import logging
from datetime import datetime
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RateLimitBucket

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when the token bucket is empty."""
    def __init__(self, provider: str, retry_after: float):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for {provider}. Retry after {retry_after:.1f}s")


class RateLimitService:
    """
    Distributed token bucket rate limiter backed by PostgreSQL.

    Guarantees: atomic refill + consume via SELECT FOR UPDATE.
    Capacity   : settings.RATE_LIMIT_RPM tokens (default 300)
    Refill rate: capacity / 60 tokens per second
    """

    def __init__(self, db: Session):
        self.db = db

    def check_and_consume(self, provider: str = settings.DEFAULT_PROVIDER) -> None:
        """
        Consume one token for `provider`.
        Raises RateLimitExceeded if the bucket is empty.
        """
        with self.db.begin_nested():
            bucket = (
                self.db.execute(
                    select(RateLimitBucket)
                    .where(RateLimitBucket.provider == provider)
                    .with_for_update()
                )
                .scalars()
                .first()
            )

            if bucket is None:
                bucket = self._create_bucket(provider)

            # Refill based on elapsed time
            now = datetime.utcnow()
            elapsed = (now - bucket.last_refill).total_seconds()
            refill_amount = elapsed * bucket.refill_rate
            bucket.tokens = min(bucket.max_tokens, bucket.tokens + refill_amount)
            bucket.last_refill = now

            if bucket.tokens < 1:
                # Calculate how long until one token is available
                retry_after = (1 - bucket.tokens) / bucket.refill_rate
                raise RateLimitExceeded(provider, retry_after)

            bucket.tokens -= 1
            self.db.flush()
            logger.debug("Rate limit OK for %s – %.1f tokens remaining", provider, bucket.tokens)

    def get_status(self, provider: str = settings.DEFAULT_PROVIDER) -> dict:
        bucket = (
            self.db.query(RateLimitBucket)
            .filter(RateLimitBucket.provider == provider)
            .first()
        )
        if not bucket:
            return {"provider": provider, "tokens": settings.RATE_LIMIT_RPM, "max_tokens": settings.RATE_LIMIT_RPM}

        now = datetime.utcnow()
        elapsed = (now - bucket.last_refill).total_seconds()
        current_tokens = min(bucket.max_tokens, bucket.tokens + elapsed * bucket.refill_rate)
        return {
            "provider": provider,
            "tokens_available": round(current_tokens, 2),
            "max_tokens": bucket.max_tokens,
            "refill_rate_per_sec": bucket.refill_rate,
            "utilization_pct": round((1 - current_tokens / bucket.max_tokens) * 100, 1),
        }

    def _create_bucket(self, provider: str) -> RateLimitBucket:
        capacity = float(settings.RATE_LIMIT_RPM)
        bucket = RateLimitBucket(
            provider=provider,
            tokens=capacity,
            max_tokens=capacity,
            refill_rate=capacity / 60.0,  # tokens per second
        )
        self.db.add(bucket)
        self.db.flush()
        logger.info("Created rate limit bucket for provider=%s capacity=%d rpm", provider, int(capacity))
        return bucket
