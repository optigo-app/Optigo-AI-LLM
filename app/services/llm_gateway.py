from typing import Any, AsyncIterator, Dict, List, Literal, Optional

import asyncio
import httpx

from app.config import settings
from app.middleware.audit_log import log_llm_call, AuditTimer
from app.middleware.circuit_breaker import get_breaker, CircuitOpenError
from app.middleware.logging import get_logger

logger = get_logger(__name__)

# Shared httpx client for LLM API calls — reuses TLS connections across requests.
# The OpenAI/Anthropic SDK clients are lightweight wrappers; the httpx client
# is the expensive part (connection pool, TLS handshake).
_llm_http_client: Optional[httpx.AsyncClient] = None


def _get_llm_http_client(timeout: float = 60.0) -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient for LLM provider calls."""
    global _llm_http_client
    if _llm_http_client is None or _llm_http_client.is_closed:
        _llm_http_client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        )
    return _llm_http_client


class LLMGatewayError(Exception):
    """Raised when an LLM gateway call fails."""
    pass


class ChatResult:
    """Text plus token usage from a completion call."""

    def __init__(self, text: str, usage: Dict[str, int]):
        self.text = text
        self.usage = usage


# Approximate cost per 1K tokens (in USD) by provider.
# Used for cost tracking/estimation only — update with actual pricing.
_COST_PER_1K: Dict[str, Dict[str, float]] = {
    "gemini": {"prompt": 0.000075, "completion": 0.0003},
    "openai": {"prompt": 0.00015, "completion": 0.0006},
    "anthropic": {"prompt": 0.00025, "completion": 0.00125},
    "mistral": {"prompt": 0.0001, "completion": 0.0003},
    "groq": {"prompt": 0.00005, "completion": 0.0001},
    "openrouter": {"prompt": 0.0001, "completion": 0.0003},
}


def _estimate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost for a token usage."""
    rates = _COST_PER_1K.get(provider, {"prompt": 0.0001, "completion": 0.0003})
    return (prompt_tokens / 1000 * rates["prompt"]) + (completion_tokens / 1000 * rates["completion"])


class EmbeddingResult:
    """Embedding vector plus token usage."""

    def __init__(self, embedding: List[float], usage: Dict[str, int]):
        self.embedding = embedding
        self.usage = usage


def _resolve_model(provider: str, model: str) -> str:
    """Map symbolic model names to likely API model IDs."""
    aliases: Dict[str, Dict[str, str]] = {
        "gemini": {
            "gemini-flash-lite": "gemini-3.5-flash-lite",
            "gemini-flash": "gemini-3.5-flash",
            "gemini-pro": "gemini-3.1-pro-preview",
            # Also support latest aliases
            "gemini-flash-latest": "gemini-flash-latest",
            "gemini-flash-lite-latest": "gemini-flash-lite-latest",
            "gemini-pro-latest": "gemini-pro-latest",
        },
        "openai": {},
        "anthropic": {},
    }
    return aliases.get(provider, {}).get(model, model)


def _get_provider_config(tier: Literal["cheap", "strong"]) -> tuple[str, str, str, str]:
    """Return (provider, model, api_key, base_url) for the requested tier."""
    if tier == "cheap":
        provider = settings.cheap_llm_provider.lower()
        model = settings.cheap_llm_model
        if provider == "gemini":
            api_key, base_url = settings.gemini_api_key, settings.gemini_base_url
        elif provider == "openai":
            api_key, base_url = settings.openai_api_key, settings.openai_base_url
        elif provider == "anthropic":
            api_key, base_url = settings.anthropic_api_key, settings.anthropic_base_url
        elif provider == "mistral":
            api_key, base_url = settings.mistral_api_key, settings.mistral_base_url
        elif provider == "groq":
            api_key, base_url = settings.groq_api_key, settings.groq_base_url
        elif provider == "openrouter":
            api_key, base_url = settings.openrouter_api_key, settings.openrouter_base_url
        else:
            raise LLMGatewayError(f"Unsupported cheap LLM provider: {provider}")
    elif tier == "strong":
        provider = settings.strong_llm_provider.lower()
        model = settings.strong_llm_model
        if provider == "gemini":
            api_key, base_url = settings.gemini_api_key, settings.gemini_base_url
        elif provider == "openai":
            api_key, base_url = settings.openai_api_key, settings.openai_base_url
        elif provider == "anthropic":
            api_key, base_url = settings.anthropic_api_key, settings.anthropic_base_url
        elif provider == "mistral":
            api_key, base_url = settings.mistral_api_key, settings.mistral_base_url
        elif provider == "groq":
            api_key, base_url = settings.groq_api_key, settings.groq_base_url
        elif provider == "openrouter":
            api_key, base_url = settings.openrouter_api_key, settings.openrouter_base_url
        else:
            raise LLMGatewayError(f"Unsupported strong LLM provider: {provider}")
    else:
        raise LLMGatewayError(f"Unknown tier: {tier}")

    model = _resolve_model(provider, model)
    return provider, model, api_key, base_url


