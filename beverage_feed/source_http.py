"""Shared resilient HTTP plumbing for retailer source clients.

Ticket 08: transport failures, HTTP 429 responses, and retryable 5xx
responses are classified once here so collection can retry them with bounded
exponential backoff and jitter, honor ``Retry-After``, space out requests per
retailer, and open a circuit breaker after repeated failures — while operator
diagnostics preserve status codes and retryability and never carry
credentials, cookies, or sensitive headers.

Wired into every retailer client: ``RetailerTransport.send`` is the single
throttle + error-classification path for Dunnes, SuperValu, Tesco (urllib
branch), Lidl, and Aldi.
"""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

__all__ = [
    "DEFAULT_CIRCUIT_COOLDOWN",
    "DEFAULT_CIRCUIT_THRESHOLD",
    "RETRYABLE_STATUSES",
    "TRANSPORT_ERRORS",
    "CircuitBreaker",
    "RetailerTransport",
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
    """Retryable failure for a transport-level outage (no HTTP status).

    The exception text may carry credentials/cookies (urllib embeds request
    headers in some errors); messages stay header-free, so the raw text is
    never echoed — the failure metadata keeps status/retryability evidence.
    """
    return SourceHTTPError(f"{retailer} request failed", status=None)


#: How much of a 4xx response body an evidence record keeps.
ERROR_BODY_CHARS = 2000


def _error_record(
    status: int, headers: Any, body: Any, *, parse_json: bool
) -> dict[str, Any]:
    """Evidence record for an HTTP >= 400 response (``capture_errors=True``).

    Keeps the status, the Retry-After hint, and a bounded body sample — the
    body is what explains a rejected GraphQL operation or a WAF block page.
    Scrubbing is the caller's job (the probe runs it through ``safe_record``).
    """
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace")
    else:
        text = str(body)
    record: dict[str, Any] = {
        "error_response": True,
        "status": status,
        "retry_after": _header_value(headers, "Retry-After"),
        "body": text[:ERROR_BODY_CHARS],
    }
    if parse_json:
        try:
            record["json"] = json.loads(text)
        except ValueError:
            record["json"] = None
    return record


def _header_value(headers: Any, name: str) -> Any:
    """Header lookup that tolerates dicts and ``email.message.Message``."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    return value


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


class RetailerTransport:
    """Throttled urllib fetcher shared by the retailer clients.

    One throttle and one error-mapping path: HTTP >= 400 becomes
    ``status_error`` (carrying Retry-After evidence), transport-level outages
    become ``transport_error``, anything else degrades to a plain
    ``RuntimeError``. When ``impersonate`` is set and curl-cffi is installed
    (the optional ``impersonation`` extra), requests go through a
    Chrome-fingerprinted session instead of urllib — the same escape hatch
    ``TescoClient`` uses to clear Akamai; Dunnes now needs it too (CI egress
    gets HTTP 403 on the plain path). The error classification above is
    shared by both branches.
    """

    def __init__(
        self,
        retailer: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
        impersonate: str | None = None,
        session: Any | None = None,
    ) -> None:
        if min_request_interval < 0:
            raise ValueError(f"{retailer} request interval must not be negative")
        self.retailer = retailer
        # opener=None keeps the module-level urllib.request.urlopen as the
        # transport (tests intercept it as the network seam).
        self.opener = opener
        self.min_request_interval = min_request_interval
        # Public so wiring tests and operators can observe the transport
        # mode; an explicitly injected session wins, then an explicit opener
        # (the test seam — no impersonation), then the requested profile.
        self.impersonate = impersonate
        if session is not None:
            self._session: Any = session
        elif opener is not None:
            self._session = None
        else:
            self._session = self._build_session(impersonate)
        self._last_request_at: float | None = None

    def _build_session(self, impersonate: str | None) -> Any | None:
        """A browser-impersonated session, or ``None`` without curl-cffi.

        The impersonation extra is optional: when it is not installed the
        transport silently keeps the urllib path, exactly like
        ``TescoClient``.
        """
        if impersonate is None:
            return None
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            return None
        return curl_requests.Session(impersonate=impersonate)

    def _throttle(self) -> None:
        delay = spacing_delay(self._last_request_at, self.min_request_interval)
        if delay:
            time.sleep(delay)

    def _session_response(
        self, request: urllib.request.Request
    ) -> tuple[int, Mapping[str, Any], bytes]:
        """Send one prepared Request through the impersonated session.

        The tracker's own ``User-Agent`` is dropped: the impersonated profile
        supplies the matching browser UA and client hints (a Chrome TLS
        fingerprint paired with a non-browser UA is itself a detection
        signal). curl_cffi raises ``RequestException``, an ``OSError``
        subclass, so transport failures keep the shared classification.
        """
        headers = {
            key: value
            for key, value in request.header_items()
            if key.lower() != "user-agent"
        }
        response = self._session.request(
            request.get_method(),
            request.full_url,
            headers=headers,
            data=request.data,
            timeout=30,
        )
        return (
            int(getattr(response, "status_code", 200)),
            getattr(response, "headers", None) or {},
            bytes(getattr(response, "content", b"") or b""),
        )

    def send(
        self,
        request: urllib.request.Request,
        *,
        parse_json: bool = True,
        capture_errors: bool = False,
    ) -> Any:
        """Fetch a prepared urllib Request with shared throttle + error mapping.

        HTTP >= 400 becomes ``status_error`` (carrying Retry-After evidence),
        transport-level outages become ``transport_error``, anything else
        degrades to a plain ``RuntimeError``.

        ``capture_errors=True`` returns the failure as an evidence record
        (``{"error_response": True, "status", "retry_after", "body"}``) instead
        of raising. Operator/reconnaissance tooling needs the body a retailer
        sends with a 4xx — a GraphQL gateway, for example, answers a rejected
        operation with an explanatory error document.
        """
        self._throttle()
        try:
            if self._session is not None:
                status, headers, body = self._session_response(request)
                if status >= 400:
                    if capture_errors:
                        return _error_record(status, headers, body, parse_json=parse_json)
                    raise status_error(
                        self.retailer, status, headers.get("Retry-After")
                    )
            else:
                context = (
                    self.opener.open(request, timeout=30)
                    if self.opener is not None
                    else urllib.request.urlopen(request, timeout=30)
                )
                with context as response:
                    if getattr(response, "status", 200) >= 400:
                        if capture_errors:
                            return _error_record(
                                getattr(response, "status", 200),
                                response.headers,
                                response.read(),
                                parse_json=parse_json,
                            )
                        raise status_error(
                            self.retailer, getattr(response, "status", 200),
                            response_retry_after(response),
                        )
                    body = response.read()
        except SourceHTTPError:
            raise
        except urllib.error.HTTPError as exc:
            if capture_errors:
                return _error_record(
                    exc.code, exc.headers, exc.read(), parse_json=parse_json
                )
            raise status_error(
                self.retailer, exc.code, exc.headers.get("Retry-After")
            ) from exc
        except TRANSPORT_ERRORS as exc:
            raise transport_error(self.retailer, exc) from exc
        except Exception as exc:
            raise RuntimeError(f"{self.retailer} request failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()
        if not parse_json:
            return body
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"{self.retailer} response was not valid JSON: {exc}"
            ) from exc

    def text(
        self,
        url: str,
        *,
        accept: str = "application/json",
        headers: Mapping[str, str] | None = None,
    ) -> str:
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": "drinks-tracker/0.1", **(headers or {})},
        )
        body = self.send(request, parse_json=False)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{self.retailer} response was not UTF-8: {exc}") from exc

    def json(
        self,
        url: str,
        *,
        accept: str = "application/json",
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            return json.loads(self.text(url, accept=accept, headers=headers))
        except ValueError as exc:
            raise RuntimeError(f"{self.retailer} response was not valid JSON: {exc}") from exc


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
