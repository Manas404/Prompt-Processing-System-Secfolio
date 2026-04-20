"""
Pytest test suite for Prompt Processing System.
Run with: pytest tests/ -v --cov=app
"""
import pytest
from unittest.mock import patch, MagicMock
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app.models import PromptRequest, PromptResponse, RateLimitBucket, SemanticCache
from app.config import settings

# ── Test DB (SQLite in-memory, no pgvector needed) ─────────────────────────────
SQLALCHEMY_TEST_URL = "sqlite:///./test.db"

engine = create_engine(SQLALCHEMY_TEST_URL, connect_args={"check_same_thread": False})
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_request(db):
    req = PromptRequest(
        id=uuid4(),
        prompt="What is the capital of France?",
        model="claude-3-haiku-20240307",
        provider="anthropic",
        max_tokens=500,
        temperature=0.7,
        status="completed",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


@pytest.fixture
def sample_request_with_response(db, sample_request):
    resp = PromptResponse(
        request_id=sample_request.id,
        content="The capital of France is Paris.",
        tokens_used=42,
        cost_usd=0.00001,
        latency_ms=300,
        from_cache=False,
    )
    db.add(resp)
    db.commit()
    return sample_request


# ── Health endpoint ────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200(self, client):
        with patch("app.main.redis_lib") as mock_redis, \
             patch("app.main.celery_app") as mock_celery:
            mock_redis.from_url.return_value.ping.return_value = True
            mock_celery.control.inspect.return_value.ping.return_value = {"worker1": True}
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_health_response_shape(self, client):
        with patch("app.main.redis_lib"), patch("app.main.celery_app"):
            resp = client.get("/api/v1/health")
        data = resp.json()
        assert "status" in data
        assert "version" in data
        assert "database" in data


# ── Submit prompt ──────────────────────────────────────────────────────────────

class TestSubmitPrompt:
    @patch("app.main.process_prompt")
    def test_submit_returns_202(self, mock_task, client):
        mock_task.apply_async.return_value = MagicMock(id="task-abc-123")
        resp = client.post("/api/v1/prompts", json={
            "prompt": "Explain recursion simply",
            "provider": "anthropic",
        })
        assert resp.status_code == 202

    @patch("app.main.process_prompt")
    def test_submit_returns_request_id(self, mock_task, client):
        mock_task.apply_async.return_value = MagicMock(id="task-xyz")
        resp = client.post("/api/v1/prompts", json={"prompt": "Hello world"})
        data = resp.json()
        assert "request_id" in data
        assert data["status"] == "queued"

    def test_submit_rejects_empty_prompt(self, client):
        resp = client.post("/api/v1/prompts", json={"prompt": "   "})
        assert resp.status_code == 422

    def test_submit_rejects_invalid_temperature(self, client):
        resp = client.post("/api/v1/prompts", json={
            "prompt": "Test",
            "temperature": 5.0,
        })
        assert resp.status_code == 422

    def test_submit_rejects_invalid_priority(self, client):
        resp = client.post("/api/v1/prompts", json={
            "prompt": "Test",
            "priority": 99,
        })
        assert resp.status_code == 422

    @patch("app.main.process_prompt")
    def test_submit_stores_in_db(self, mock_task, client, db):
        mock_task.apply_async.return_value = MagicMock(id="task-store")
        client.post("/api/v1/prompts", json={"prompt": "Store this please"})
        row = db.query(PromptRequest).filter(PromptRequest.prompt == "Store this please").first()
        assert row is not None
        assert row.status == "queued"


# ── Bulk submit ────────────────────────────────────────────────────────────────

class TestBulkSubmit:
    @patch("app.main.process_prompt")
    def test_bulk_submit(self, mock_task, client):
        mock_task.apply_async.return_value = MagicMock(id="task-bulk")
        resp = client.post("/api/v1/prompts/bulk", json={
            "requests": [
                {"prompt": "Prompt A"},
                {"prompt": "Prompt B"},
                {"prompt": "Prompt C"},
            ]
        })
        assert resp.status_code == 202
        data = resp.json()
        assert data["submitted"] == 3
        assert len(data["request_ids"]) == 3


# ── Get prompt status ──────────────────────────────────────────────────────────

class TestGetPrompt:
    def test_get_existing_request(self, client, sample_request):
        resp = client.get(f"/api/v1/prompts/{sample_request.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["prompt"] == "What is the capital of France?"

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get(f"/api/v1/prompts/{uuid4()}")
        assert resp.status_code == 404

    def test_get_includes_response(self, client, sample_request_with_response):
        resp = client.get(f"/api/v1/prompts/{sample_request_with_response.id}")
        data = resp.json()
        assert data["response"] is not None
        assert data["response"]["content"] == "The capital of France is Paris."
        assert data["response"]["from_cache"] is False


# ── List prompts ───────────────────────────────────────────────────────────────

class TestListPrompts:
    @patch("app.main.process_prompt")
    def test_list_returns_all(self, mock_task, client, db):
        mock_task.apply_async.return_value = MagicMock(id="t1")
        for i in range(3):
            req = PromptRequest(prompt=f"Prompt {i}", model="claude-3-haiku-20240307",
                                provider="anthropic", status="completed")
            db.add(req)
        db.commit()
        resp = client.get("/api/v1/prompts")
        assert resp.status_code == 200
        assert len(resp.json()) >= 3

    def test_list_filter_by_status(self, client, db):
        for status in ["completed", "completed", "failed"]:
            req = PromptRequest(prompt="x", model="m", provider="anthropic", status=status)
            db.add(req)
        db.commit()
        resp = client.get("/api/v1/prompts?status=completed")
        results = resp.json()
        assert all(r["status"] == "completed" for r in results)

    def test_list_pagination(self, client, db):
        for i in range(10):
            db.add(PromptRequest(prompt=f"P{i}", model="m", provider="anthropic", status="completed"))
        db.commit()
        resp = client.get("/api/v1/prompts?limit=3&offset=0")
        assert len(resp.json()) == 3


# ── Cancel prompt ──────────────────────────────────────────────────────────────

class TestCancelPrompt:
    def test_cancel_queued_request(self, client, db):
        req = PromptRequest(prompt="cancel me", model="m", provider="anthropic",
                            status="queued", task_id="task-cancel")
        db.add(req)
        db.commit()
        with patch("app.main.celery_app") as mock_celery:
            resp = client.delete(f"/api/v1/prompts/{req.id}")
        assert resp.status_code == 204
        db.refresh(req)
        assert req.status == "failed"

    def test_cannot_cancel_completed(self, client, sample_request):
        resp = client.delete(f"/api/v1/prompts/{sample_request.id}")
        assert resp.status_code == 409


# ── Rate limit service ─────────────────────────────────────────────────────────

class TestRateLimitService:
    def test_rate_limit_status_endpoint(self, client, db):
        resp = client.get("/api/v1/rate-limit/anthropic")
        assert resp.status_code == 200
        data = resp.json()
        assert "tokens_available" in data or "tokens" in data

    def test_token_bucket_created_on_first_check(self, db):
        from app.services.rate_limit_service import RateLimitService
        svc = RateLimitService(db)
        # Should not raise
        try:
            svc.check_and_consume("test_provider")
        except Exception:
            pass  # May raise RateLimitExceeded but bucket should be created
        bucket = db.query(RateLimitBucket).filter(
            RateLimitBucket.provider == "test_provider"
        ).first()
        assert bucket is not None

    def test_token_bucket_decrements(self, db):
        from app.services.rate_limit_service import RateLimitService
        svc = RateLimitService(db)
        svc.check_and_consume("anthropic")
        bucket = db.query(RateLimitBucket).filter(
            RateLimitBucket.provider == "anthropic"
        ).first()
        assert bucket.tokens < bucket.max_tokens


# ── Cache service ──────────────────────────────────────────────────────────────

class TestCacheService:
    def test_cache_miss_returns_none(self, db):
        from app.services.cache_service import CacheService
        with patch("app.services.cache_service._get_embedding", return_value=None):
            svc = CacheService(db)
            result = svc.get("unknown prompt xyz", "model", "anthropic")
        assert result is None

    def test_cache_exact_match(self, db):
        from app.services.cache_service import CacheService, _sha256
        from datetime import datetime, timedelta
        entry = SemanticCache(
            prompt_hash=_sha256("exact prompt"),
            prompt_text="exact prompt",
            embedding=None,
            response_content="cached response",
            model="claude-3-haiku-20240307",
            provider="anthropic",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(entry)
        db.commit()

        with patch("app.services.cache_service._get_embedding", return_value=None):
            svc = CacheService(db)
            result = svc.get("exact prompt", "claude-3-haiku-20240307", "anthropic")

        assert result is not None
        assert result[0] == "cached response"

    def test_cache_set_stores_entry(self, db):
        from app.services.cache_service import CacheService
        with patch("app.services.cache_service._get_embedding", return_value=None):
            svc = CacheService(db)
            svc.set("test prompt", "test response", "model", "anthropic", tokens_used=10)
        count = db.query(SemanticCache).count()
        assert count == 1


# ── Provider service ───────────────────────────────────────────────────────────

class TestProviderService:
    def test_anthropic_provider_called(self):
        from app.services.provider_service import ProviderService
        svc = ProviderService()
        with patch.object(svc, "_call_anthropic") as mock_call:
            mock_call.return_value = {"content": "test response", "tokens": 50}
            result = svc.complete("Hello", "anthropic", "claude-3-haiku-20240307")
        assert result.content == "test response"
        assert result.tokens_used == 50
        assert result.provider == "anthropic"

    def test_openai_provider_called(self):
        from app.services.provider_service import ProviderService
        svc = ProviderService()
        with patch.object(svc, "_call_openai") as mock_call:
            mock_call.return_value = {"content": "openai response", "tokens": 30}
            result = svc.complete("Hello", "openai", "gpt-4o-mini")
        assert result.content == "openai response"
        assert result.provider == "openai"

    def test_unknown_provider_raises(self):
        from app.services.provider_service import ProviderService
        svc = ProviderService()
        with pytest.raises(ValueError, match="Unknown provider"):
            svc.complete("Hello", "unknown_provider", "some-model")

    def test_cost_calculation(self):
        from app.services.provider_service import ProviderService
        svc = ProviderService()
        with patch.object(svc, "_call_anthropic") as mock_call:
            mock_call.return_value = {"content": "r", "tokens": 1000}
            result = svc.complete("prompt", "anthropic", "claude-3-haiku-20240307")
        assert result.cost_usd > 0