async def chat(
    tier: Literal["cheap", "strong"],
    messages: List[Dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 1024,
    response_format: Optional[Dict[str, Any]] = None,
) -> ChatResult:
    """Single choke point for all LLM calls. Returns the model's text response.

    Falls back to the alternate tier if the primary provider fails.
    """
    try:
        return await _chat_single(tier, messages, temperature, max_tokens, response_format)
    except (CircuitOpenError, LLMGatewayError) as primary_exc:
        # Try the alternate tier as fallback
        fallback_tier = "strong" if tier == "cheap" else "cheap"
        try:
            logger.warning(
                "Primary LLM (%s) failed, falling back to %s tier: %s",
                tier, fallback_tier, primary_exc,
            )
            return await _chat_single(fallback_tier, messages, temperature, max_tokens, response_format)
        except Exception as fallback_exc:
            logger.error("Fallback LLM (%s) also failed: %s", fallback_tier, fallback_exc)
            raise primary_exc


async def _chat_single(
    tier: Literal["cheap", "strong"],
    messages: List[Dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 1024,
    response_format: Optional[Dict[str, Any]] = None,
) -> ChatResult:
    """Make a single LLM call without fallback."""
    provider, model, api_key, base_url = _get_provider_config(tier)

    if not api_key:
        raise LLMGatewayError(
            f"No API key configured for {provider}. Set the corresponding API key in the environment."
        )

    breaker = get_breaker(provider)
    if not breaker.allow_request():
        raise CircuitOpenError(provider)

    # Build a short summary of the messages for the audit log
    msgs_summary = " | ".join(
        f"{m.get('role','?')}: {str(m.get('content',''))[:80]}" for m in messages[:3]
    )

    timer = AuditTimer()
    try:
        if provider in ("openai", "gemini", "mistral", "groq", "openrouter"):
            with timer:
                result = await _call_openai_compat(provider, base_url, api_key, model, messages, temperature, max_tokens, response_format)
            breaker.record_success()
            log_llm_call(
                caller="chat",
                tier=tier, provider=provider, model=model,
                messages_summary=msgs_summary,
                temperature=temperature, max_tokens=max_tokens,
                response_text=result.text,
                prompt_tokens=result.usage.get("prompt_tokens", 0),
                completion_tokens=result.usage.get("completion_tokens", 0),
                estimated_cost_usd=result.usage.get("estimated_cost_usd", 0.0),
                latency_ms=timer.ms,
                success=True,
                full_messages=messages,
            )
            return result
        if provider == "anthropic":
            with timer:
                result = await _call_anthropic(api_key, model, messages, temperature, max_tokens)
            breaker.record_success()
            log_llm_call(
                caller="chat",
                tier=tier, provider=provider, model=model,
                messages_summary=msgs_summary,
                temperature=temperature, max_tokens=max_tokens,
                response_text=result.text,
                prompt_tokens=result.usage.get("prompt_tokens", 0),
                completion_tokens=result.usage.get("completion_tokens", 0),
                estimated_cost_usd=result.usage.get("estimated_cost_usd", 0.0),
                latency_ms=timer.ms,
                success=True,
                full_messages=messages,
            )
            return result

        raise LLMGatewayError(f"Provider {provider} is not yet supported by the gateway.")
    except Exception as exc:
        breaker.record_failure()
        log_llm_call(
            caller="chat",
            tier=tier, provider=provider, model=model,
            messages_summary=msgs_summary,
            temperature=temperature, max_tokens=max_tokens,
            latency_ms=timer.ms,
            success=False,
            error=str(exc),
            full_messages=messages,
        )
        raise


async def chat_stream(
    tier: Literal["cheap", "strong"],
    messages: List[Dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> AsyncIterator[str]:
    """Streaming version of chat() — yields text chunks as they arrive.

    Supports OpenAI-compatible providers (openai, gemini, mistral, groq) and Anthropic.
    Does not support response_format (streaming JSON mode is unreliable across providers).
    """
    provider, model, api_key, base_url = _get_provider_config(tier)

    if not api_key:
        raise LLMGatewayError(
            f"No API key configured for {provider}. Set the corresponding API key in the environment."
        )

    if provider in ("openai", "gemini", "mistral", "groq", "openrouter"):
        breaker = get_breaker(provider)
        if not breaker.allow_request():
            raise CircuitOpenError(provider)
        try:
            async for chunk in _stream_openai_compat(provider, base_url, api_key, model, messages, temperature, max_tokens):
                yield chunk
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()
    elif provider == "anthropic":
        breaker = get_breaker(provider)
        if not breaker.allow_request():
            raise CircuitOpenError(provider)
        try:
            async for chunk in _stream_anthropic(api_key, model, messages, temperature, max_tokens):
                yield chunk
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()
    else:
        raise LLMGatewayError(f"Provider {provider} is not yet supported for streaming.")


async def _stream_openai_compat(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> AsyncIterator[str]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=_get_llm_http_client(timeout=120.0),
    )
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for event in stream:
            if event.choices and event.choices[0].delta.content:
                yield event.choices[0].delta.content
    except Exception as exc:
        raise LLMGatewayError(f"OpenAI-compatible streaming error: {exc}") from exc


async def _stream_anthropic(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> AsyncIterator[str]:
    from anthropic import AsyncAnthropic

    system: Optional[str] = None
    chat_messages: List[Dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system" and system is None:
            system = msg.get("content", "")
        else:
            chat_messages.append({"role": msg["role"], "content": msg["content"]})

    client = AsyncAnthropic(
        api_key=api_key,
        base_url=settings.anthropic_base_url,
        http_client=_get_llm_http_client(timeout=120.0),
    )
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=chat_messages,  # type: ignore[arg-type]
            system=system,
            temperature=temperature,
        ) as stream:
            async for text in stream.text_stream:
                yield text
    except Exception as exc:
        raise LLMGatewayError(f"Anthropic streaming error: {exc}") from exc


async def _with_retry(func, *args, **kwargs):
    """Retry an async callable with exponential backoff.

    Skips retries for client errors (4xx) since those won't succeed on retry.
    Only retries on server errors (5xx), timeouts, and connection errors.
    """
    max_retries = settings.llm_max_retries
    base_delay = settings.llm_retry_base_delay
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            # Don't retry 4xx client errors (bad request, auth, not found, etc.)
            status = getattr(exc, "status_code", None)
            if status is None:
                # OpenAI SDK wraps HTTP errors — check response attribute
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None) if resp is not None else None
            if status is not None:
                if 400 <= status < 500:
                    logger.error("LLM call failed (client error %s, not retrying): %s", status, exc)
                    raise
            else:
                # Fallback: check exception message for HTTP status codes
                exc_str = str(exc).lower()
                if any(code in exc_str for code in ("400", "401", "403", "404", "422", "429")):
                    logger.error("LLM call failed (client error, not retrying): %s", exc)
                    raise
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries + 1, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("LLM call failed after %d retries: %s", max_retries, exc)
    raise last_exc


async def _call_openai_compat(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
    response_format: Optional[Dict[str, Any]] = None,
) -> ChatResult:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=_get_llm_http_client(timeout=60.0),
    )
    try:
        kwargs = {
            "model": model,
            "messages": messages,  # type: ignore[arg-type]
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = await _with_retry(client.chat.completions.create, **kwargs)
    except Exception as exc:
        raise LLMGatewayError(f"OpenAI-compatible API error: {exc}") from exc

    content = response.choices[0].message.content
    if content is None:
        # Reasoning models (GPT-OSS, Ling, Nemotron) may put output in reasoning
        reasoning = getattr(response.choices[0].message, "reasoning", None)
        reasoning_content = getattr(response.choices[0].message, "reasoning_content", None)
        content = reasoning or reasoning_content
        if content is None:
            raise LLMGatewayError("OpenAI-compatible API returned empty content")
    usage = {
        "provider": provider,
        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
        "estimated_cost_usd": round(_estimate_cost(
            provider,
            getattr(response.usage, "prompt_tokens", 0) or 0,
            getattr(response.usage, "completion_tokens", 0) or 0,
        ), 6),
    }
    return ChatResult(content, usage)


async def _call_anthropic(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> ChatResult:
    from anthropic import AsyncAnthropic

    # Anthropic expects a separate system string and non-system messages.
    system: Optional[str] = None
    chat_messages: List[Dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system" and system is None:
            system = msg.get("content", "")
        else:
            chat_messages.append({"role": msg["role"], "content": msg["content"]})

    client = AsyncAnthropic(
        api_key=api_key,
        base_url=settings.anthropic_base_url,
        http_client=_get_llm_http_client(timeout=60.0),
    )
    system_payload: Any = system
    if settings.prompt_caching and system:
        system_payload = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    try:
        response = await _with_retry(
            client.messages.create,
            model=model,
            max_tokens=max_tokens,
            messages=chat_messages,  # type: ignore[arg-type]
            system=system_payload,
            temperature=temperature,
        )
    except Exception as exc:
        raise LLMGatewayError(f"Anthropic API error: {exc}") from exc

    content = response.content[0].text
    usage_obj = getattr(response, "usage", None)
    input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
    output_tokens = getattr(usage_obj, "output_tokens", 0) or 0
    usage = {
        "provider": "anthropic",
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": round(_estimate_cost("anthropic", input_tokens, output_tokens), 6),
    }
    return ChatResult(content, usage)


UNCERTAINTY_MARKERS: List[str] = [
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "uncertain",
    "not enough information",
    "insufficient information",
    "no data available",
    "does not provide",
    "unable to determine",
    "cannot determine",
]


def is_uncertain(text: str) -> bool:
    """Heuristic to detect when an LLM output is ungrounded or unsure."""
    lowered = text.lower()
    return any(marker in lowered for marker in UNCERTAINTY_MARKERS)


def handoff_message() -> str:
    """Plain human hand-off message used when both LLM tiers are ungrounded."""
    return (
        "I'm not able to answer that confidently from the report data. "
        "Please contact OptigoApps support for assistance with this question."
    )


async def get_embedding(text: str, model: Optional[str] = None) -> EmbeddingResult:
    """Generate an embedding vector for the given text.

    Supports OpenAI `text-embedding-*` models and Gemini `text-embedding-004`.
    """
    model_name = model or settings.embedding_model

    if "text-embedding" in model_name:
        if not settings.openai_api_key:
            raise LLMGatewayError(
                "OpenAI API key not configured. Set OPENAI_API_KEY to use text-embedding models."
            )
        return await _call_openai_embedding(settings.openai_base_url, settings.openai_api_key, model_name, text)

    # Gemini embedding support (text-embedding-004 or gemini-embedding-001)
    if "embedding" in model_name and (settings.gemini_api_key or settings.openai_api_key):
        # Use Gemini via OpenAI-compatible endpoint
        api_key = settings.gemini_api_key or settings.openai_api_key
        base_url = settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta/openai/"
        return await _call_openai_embedding(base_url, api_key, model_name, text)

    raise LLMGatewayError(
        f"Embedding model '{model_name}' is not supported by the current gateway. "
        "Supported patterns: text-embedding-* (OpenAI) or *-embedding-* (Gemini)"
    )


async def get_embeddings(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """Generate embedding vectors for a list of texts in one API call.

    Returns a list of embeddings in the same order as the input.
    """
    model_name = model or settings.embedding_model

    if "text-embedding" in model_name:
        if not settings.openai_api_key:
            raise LLMGatewayError(
                "OpenAI API key not configured. Set OPENAI_API_KEY to use text-embedding models."
            )
        return await _call_openai_embeddings(settings.openai_base_url, settings.openai_api_key, model_name, texts)

    # Gemini embedding support
    if "embedding" in model_name and (settings.gemini_api_key or settings.openai_api_key):
        api_key = settings.gemini_api_key or settings.openai_api_key
        base_url = settings.gemini_base_url or "https://generativelanguage.googleapis.com/v1beta/openai/"
        return await _call_openai_embeddings(base_url, api_key, model_name, texts)

    raise LLMGatewayError(
        f"Embedding model '{model_name}' is not supported by the current gateway. "
        "Supported patterns: text-embedding-* (OpenAI) or *-embedding-* (Gemini)"
    )


async def _call_openai_embedding(base_url: str, api_key: str, model: str, text: str) -> EmbeddingResult:
    from openai import AsyncOpenAI

    async with AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.AsyncClient(timeout=60.0),
    ) as client:
        try:
            response = await client.embeddings.create(
                model=model,
                input=text,
            )
        except Exception as exc:
            raise LLMGatewayError(f"OpenAI embedding API error: {exc}") from exc

    if not response.data:
        raise LLMGatewayError("OpenAI embedding API returned no data")
    embedding = response.data[0].embedding
    usage = {
        "provider": "openai",
        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": 0,
        "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
    }
    return EmbeddingResult(embedding, usage)


async def _call_openai_embeddings(base_url: str, api_key: str, model: str, texts: List[str]) -> List[List[float]]:
    from openai import AsyncOpenAI

    async with AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.AsyncClient(timeout=60.0),
    ) as client:
        try:
            response = await client.embeddings.create(
                model=model,
                input=texts,
            )
        except Exception as exc:
            raise LLMGatewayError(f"OpenAI embedding API error: {exc}") from exc

    if not response.data:
        raise LLMGatewayError("OpenAI embedding API returned no data")
    return [item.embedding for item in response.data]

