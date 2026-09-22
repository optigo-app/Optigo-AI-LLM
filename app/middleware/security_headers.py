"""Security headers middleware — FastAPI equivalent of Node.js helmet.

Adds standard security HTTP headers to every response:
  - X-Content-Type-Options: nosniff (prevents MIME sniffing)
  - X-Frame-Options: DENY (prevents clickjacking)
  - X-XSS-Protection: 0 (disables legacy XSS auditor — modern browsers handle this)
  - Referrer-Policy: strict-origin-when-cross-origin
  - X-Permitted-Cross-Domain-Policies: none (blocks Flash/PDF cross-domain)
  - Cross-Origin-Opener-Policy: same-origin (process isolation)
  - Cross-Origin-Resource-Policy: same-origin (resource isolation)
  - Content-Security-Policy: default-src 'self' (restricts resource loading)

These headers work alongside CORSMiddleware to provide defense-in-depth.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response
