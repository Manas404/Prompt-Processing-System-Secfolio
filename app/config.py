from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Prompt Processing System"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"

    # Database
    DATABASE_URL: str = "postgresql://postgres:password@localhost:5432/promptdb"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # Rate Limiting
    RATE_LIMIT_RPM: int = 300  # requests per minute
    RATE_LIMIT_WINDOW: int = 60  # seconds

    # Semantic Cache
    CACHE_SIMILARITY_THRESHOLD: float = 0.92  # cosine similarity
    CACHE_TTL_HOURS: int = 24

    # LLM Providers
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    DEFAULT_PROVIDER: str = "anthropic"
    DEFAULT_MODEL: str = "claude-3-haiku-20240307"

    # Celery
    CELERY_TASK_MAX_RETRIES: int = 3
    CELERY_TASK_RETRY_BACKOFF: int = 2  # exponential base (seconds)

    # Embeddings
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
