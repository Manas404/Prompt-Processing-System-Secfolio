import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SemanticCache

logger = logging.getLogger(__name__)


def _sha256(text_input: str) -> str:
    return hashlib.sha256(text_input.encode()).hexdigest()


def _get_embedding(prompt: str) -> Optional[list]:
    """Generate embedding using OpenAI API (or mock for testing)."""
    try:
        import openai
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        resp = client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=prompt,
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.warning("Embedding generation failed: %s. Falling back to exact-match only.", exc)
        return None


class CacheService:
    def __init__(self, db: Session):
        self.db = db
        self.threshold = settings.CACHE_SIMILARITY_THRESHOLD

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, prompt: str, model: str, provider: str) -> Optional[Tuple[str, UUID]]:
        """
        Return (cached_response_text, cache_entry_id) if a match is found.
        Priority: exact hash match → semantic similarity match → None.
        """
        # 1. Exact match (free, fast)
        exact = self._exact_match(prompt, model, provider)
        if exact:
            self._increment_hit(exact.id)
            logger.info("Cache HIT (exact) for prompt hash %s", _sha256(prompt)[:8])
            return exact.response_content, exact.id

        # 2. Semantic match (vector cosine similarity)
        embedding = _get_embedding(prompt)
        if embedding:
            semantic = self._semantic_match(embedding, model, provider)
            if semantic:
                self._increment_hit(semantic.id)
                logger.info("Cache HIT (semantic) for prompt – similarity above %.2f", self.threshold)
                return semantic.response_content, semantic.id

        return None

    def set(
        self,
        prompt: str,
        response: str,
        model: str,
        provider: str,
        tokens_used: Optional[int] = None,
    ) -> None:
        """Store a prompt→response pair in the semantic cache."""
        embedding = _get_embedding(prompt)
        expires_at = datetime.utcnow() + timedelta(hours=settings.CACHE_TTL_HOURS)

        entry = SemanticCache(
            prompt_hash=_sha256(prompt),
            prompt_text=prompt,
            embedding=embedding,
            response_content=response,
            model=model,
            provider=provider,
            tokens_used=tokens_used,
            expires_at=expires_at,
        )
        self.db.add(entry)
        self.db.commit()
        logger.info("Stored cache entry (expires %s)", expires_at.isoformat())

    def stats(self) -> dict:
        total = self.db.query(SemanticCache).count()
        total_hits = self.db.query(
            self.db.query(SemanticCache.hit_count).subquery()
        ).count()
        # Simpler aggregation
        from sqlalchemy import func
        agg = self.db.query(
            func.sum(SemanticCache.hit_count),
            func.min(SemanticCache.created_at),
            func.max(SemanticCache.created_at),
        ).one()
        total_hits = agg[0] or 0
        hit_rate = (total_hits / max(total_hits + total, 1)) * 100
        return {
            "total_entries": total,
            "total_hits": total_hits,
            "hit_rate_percent": round(hit_rate, 2),
            "oldest_entry": agg[1],
            "newest_entry": agg[2],
        }

    def evict_expired(self) -> int:
        """Remove expired cache entries. Call via scheduled Celery beat task."""
        now = datetime.utcnow()
        deleted = (
            self.db.query(SemanticCache)
            .filter(SemanticCache.expires_at < now)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        logger.info("Evicted %d expired cache entries", deleted)
        return deleted

    # ── Private helpers ────────────────────────────────────────────────────────

    def _exact_match(self, prompt: str, model: str, provider: str) -> Optional[SemanticCache]:
        ph = _sha256(prompt)
        return (
            self.db.query(SemanticCache)
            .filter(
                SemanticCache.prompt_hash == ph,
                SemanticCache.model == model,
                SemanticCache.provider == provider,
                SemanticCache.expires_at > datetime.utcnow(),
            )
            .first()
        )

    def _semantic_match(
        self, embedding: list, model: str, provider: str
    ) -> Optional[SemanticCache]:
        """Use pgvector cosine distance to find closest cached prompt."""
        # pgvector operator: <=> = cosine distance (0 = identical, 2 = opposite)
        threshold_dist = 1.0 - self.threshold  # convert similarity → distance
        sql = text(
            """
            SELECT id, response_content
            FROM semantic_cache
            WHERE model = :model
              AND provider = :provider
              AND expires_at > NOW()
              AND (embedding <=> CAST(:embedding AS vector)) < :threshold
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT 1
            """
        )
        row = self.db.execute(
            sql,
            {
                "model": model,
                "provider": provider,
                "embedding": str(embedding),
                "threshold": threshold_dist,
            },
        ).fetchone()
        if row:
            return self.db.query(SemanticCache).get(row[0])
        return None

    def _increment_hit(self, entry_id) -> None:
        self.db.query(SemanticCache).filter(SemanticCache.id == entry_id).update(
            {"hit_count": SemanticCache.hit_count + 1}
        )
        self.db.commit()
