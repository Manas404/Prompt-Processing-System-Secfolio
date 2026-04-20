from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from enum import Enum


class ProviderEnum(str, Enum):
    anthropic = "anthropic"
    openai = "openai"


class StatusEnum(str, Enum):
    pending = "pending"
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cached = "cached"


# ── Request Schemas ────────────────────────────────────────────────────────────

class PromptSubmitRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32000, description="The prompt text")
    model: Optional[str] = Field(None, description="Model name (e.g. claude-3-haiku-20240307)")
    provider: ProviderEnum = Field(ProviderEnum.anthropic, description="LLM provider")
    max_tokens: int = Field(1000, ge=1, le=8192)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    priority: int = Field(5, ge=1, le=10, description="1=highest, 10=lowest")
    user_id: Optional[str] = Field(None, description="Optional user identifier")
    tags: List[str] = Field(default_factory=list)
    extra_metadata: Dict[str, Any] = Field(default_factory=dict)

    @validator("prompt")
    def prompt_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Prompt cannot be blank")
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "prompt": "Explain the CAP theorem in simple terms",
                "provider": "anthropic",
                "max_tokens": 500,
                "temperature": 0.7,
                "priority": 3,
            }
        }


class BulkPromptRequest(BaseModel):
    requests: List[PromptSubmitRequest] = Field(..., min_items=1, max_items=100)


# ── Response Schemas ───────────────────────────────────────────────────────────

class PromptResponseSchema(BaseModel):
    id: UUID
    content: str
    tokens_used: Optional[int]
    cost_usd: Optional[float]
    latency_ms: Optional[int]
    from_cache: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PromptRequestSchema(BaseModel):
    id: UUID
    prompt: str
    model: str
    provider: str
    max_tokens: int
    temperature: float
    status: StatusEnum
    task_id: Optional[str]
    priority: int
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    response: Optional[PromptResponseSchema]

    class Config:
        from_attributes = True


class SubmitResponse(BaseModel):
    request_id: UUID
    status: StatusEnum
    message: str
    estimated_wait_seconds: Optional[float] = None


class BulkSubmitResponse(BaseModel):
    submitted: int
    request_ids: List[UUID]
    message: str


class QueueStatsSchema(BaseModel):
    pending_tasks: int
    active_tasks: int
    scheduled_tasks: int
    failed_tasks: int
    workers_online: int


class HealthSchema(BaseModel):
    status: str
    version: str
    database: str
    redis: str
    celery: str
    timestamp: datetime


class CacheStatsSchema(BaseModel):
    total_entries: int
    total_hits: int
    hit_rate_percent: float
    oldest_entry: Optional[datetime]
    newest_entry: Optional[datetime]
