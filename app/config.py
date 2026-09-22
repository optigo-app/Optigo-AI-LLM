from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Env-driven configuration."""

    # Required from .env — no hardcoded defaults so no secrets/URLs live in the repo.
    node_api_base: str = ""
    registry_path: str = "app/registry.json"
    cache_dir: str = "./cache_data"

    # Real report API (Optigoapps report endpoint)
    use_real_api: bool = False
    real_api_base_url: str = ""
    real_api_timeout: float = 60.0
    # TLS verification for the real API. Set to true in production (HTTPS).
    # Default false only for local dev where the API uses self-signed certs.
    real_api_verify_tls: bool = False
    # Static session values for the chatbot SP — all required from .env.
    real_api_yearcode: str = ""
    real_api_sp: int = 0
    real_api_sv: str = ""
    real_api_version: str = ""
    # Shared LLM-chat SP number. When > 0, chat-mode (GetLLMChatSummary) requests
    # route to this single metadata-driven SP instead of each report's own SP,
    # so the GetLLMChatSummary block doesn't need to be copied into 100+ report SPs.
    # 0 = disabled (use each report's own SP — legacy behaviour).
    real_api_llm_chat_sp: int = 0

    # Operational constants
    llm_safe_row_limit: int = 50
    grid_row_limit: int = 500
    cache_similarity_threshold: float = 0.93
    cache_ttl_seconds: int = 3600
    classifier_similarity_threshold: float = 0.60

    # Security
    auth_required: bool = True
    rate_limit_per_minute: int = 60
    max_question_length: int = 1000
    # CORS origins — comma-separated allowlist. Default "*" is dev-only.
    # Production should set CORS_ORIGINS="https://app.optigoapps.com,https://admin.optigoapps.com"
    cors_origins: str = "*"

    # LLM retry
    llm_max_retries: int = 1
    llm_retry_base_delay: float = 0.5

    # Model routing
    cheap_llm_provider: str = "gemini"
    cheap_llm_model: str = "gemini-flash-lite"
    strong_llm_provider: str = "anthropic"
    strong_llm_model: str = "claude-3-7-sonnet-latest"
    embedding_model: str = "text-embedding-3-small"

    # API keys and endpoints for the LLM gateway
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Wide response mode (structured blocks) — comma-separated company codes
    # Companies in this list get wide mode by default; others use normal text.
    # Leave empty to require explicit response_mode="wide" per request.
    wide_response_companies: str = ""

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()


def validate_startup() -> list[str]:
    """Check required configuration at startup. Returns a list of warning messages."""
    warnings = []
    s = settings

    cheap_provider = s.cheap_llm_provider.lower()
    strong_provider = s.strong_llm_provider.lower()

    key_map = {
        "gemini": "gemini_api_key",
        "openai": "openai_api_key",
        "anthropic": "anthropic_api_key",
        "mistral": "mistral_api_key",
        "groq": "groq_api_key",
    }

    for provider, key_attr in key_map.items():
        if provider in (cheap_provider, strong_provider) and not getattr(s, key_attr):
            warnings.append(f"{key_attr.upper()} is not set but {provider} is configured as a LLM provider")

    # Embeddings always need OpenAI key for now
    if not s.openai_api_key:
        warnings.append("OPENAI_API_KEY is not set – embeddings (classifier + cache) will not work")

    # Production security warnings
    if s.cors_origins == "*":
        warnings.append("CORS_ORIGINS is '*' (allow all) — set an explicit allowlist for production")

    if s.use_real_api:
        if not s.real_api_base_url:
            warnings.append("REAL_API_BASE_URL is not set — required when USE_REAL_API=true")
        if not s.real_api_yearcode:
            warnings.append("REAL_API_YEARCODE is not set — required when USE_REAL_API=true")
        if not s.real_api_sp:
            warnings.append("REAL_API_SP is not set — required when USE_REAL_API=true")
        if not s.real_api_verify_tls:
            warnings.append("REAL_API_VERIFY_TLS is false — enable TLS verification for production")
        if s.real_api_base_url.startswith("http://"):
            warnings.append(f"REAL_API_BASE_URL uses HTTP ({s.real_api_base_url}) — use HTTPS for production")

    if s.use_real_api and s.real_api_llm_chat_sp == 0:
        warnings.append("REAL_API_LLM_CHAT_SP is 0 — set to the deployed shared SP number for production")

    # Registry sync check
    try:
        from app.services.column_registry import check_registry_sync
        sync_warnings = check_registry_sync()
        warnings.extend(sync_warnings)
    except Exception as e:
        warnings.append(f"Registry sync check failed: {e}")

    return warnings
