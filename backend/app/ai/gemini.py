"""Gemini client wrapper: structured JSON output, timeout, and a fallback model.

Callers get `(parsed_model, model_id)` or an `AIUnavailable` they must handle with a deterministic fallback.
"""

import asyncio
from functools import lru_cache

import structlog
from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings

log = structlog.get_logger()


class AIUnavailable(RuntimeError):
    pass


@lru_cache
def _client() -> genai.Client:
    key = get_settings().gemini_api_key
    if not key:
        raise AIUnavailable("GEMINI_API_KEY not set")
    return genai.Client(api_key=key)


def _thinking(model: str) -> types.ThinkingConfig | None:
    # Light reasoning keeps verdicts fast (seconds, not tens of seconds) without hurting accuracy
    # on this task (scripts/eval_ai.py).
    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=512)
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    return None


async def generate_json[T: BaseModel](
    *, system: str, prompt: str, schema: type[T], temperature: float = 0.1
) -> tuple[T, str]:
    settings = get_settings()
    models = [settings.gemini_model, *settings.gemini_fallback_models.split(",")]
    last_error: Exception | None = None
    for model in dict.fromkeys(m.strip() for m in models if m.strip()):  # dedupe, keep order
        try:
            res = await asyncio.wait_for(
                _client().aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=temperature,
                        thinking_config=_thinking(model),
                    ),
                ),
                timeout=settings.ai_timeout_s,
            )
            parsed = (
                res.parsed if isinstance(res.parsed, schema) else schema.model_validate_json(res.text or "")
            )
            return parsed, res.model_version or model
        except AIUnavailable:
            raise
        except Exception as exc:  # timeout, quota, bad JSON…: log and try the next model
            last_error = exc
            log.warning("gemini_call_failed", model=model, error=f"{type(exc).__name__}: {str(exc)[:200]}")
    raise AIUnavailable(f"all Gemini models failed: {last_error}")
