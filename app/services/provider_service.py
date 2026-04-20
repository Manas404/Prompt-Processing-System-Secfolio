import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str
    tokens_used: int
    cost_usd: float
    latency_ms: int
    model: str
    provider: str


# Cost per 1K tokens (input + output blended estimate)
COST_TABLE = {
    "anthropic": {
        "claude-3-haiku-20240307": 0.00025,
        "claude-3-sonnet-20240229": 0.003,
        "claude-3-opus-20240229": 0.015,
        "claude-sonnet-4-20250514": 0.003,
    },
    "openai": {
        "gpt-4o-mini": 0.00015,
        "gpt-4o": 0.005,
        "gpt-4-turbo": 0.01,
    },
}


def _estimate_cost(provider: str, model: str, tokens: int) -> float:
    rate = COST_TABLE.get(provider, {}).get(model, 0.002)
    return round((tokens / 1000) * rate, 6)


class ProviderService:
    """
    Unified LLM provider abstraction.
    Supports Anthropic and OpenAI with a consistent interface.
    """

    def complete(
        self,
        prompt: str,
        provider: str,
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
    ) -> LLMResponse:
        t0 = time.monotonic()

        if provider == "anthropic":
            result = self._call_anthropic(prompt, model, max_tokens, temperature)
        elif provider == "openai":
            result = self._call_openai(prompt, model, max_tokens, temperature)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        latency_ms = int((time.monotonic() - t0) * 1000)
        cost = _estimate_cost(provider, model, result["tokens"])

        return LLMResponse(
            content=result["content"],
            tokens_used=result["tokens"],
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            provider=provider,
        )

    # ── Provider implementations ───────────────────────────────────────────────

    def _call_anthropic(
        self, prompt: str, model: str, max_tokens: int, temperature: float
    ) -> dict:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model=model or settings.DEFAULT_MODEL,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            content = msg.content[0].text
            tokens = msg.usage.input_tokens + msg.usage.output_tokens
            return {"content": content, "tokens": tokens}
        except Exception as exc:
            logger.error("Anthropic API error: %s", exc)
            raise

    def _call_openai(
        self, prompt: str, model: str, max_tokens: int, temperature: float
    ) -> dict:
        try:
            import openai
            client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=model or "gpt-4o-mini",
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            content = resp.choices[0].message.content
            tokens = resp.usage.total_tokens
            return {"content": content, "tokens": tokens}
        except Exception as exc:
            logger.error("OpenAI API error: %s", exc)
            raise
