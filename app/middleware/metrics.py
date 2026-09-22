"""Prometheus metrics for monitoring application health and performance."""

from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# Request metrics
REQUEST_COUNT = Counter(
    "chatbot_requests_total",
    "Total requests by endpoint and status",
    ["endpoint", "method", "status"],
)

REQUEST_LATENCY = Histogram(
    "chatbot_request_duration_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# LLM metrics
LLM_CALLS = Counter(
    "chatbot_llm_calls_total",
    "Total LLM calls by provider, tier, and status",
    ["provider", "tier", "status"],
)

LLM_LATENCY = Histogram(
    "chatbot_llm_duration_seconds",
    "LLM call latency in seconds",
    ["provider", "tier"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 60.0],
)

LLM_TOKENS = Counter(
    "chatbot_llm_tokens_total",
    "Total tokens consumed by type",
    ["provider", "type"],  # type: prompt, completion
)

# Cache metrics
CACHE_HITS = Counter(
    "chatbot_cache_hits_total",
    "Total cache hits",
)

CACHE_MISSES = Counter(
    "chatbot_cache_misses_total",
    "Total cache misses",
)

CACHE_SIZE = Gauge(
    "chatbot_cache_size",
    "Number of items in the semantic cache",
)

# Classification metrics
CLASSIFICATION_RESULTS = Counter(
    "chatbot_classification_total",
    "Total classifications by result",
    ["result"],  # result: matched, fallback, none
)

# Active sessions
ACTIVE_SESSIONS = Gauge(
    "chatbot_active_sessions",
    "Approximate number of active sessions",
)
