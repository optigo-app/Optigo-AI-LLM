import httpx
from typing import Any, Dict, Optional

from app.config import settings
from app.models import ReportRegistryEntry
from app.services.validator import load_registry

# Shared connection pool for Node API calls (reused across requests).
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def get_report_registry() -> Dict[str, ReportRegistryEntry]:
    """Load the registry once per module import."""
    return load_registry(settings.registry_path)


def _build_call_params(
    entry: ReportRegistryEntry,
    validated_filters: Dict[str, Any],
    company_code: str,
    user_id: str,
) -> Dict[str, Any]:
    """Merge validated filters with the identity params the Node API uses for permission."""
    params: Dict[str, Any] = {
        "company_code": company_code,
        "user_id": user_id,
    }
    for field_name, value in validated_filters.items():
        if value is not None:
            params[field_name] = value
    return params


async def call_report_api(
    report_key: str,
    validated_filters: Dict[str, Any],
    company_code: str,
    user_id: str,
    registry: Optional[Dict[str, ReportRegistryEntry]] = None,
) -> Dict[str, Any]:
    """Call the registry-defined Node/Express endpoint for a report.

    No SQL is used; no permission logic lives in the chatbot. The Node API is the
    single source of truth for what this user is allowed to see.
    """
    if registry is None:
        registry = get_report_registry()

    entry = registry.get(report_key)
    if entry is None:
        raise ValueError(f"Unknown report key: {report_key}")

    params = _build_call_params(entry, validated_filters, company_code, user_id)

    url = settings.node_api_base.rstrip("/") + entry.api_endpoint
    client = _get_client()

    method = entry.method.upper()
    if method == "GET":
        response = await client.get(url, params=params)
    elif method == "POST":
        response = await client.post(url, json=params)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise ReportApiError(
            f"Report API returned {exc.response.status_code}: {body}",
            status_code=exc.response.status_code,
            body=body,
        ) from exc

    return response.json()


async def export_report_api(
    report_key: str,
    validated_filters: Dict[str, Any],
    company_code: str,
    user_id: str,
    registry: Optional[Dict[str, ReportRegistryEntry]] = None,
) -> str:
    """Call the Node/Express export endpoint for a report and return a download URL.

    The export endpoint is assumed to live at `<report_api_endpoint>/export`.
    It must accept the same permission params (company_code, user_id) and validated
    filters, and return JSON containing a `download_url`.
    """
    if registry is None:
        registry = get_report_registry()

    entry = registry.get(report_key)
    if entry is None:
        raise ValueError(f"Unknown report key: {report_key}")

    params = _build_call_params(entry, validated_filters, company_code, user_id)

    url = settings.node_api_base.rstrip("/") + entry.api_endpoint.rstrip("/") + "/export"

    client = _get_client()
    response = await client.post(url, json=params)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise ReportApiError(
            f"Export API returned {exc.response.status_code}: {body}",
            status_code=exc.response.status_code,
            body=body,
        ) from exc

    data = response.json()
    download_url = data.get("download_url") if isinstance(data, dict) else None
    if not download_url:
        raise ReportApiError(
            "Export API response missing download_url",
            status_code=response.status_code,
            body=response.text,
        )
    return download_url


class ReportApiError(Exception):
    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
