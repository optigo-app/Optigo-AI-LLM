"""Circuit breaker for LLM provider fail-fast behavior."""

import time
from enum import Enum
from typing import Dict

from app.middleware.logging import get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple circuit breaker with failure threshold, cooldown, and half-open probe.

    State machine:
    - CLOSED: requests pass through normally. Failures increment the failure count.
    - OPEN: requests fail immediately without calling the provider. After cooldown,
      transition to HALF_OPEN.
    - HALF_OPEN: one probe request is allowed. If it succeeds, reset to CLOSED.
      If it fails, go back to OPEN.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.name = name
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker '%s' transitioning OPEN -> HALF_OPEN", self.name)
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        s = self.state
        if s == CircuitState.OPEN:
            return False
        return True

    def record_success(self) -> None:
        """Record a successful request — resets the breaker."""
        if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info("Circuit breaker '%s' resetting to CLOSED", self.name)
        self._state = CircuitState.CLOSED
        self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed request — may trip the breaker."""
        self._failure_count += 1
        if self._state == CircuitState.HALF_OPEN:
            self._trip()
            return
        if self._failure_count >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        logger.warning(
            "Circuit breaker '%s' tripped OPEN after %d failures (cooldown: %.0fs)",
            self.name, self._failure_count, self.cooldown_seconds,
        )


# Per-provider circuit breakers
_breakers: Dict[str, CircuitBreaker] = {}


def get_breaker(provider: str) -> CircuitBreaker:
    """Get or create a circuit breaker for the given provider."""
    if provider not in _breakers:
        _breakers[provider] = CircuitBreaker(
            failure_threshold=10,
            cooldown_seconds=15.0,
            name=provider,
        )
    return _breakers[provider]


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open for a provider."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(
            f"Circuit breaker is OPEN for provider '{provider}'. "
            f"Requests will fail-fast until cooldown expires."
        )
