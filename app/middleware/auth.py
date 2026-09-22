import json
import re
from typing import Any, Dict, Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware


_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
_PUBLIC_PATHS = {"/health", "/reports", "/static", "/metrics", "/v1/health", "/v1/reports"}


def _extract_cookie_user(cookie_header: str) -> Optional[Dict[str, Any]]:
    """Parse the userData cookie that OptigoApps sets on login."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("userData="):
            raw = part[len("userData="):]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
    return None


def validate_session_id(sid: Optional[str]) -> Optional[str]:
    """Sanitize session_id to prevent injection; return None if invalid."""
    if sid is None:
        return None
    if _SESSION_ID_RE.match(sid):
        return sid
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract OptigoApps session from cookie and inject identity into request state.

    Skips auth for public paths (/health, /reports, /static/*).
    In development (USE_DUMMY_DB=true), allows requests without a cookie
    so the chat UI can be tested with manual company_code/user_id.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        for prefix in _PUBLIC_PATHS:
            if path == prefix or path.startswith(prefix + "/"):
                return await call_next(request)

        # Extract user from cookie
        cookie_header = request.headers.get("cookie", "")
        user_data = _extract_cookie_user(cookie_header)

        if user_data:
            request.state.user = user_data
            request.state.company_code = user_data.get("companycode", "")
            request.state.user_id = user_data.get("userid", "")
            request.state.token = user_data.get("token", "")
        else:
            request.state.user = None
            request.state.company_code = None
            request.state.user_id = None
            request.state.token = None

        # Require a valid cookie for authentication
        # In dev mode (AUTH_REQUIRED=false), allow requests without a cookie
        from app.config import settings
        if not user_data and settings.auth_required:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Please log in to OptigoApps.",
            )

        return await call_next(request)
