"""Token budget management — estimate prompt size and truncate payloads to fit model context windows."""

import json
import math
from typing import Any, Dict, List, Optional

from app.config import settings
from app.middleware.logging import get_logger

logger = get_logger(__name__)

# Approximate context window sizes (in tokens) for common models.
# Conservative estimates — actual limits may be higher.
_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    # Anthropic
    "claude-3-7-sonnet-latest": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-haiku-latest": 200_000,
    "claude-3-opus-latest": 200_000,
    # Gemini
    "gemini-3.5-flash-lite": 1_000_000,
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.5-pro": 2_000_000,
    # Mistral
    "mistral-large-latest": 128_000,
    "mistral-small-latest": 32_000,
    # Groq
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-8b-instant": 128_000,
    "qwen/qwen3.8-27b": 128_000,
    "openai/gpt-oss-20b": 131_072,
    "openai/gpt-oss-120b": 131_072,
    "groq/compound-mini": 131_072,
    "groq/compound": 131_072,
    # OpenRouter free models
    "inclusionai/ling-3.0-flash-fin:free": 262_144,
    "inclusionai/ling-3.0-flash-sante:free": 262_144,
    "nvidia/nemotron-3-super-120b-a12b:free": 262_144,
    "nvidia/nemotron-3-ultra-550b-a55b:free": 1_000_000,
    "google/gemma-4-31b-it:free": 262_144,
}

# Default fallback if model is unknown
_DEFAULT_CONTEXT_WINDOW = 16_384

# Reserve tokens for the model's response
_RESERVED_FOR_COMPLETION = 512


def get_context_window(model: str) -> int:
    """Return the approximate context window size for a model."""
    # Try exact match first, then case-insensitive contains match
    if model in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[model]
    model_lower = model.lower()
    for key, size in _MODEL_CONTEXT_WINDOWS.items():
        if key in model_lower:
            return size
    return _DEFAULT_CONTEXT_WINDOW


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, math.ceil(len(text) / 4))


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """Estimate total tokens for a chat messages list."""
    total = 0
    for msg in messages:
        # ~4 tokens overhead per message (role markers, etc.)
        total += 4
        total += estimate_tokens(msg.get("content", ""))
    return total


def check_token_budget(
    messages: List[Dict[str, str]],
    model: str,
    max_tokens: int = 256,
) -> tuple[bool, int, int]:
    """Check if messages fit within the model's context window.

    Returns:
        (fits, used_tokens, available_tokens)
    """
    context_window = get_context_window(model)
    prompt_tokens = estimate_messages_tokens(messages)
    available = context_window - _RESERVED_FOR_COMPLETION - max_tokens
    fits = prompt_tokens <= available
    if not fits:
        logger.warning(
            "Token budget exceeded: estimated %d tokens, available %d for model %s",
            prompt_tokens, available, model,
        )
    return fits, prompt_tokens, available


def truncate_payload_for_budget(
    payload: Dict[str, Any],
    messages: List[Dict[str, str]],
    model: str,
    max_tokens: int = 256,
) -> Dict[str, Any]:
    """Truncate the data payload to fit within the model's token budget.

    Progressively reduces the sample rows until the total fits.
    """
    fits, used, available = check_token_budget(messages, model, max_tokens)
    if fits:
        return payload

    # Calculate how many tokens we need to cut
    overflow = used - available
    logger.info("Truncating payload: overflow ~%d tokens", overflow)

    if payload.get("mode") == "list" and "sample" in payload:
        sample = payload["sample"]
        # Each row is roughly equal in size; estimate tokens per row
        if sample:
            payload_json = json.dumps(payload, ensure_ascii=False, default=str)
            total_payload_tokens = estimate_tokens(payload_json)
            per_row = max(1, total_payload_tokens // max(1, len(sample)))
            max_rows = max(1, len(sample) - math.ceil(overflow / per_row))
            if max_rows < len(sample):
                payload = {**payload, "sample": sample[:max_rows], "truncated": True}
                logger.info("Truncated sample from %d to %d rows", len(sample), max_rows)
    elif payload.get("mode") == "aggregate":
        # For aggregate mode, truncate long string values
        data = payload.get("data", {})
        if isinstance(data, dict):
            truncated_data = {}
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 500:
                    truncated_data[key] = value[:500] + "...[truncated]"
                else:
                    truncated_data[key] = value
            payload = {**payload, "data": truncated_data}

    return payload
