"""Unit tests for the shared resilient source-HTTP plumbing (ticket 08)."""

from __future__ import annotations

import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from beverage_feed import source_http
from beverage_feed.source_http import (
    CircuitBreaker,
    SourceHTTPError,
    backoff_delay,
    failure_metadata,
    is_retryable_failure,
    parse_retry_after,
    response_retry_after,
    spacing_delay,
    status_error,
    transport_error,
)


class SourceHTTPErrorTests(unittest.TestCase):
    """Classification of transport and HTTP-status failures."""

    def test_transport_failure_is_retryable_without_a_status(self):
        error = transport_error("Dunnes", ConnectionError("connection refused"))
        self.assertIsNone(error.status)
        self.assertTrue(error.retryable)
        self.assertIsNone(error.retry_after)
        self.assertIn("Dunnes request failed", str(error))

    def test_429_and_retryable_5xx_statuses_are_flagged_retryable(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(status_error("Tesco", status).retryable)

    def test_permanent_http_statuses_are_not_retryable(self):
        for status in (400, 401, 403, 404, 410, 501):
            with self.subTest(status=status):
                self.assertFalse(status_error("Tesco", status).retryable)

    def test_error_message_carries_no_headers_credentials_or_cookies(self):
        error = status_error("Tesco", 429, "x-apikey: super-secret; Cookie: session=a=b")
        self.assertEqual(str(error), "Tesco HTTP 429")
        self.assertNotIn("secret", str(error))
        self.assertNotIn("Cookie", str(error))
        self.assertNotIn("apikey", str(error))

    def test_is_a_runtime_error_for_existing_handlers(self):
        self.assertIsInstance(status_error("Lidl", 503), RuntimeError)


class ParseRetryAfterTests(unittest.TestCase):
    """Retry-After header parsing: seconds and HTTP-dates."""

    def test_delay_seconds_are_parsed(self):
        self.assertEqual(parse_retry_after("12"), 12.0)

    def test_http_date_in_the_future_yields_a_positive_delay(self):
        soon = datetime.now(timezone.utc) + timedelta(seconds=90)
        delay = parse_retry_after(soon.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.assertIsNotNone(delay)
        assert delay is not None
        self.assertGreater(delay, 60.0)
        self.assertLessEqual(delay, 90.0)

    def test_unparseable_and_missing_values_yield_none(self):
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("not a date"))

    def test_response_retry_after_reads_the_header_when_present(self):
        class Response:
            def __init__(self, headers):
                self.headers = headers

        self.assertEqual(response_retry_after(Response({"Retry-After": "7"})), 7.0)
        self.assertIsNone(response_retry_after(Response({})))
        self.assertIsNone(response_retry_after(object()))


class RetryClassificationTests(unittest.TestCase):
    """Only transport failures, 429s, and retryable 5xx responses retry."""

    def test_transport_and_retryable_status_failures_are_retryable(self):
        self.assertTrue(is_retryable_failure(transport_error("Aldi", OSError("reset"))))
        self.assertTrue(is_retryable_failure(status_error("Aldi", 429)))
        self.assertTrue(is_retryable_failure(status_error("Aldi", 503)))

    def test_permanent_statuses_parse_and_lookup_failures_never_retry(self):
        self.assertFalse(is_retryable_failure(status_error("Aldi", 403)))
        self.assertFalse(is_retryable_failure(ValueError("no items list")))
        self.assertFalse(is_retryable_failure(LookupError("not found")))
        self.assertFalse(is_retryable_failure(KeyError("items")))

    def test_untyped_runtime_error_keeps_the_legacy_transport_treatment(self):
        # Injected adapters (and the lidl.py/aldi.py thin clients) still raise
        # bare RuntimeError for outages; those stay retryable until they adopt
        # SourceHTTPError.
        self.assertTrue(is_retryable_failure(RuntimeError("temporary outage")))


class FailureMetadataTests(unittest.TestCase):
    """Diagnostics preserve status codes and retryability."""

    def test_status_retryability_and_retry_after_are_preserved(self):
        metadata = failure_metadata(status_error("Tesco", 429, "7"))
        self.assertEqual(
            metadata,
            {
                "error_type": "SourceHTTPError",
                "http_status": 429,
                "retryable": True,
                "retry_after_seconds": 7.0,
            },
        )

    def test_transport_failure_metadata_has_no_status(self):
        metadata = failure_metadata(transport_error("Tesco", OSError("timeout")))
        self.assertEqual(
            metadata,
            {"error_type": "SourceHTTPError", "http_status": None, "retryable": True},
        )

    def test_unclassified_failures_carry_only_the_error_type(self):
        self.assertEqual(
            failure_metadata(ValueError("bad price")),
            {"error_type": "ValueError"},
        )


class BackoffDelayTests(unittest.TestCase):
    """Bounded exponential backoff with jitter, honoring Retry-After."""

    def test_delay_grows_exponentially_within_the_jitter_band(self):
        with patch("beverage_feed.source_http.random.uniform", return_value=0.0):
            self.assertEqual(backoff_delay(0.5, 0, None), 0.5)
            self.assertEqual(backoff_delay(0.5, 1, None), 1.0)
            self.assertEqual(backoff_delay(0.5, 2, None), 2.0)

    def test_jitter_is_bounded_to_a_fraction_of_the_exponential(self):
        delay = backoff_delay(0.5, 1, None)
        self.assertGreaterEqual(delay, 1.0)
        self.assertLessEqual(delay, 1.0 * 1.25)

    def test_retry_after_overrides_the_backoff(self):
        self.assertEqual(backoff_delay(0.5, 0, 10.0), 10.0)

    def test_zero_backoff_stays_zero_without_retry_after(self):
        self.assertEqual(backoff_delay(0.0, 3, None), 0.0)


class SpacingDelayTests(unittest.TestCase):
    """Per-retailer request spacing."""

    def test_first_request_has_no_delay(self):
        self.assertEqual(spacing_delay(None, 1.0), 0.0)

    def test_recent_request_waits_out_the_interval(self):
        with patch("beverage_feed.source_http.time.monotonic", return_value=100.0):
            self.assertAlmostEqual(spacing_delay(99.5, 1.0), 0.5)

    def test_elapsed_interval_has_no_delay(self):
        self.assertEqual(spacing_delay(0.0, 1.0), 0.0)


class CircuitBreakerTests(unittest.TestCase):
    """Trip on repeated consecutive failures; half-open after a cooldown."""

    def test_threshold_and_cooldown_are_validated(self):
        with self.assertRaises(ValueError):
            CircuitBreaker(threshold=0)
        with self.assertRaises(ValueError):
            CircuitBreaker(cooldown=-1.0)

    def test_stays_closed_below_the_failure_threshold(self):
        breaker = CircuitBreaker(threshold=3, cooldown=300.0)
        breaker.record_failure()
        breaker.record_failure()
        self.assertFalse(breaker.open)

    def test_opens_after_repeated_consecutive_failures(self):
        breaker = CircuitBreaker(threshold=3, cooldown=300.0)
        for _ in range(3):
            breaker.record_failure()
        self.assertTrue(breaker.open)

    def test_success_resets_the_failure_streak(self):
        breaker = CircuitBreaker(threshold=2, cooldown=300.0)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        self.assertFalse(breaker.open)

    def test_half_opens_once_the_cooldown_elapses(self):
        breaker = CircuitBreaker(threshold=2, cooldown=60.0)
        breaker.record_failure()
        breaker.record_failure()
        self.assertTrue(breaker.open)
        with patch(
            "beverage_feed.source_http.time.monotonic",
            return_value=time_module.monotonic() + 61.0,
        ):
            self.assertFalse(breaker.open)

    def test_a_failed_half_open_trial_re_trips_the_breaker(self):
        breaker = CircuitBreaker(threshold=2, cooldown=60.0)
        breaker.record_failure()
        breaker.record_failure()
        with patch(
            "beverage_feed.source_http.time.monotonic",
            return_value=time_module.monotonic() + 61.0,
        ):
            self.assertFalse(breaker.open)
            breaker.record_failure()
            self.assertTrue(breaker.open)


if __name__ == "__main__":
    unittest.main()
