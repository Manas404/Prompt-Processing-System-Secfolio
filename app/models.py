import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, Float, Integer, DateTime, Boolean, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from app.database import Base
from app.config import settings


class PromptRequest(Base):
    """Main table storing all incoming prompt requests."""
    __tablename__ = "prompt_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt = Column(Text, nullable=False)
    model = Column(String(100), nullable=False, default=settings.DEFAULT_MODEL)
    provider = Column(String(50), nullable=False, default=settings.DEFAULT_PROVIDER)
    max_tokens = Column(Integer, default=1000)
    temperature = Column(Float, default=0.7)
    status = Column(String(20), nullable=False, default="pending")
    # pending | queued | processing | completed | failed | cached
    task_id = Column(String(255), nullable=True)  # Celery task ID
    priority = Column(Integer, default=5)  # 1 (highest) to 10 (lowest)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relations
    response = relationship("PromptResponse", back_populates="request", uselist=False)
    metadata_obj = relationship("RequestMetadata", back_populates="request", uselist=False)


class PromptResponse(Base):
    """Stores LLM responses linked to requests."""
    __tablename__ = "prompt_responses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(UUID(as_uuid=True), ForeignKey("prompt_requests.id"), nullable=False)
    content = Column(Text, nullable=False)
    tokens_used = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    from_cache = Column(Boolean, default=False)
    cache_hit_id = Column(UUID(as_uuid=True), nullable=True)  # ID of cached response used
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relations
    request = relationship("PromptRequest", back_populates="response")


class SemanticCache(Base):
    """Stores prompt embeddings for semantic similarity search."""
    __tablename__ = "semantic_cache"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_hash = Column(String(64), nullable=False, index=True)  # SHA256 for exact match
    prompt_text = Column(Text, nullable=False)
    embedding = Column(Vector(settings.EMBEDDING_DIM), nullable=True)
    response_content = Column(Text, nullable=False)
    model = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)
    tokens_used = Column(Integer, nullable=True)
    hit_count = Column(Integer, default=0)  # How many times this cache entry was used
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class RateLimitBucket(Base):
    """Token bucket state for distributed rate limiting."""
    __tablename__ = "rate_limit_buckets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(50), nullable=False, unique=True)
    tokens = Column(Float, nullable=False)          # Current tokens available
    max_tokens = Column(Float, nullable=False)      # Bucket capacity (= RPM)
    refill_rate = Column(Float, nullable=False)     # Tokens per second
    last_refill = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RequestMetadata(Base):
    """Optional metadata attached to a request."""
    __tablename__ = "request_metadata"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(UUID(as_uuid=True), ForeignKey("prompt_requests.id"), nullable=False)
    user_id = Column(String(255), nullable=True)
    tags = Column(JSON, default=list)
    extra = Column(JSON, default=dict)

    # Relations
    request = relationship("PromptRequest", back_populates="metadata_obj")
