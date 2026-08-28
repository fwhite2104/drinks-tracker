"""Shared resilient HTTP plumbing for retailer source clients.

Ticket 08: transport failures, HTTP 429 responses, and retryable 5xx
responses are classified once here so collection can retry them with bounded
exponential backoff and jitter, honor ``Retry-After``, space out requests per
retailer, and open a circuit breaker after repeated failures — while operator
diagnostics preserve status codes and retryability and never carry
credentials, cookies, or sensitive headers.

Wired into ``collector.py`` today. The thin clients in ``lidl.py`` and
``aldi.py`` keep their own transports and are documented follow-up adopters
of this module.
"""

from __future__ import annotations

import http.client
import random
import time
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

__all__ = [
    "DEFAULT_CIRCUIT_COOLDOWN",
    "DEFAULT_CIRCUIT_THRESHOLD",
    "RETRYABLE_STATUSES",
    "TRANSPORT_ERRORS",
    "CircuitBreaker",
    "SourceHTTPError",
    "backoff_delay",
    "failure_metadata",
    "is_retryable_failure",
    "parse_retry_after",
    "response_retry_after",
    "spacing_delay",
    "status_error",
    "transport_error",
]

# HTTP statuses worth another attempt: rate limiting (429) and the transient
# server-side failures. 501 is permanent by definition.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Transport-level exception classes that describe an outage rather than a
# verdict about the request. HTTPError subclasses URLError and OSError, so it
# must be caught first by callers.
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    OSError,
    http.client.HTTPException,
)

DEFAULT_CIRCUIT_THRESHOLD = 4
DEFAULT_CIRCUIT_COOLDOWN = 300.0

_JITTER_FRACTION = 0.25


class SourceHTTPError(RuntimeError):
    """A retailer HTTP failure carrying retryability evidence.

    ``status`` is the HTTP status code, or ``None`` for a transport-level
    outage. ``retry_after`` is the parsed ``Retry-After`` delay in seconds
    when the source supplied one. Messages carry no headers, credentials, or
    cookies — diagnostics material is kept in structured attributes instead.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.retryable = (
            (status is None or status in RETRYABLE_STATUSES)
            if retryable is None
            else retryable
        )


def parse_retry_after(value: Any) -> float | None:
    """Parse a ``Retry-After`` value: delay seconds or an HTTP-date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    return max(0.0, seconds)


def response_retry_after(response: Any) -> float | None:
    """Retry-After delay from a response-like object's headers, if any."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return parse_retry_after(headers.get("Retry-After"))


def status_error(
    retailer: str, status: int, retry_after: Any = None
) -> SourceHTTPError:
    """Classified failure for an HTTP status, honoring Retry-After."""
    return SourceHTTPError(
        f"{retailer} HTTP {status}",
        status=status,
        retry_after=parse_retry_after(retry_after),
    )


def transport_error(retailer: str, exc: Exception) -> SourceHTTPError:
    """Retryable failure for a transport-level outage (no HTTP status)."""
    return SourceHTTPError(f"{retailer} request failed: {exc}", status=None)


def is_retryable_failure(exc: BaseException) -> bool:
    """True only for transport failures, 429s, and retryable 5xx responses.

    Parse and lookup failures never retry. An untyped ``RuntimeError`` keeps
    the legacy transport-failure treatment so injected adapters and the thin
    clients in ``lidl.py``/``aldi.py`` stay retryable until they adopt
    ``SourceHTTPError``.
    """
    if isinstance(exc, SourceHTTPError):
        return exc.retryable
    if isinstance(exc, (LookupError, ValueError)):
        return False
    return isinstance(exc, RuntimeError)


def failure_metadata(exc: BaseException) -> dict[str, Any]:
    """Operator-diagnostic metadata preserving status and retryability."""
    metadata: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, SourceHTTPError):
        metadata["http_status"] = exc.status
        metadata["retryable"] = exc.retryable
        if exc.retry_after is not None:
            metadata["retry_after_seconds"] = exc.retry_after
    return metadata


def backoff_delay(base: float, attempt: int, retry_after: float | None) -> float:
    """Bounded exponential backoff with jitter, honoring Retry-After.

    The exponential component doubles per attempt and carries up to 25%
    jitter; a source-supplied ``Retry-After`` delay is honored whenever it
    exceeds that component.
    """
    exponential = base * (2 ** attempt)
    jitter = (
        random.uniform(0.0, exponential * _JITTER_FRACTION)
        if exponential > 0.0
        else 0.0
    )
    return max(exponential + jitter, retry_after or 0.0)


def spacing_delay(
    last_request_at: float | None, min_request_interval: float
) -> float:
    """Seconds to wait before the next request to the same retailer."""
    if last_request_at is None:
        return 0.0
    return max(0.0, min_request_interval - (time.monotonic() - last_request_at))


class CircuitBreaker:
    """Trip after repeated consecutive failures; half-open after a cooldown.

    While open, callers skip the retailer without touching the source. Once
    the cooldown elapses, a trial attempt is allowed: another failure re-trips
    the breaker, a success resets it.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_CIRCUIT_THRESHOLD,
        cooldown: float = DEFAULT_CIRCUIT_COOLDOWN,
    ) -> None:
        if threshold < 1:
            raise ValueError("circuit threshold must be at least 1")
        if cooldown < 0:
            raise ValueError("circuit cooldown must not be negative")
        self.threshold = threshold
        self.cooldown = cooldown
        self._consecutive_failures = 0
        self._last_failure_at: float | None = None

    @property
    def open(self) -> bool:
        if self._consecutive_failures < self.threshold:
            return False
        last = self._last_failure_at
        if last is None:
            return True
        return (time.monotonic() - last) < self.cooldown

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._last_failure_at = time.monotonic()
