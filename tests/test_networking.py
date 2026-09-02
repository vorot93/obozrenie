import codecs
import contextlib
import gc
import io
import json
import socket
import threading
import time
import traceback
import unittest
import weakref
from types import SimpleNamespace
from unittest import mock

import requests
from icmplib import Host
from icmplib.exceptions import ICMPLibError

from obozrenie import http_client, network_calls, ping
from obozrenie.diagnostics import SafeDiagnostic
from obozrenie.models import Endpoint, Player, Server


class _StartupAbort(BaseException):
    pass


class _WeakJsonValue:
    __slots__ = ("__weakref__",)


class _WeakJsonError(Exception):
    pass


class _FloatDeadline(float):
    pass


class _ExplodingIterable:
    """Records whether it was iterated, then raises if it ever is."""

    def __init__(self):
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        raise AssertionError("iterable was consumed before deadline validation")


class _MapErrorPool:
    def __init__(self, error):
        self.error = error
        self.ensure_calls = 0
        self.map_calls = []

    def ensure_started(self):
        self.ensure_calls += 1

    def map_until(self, deadline, function, hosts, /):
        self.map_calls.append((deadline, function, tuple(hosts)))
        raise self.error


@contextlib.contextmanager
def installed_network_pools():
    fresh = network_calls.NetworkCallPools(
        dns=network_calls.BoundedDaemonPool(4, "obozrenie-dns-resolver"),
        http=network_calls.BoundedDaemonPool(4, "obozrenie-http"),
        icmp=network_calls.BoundedDaemonPool(32, "obozrenie-icmp"),
    )
    patcher = mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", fresh)
    patcher.start()
    try:
        yield fresh
    finally:
        try:
            network_calls.close_network_call_pools(timeout=0.2)
        finally:
            patcher.stop()


def _start_call(callback, name="test-http-caller"):
    outcomes = []
    finished = threading.Event()

    def run():
        try:
            outcomes.append((True, callback()))
        except BaseException as error:
            outcomes.append((False, error))
        finally:
            finished.set()

    caller = threading.Thread(target=run, name=name)
    caller.start()
    return caller, finished, outcomes


def _capture_error(callback):
    try:
        callback()
    except BaseException as error:
        return error
    raise AssertionError("expected callback to raise")


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


class _ResponseBody(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.was_read = False

    def read(self, size=-1):
        self.was_read = True
        return super().read(size)


class _HttpResponse(requests.Response):
    def __init__(self):
        super().__init__()
        self.was_entered = False
        self.was_closed = False

    def __enter__(self):
        self.was_entered = True
        return super().__enter__()

    def close(self):
        self.was_closed = True
        super().close()


def _http_response(
    body,
    *,
    status_code=200,
    encoding=None,
    headers=None,
    url="https://example.invalid/servers",
):
    response = _HttpResponse()
    response.status_code = status_code
    response.reason = "OK" if status_code == 200 else "Service Unavailable"
    response.url = url
    response.headers["Content-Type"] = "application/json"
    if encoding is not None:
        response.headers["Content-Type"] += f"; charset={encoding}"
    if headers is not None:
        response.headers.update(headers)
    response.encoding = encoding
    response.raw = _ResponseBody(body)
    return response


class _FakeSession:
    def __init__(self, outcomes=(), *, request=None):
        self.outcomes = list(outcomes)
        self.request = request
        self.calls = []
        self.was_entered = False
        self.was_closed = False

    def __enter__(self):
        self.was_entered = True
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, dict(kwargs)))
        if self.request is not None:
            return self.request(url, **kwargs)
        if not self.outcomes:
            raise AssertionError("fake HTTP session exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.was_closed = True


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.pools = self.enterContext(installed_network_pools())
        self.direct_request = self.enterContext(
            mock.patch(
                "obozrenie.http_client.requests.get",
                side_effect=AssertionError(
                    "HTTP client bypassed its owned requests.Session"
                ),
            )
        )
        self.sessions = []
        # Tests that cannot predict how many sessions production opens set
        # this handler instead of supplying explicit sessions.
        self.session_request = None

        def make_session():
            session = _FakeSession(request=self.session_request)
            self.sessions.append(session)
            return session

        self.session_constructor = self.enterContext(
            mock.patch(
                "obozrenie.http_client.requests.Session",
                side_effect=make_session,
            )
        )

    def _set_sessions(self, *sessions):
        self.session_constructor.side_effect = list(sessions)

    def _requested_urls(self):
        return [
            url
            for session in self.sessions
            for url, _kwargs in session.calls
        ]

    def _call_with_deadline_clock(
        self,
        response,
        clock_offsets,
        **options,
    ):
        deadline = time.monotonic() + 1
        session = _FakeSession((response,))
        self._set_sessions(session)
        monotonic = mock.Mock(
            side_effect=[deadline + offset for offset in clock_offsets]
        )
        with mock.patch.object(
            http_client,
            "time",
            SimpleNamespace(monotonic=monotonic),
        ):
            error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/servers",
                    deadline=deadline,
                    **options,
                )
            )
        return error, session, monotonic

    def test_https_redirect_chain_resolves_relative_locations_without_automatic_redirects(
        self,
    ):
        first_redirect = _http_response(
            b'{"redirect_secret": "first"}',
            status_code=302,
            headers={"Location": "../next"},
        )
        second_redirect = _http_response(
            b'{"redirect_secret": "second"}',
            status_code=307,
            headers={"Location": "final"},
        )
        final_response = _http_response(b'{"servers": []}')
        session = _FakeSession(
            (first_redirect, second_redirect, final_response)
        )
        self._set_sessions(session)
        real_call_until = self.pools.http.call_until

        with mock.patch.object(
            self.pools.http,
            "call_until",
            wraps=real_call_until,
        ) as call_until:
            result = http_client.get_json(
                "https://example.invalid/api/v1/servers"
            )

        self.assertEqual(result, {"servers": []})
        self.assertEqual(call_until.call_count, 1)
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/api/v1/servers",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                ),
                (
                    "https://example.invalid/api/next",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                ),
                (
                    "https://example.invalid/api/final",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                ),
            ],
        )
        self.assertTrue(session.was_entered)
        self.assertTrue(session.was_closed)
        self.assertTrue(first_redirect.was_closed)
        self.assertTrue(second_redirect.was_closed)
        self.assertFalse(first_redirect.raw.was_read)
        self.assertFalse(second_redirect.raw.was_read)
        self.assertTrue(final_response.raw.was_read)
        self.assertTrue(final_response.was_closed)
        self.direct_request.assert_not_called()

    def test_https_redirect_rejects_http_target_before_plaintext_request(self):
        redirect = _http_response(
            b'{"token": "redirect-body-secret"}',
            status_code=302,
            headers={
                "Location": "http://plaintext.invalid/private?token=secret"
            },
        )
        session = _FakeSession((redirect,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json(
                "https://example.invalid/secure-start"
            )
        )

        self.assertIs(type(error), http_client.HttpsPolicyError)
        self.assertEqual(
            str(error.diagnostic),
            "HTTPS URL policy rejected the request.",
        )
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/secure-start",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                )
            ],
        )
        self.assertTrue(
            all("http://" not in requested_url for requested_url, _ in session.calls)
        )
        self.assertTrue(redirect.was_closed)
        self.assertFalse(redirect.raw.was_read)
        self.assertTrue(session.was_closed)
        self.direct_request.assert_not_called()

    def test_redirect_limit_allows_five_and_rejects_sixth_before_seventh_request(
        self,
    ):
        redirects = [
            _http_response(
                f'{{"redirect": {index}}}'.encode(),
                status_code=302,
                headers={"Location": f"/hop-{index}"},
            )
            for index in range(1, 7)
        ]
        unrequested = _http_response(b'{"must_not": "be requested"}')
        session = _FakeSession((*redirects, unrequested))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/start")
        )

        self.assertIs(type(error), http_client.HttpsPolicyError)
        self.assertEqual(
            str(error.diagnostic),
            "HTTPS redirect policy rejected the response.",
        )
        self.assertEqual(
            [url for url, _kwargs in session.calls],
            [
                "https://example.invalid/start",
                "https://example.invalid/hop-1",
                "https://example.invalid/hop-2",
                "https://example.invalid/hop-3",
                "https://example.invalid/hop-4",
                "https://example.invalid/hop-5",
            ],
        )
        self.assertTrue(
            all(
                kwargs
                == {
                    "allow_redirects": False,
                    "stream": True,
                    "timeout": (5, 15),
                }
                for _url, kwargs in session.calls
            )
        )
        self.assertEqual(session.outcomes, [unrequested])
        self.assertTrue(all(response.was_closed for response in redirects))
        self.assertTrue(
            all(not response.raw.was_read for response in redirects)
        )
        self.assertFalse(unrequested.was_closed)
        self.assertFalse(unrequested.raw.was_read)
        self.direct_request.assert_not_called()

    def test_redirect_without_location_is_payload_free_policy_failure(self):
        cases = (
            (302, {}, "missing Location"),
            (304, {"Location": "/unsupported"}, "unsupported 3xx"),
        )
        for status_code, headers, label in cases:
            with self.subTest(case=label):
                response = _http_response(
                    b'{"secret": "response-payload"}',
                    status_code=status_code,
                    headers=headers,
                    url="https://user:password@example.invalid/private?token=secret",
                )
                session = _FakeSession((response,))
                self._set_sessions(session)

                error = _capture_error(
                    lambda: http_client.get_json(
                        "https://example.invalid/redirect"
                    )
                )

                self.assertIs(type(error), http_client.HttpsPolicyError)
                self.assertEqual(
                    str(error.diagnostic),
                    "HTTPS redirect policy rejected the response.",
                )
                self.assertNotIn("response-payload", str(error))
                self.assertNotIn("password", str(error))
                self.assertNotIn("token", str(error))
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(response.was_closed)
                self.assertFalse(response.raw.was_read)
                self.assertTrue(session.was_closed)
        self.direct_request.assert_not_called()

    def test_invalid_initial_url_fails_before_pool_or_session(self):
        invalid_urls = (
            "http://example.invalid/private?token=downgrade-secret",
            "https:///missing-host/path-secret",
            "https://example.invalid:not-a-port/private",
            "https://example.invalid\\@evil.invalid/private?token=secret",
            "\x00https://example.invalid/private?token=secret",
            " https://example.invalid/private?token=secret",
            "https://exa\tmple.invalid/private?token=secret",
            "https://example.invalid/pri\nvate?token=secret",
            "https://example.invalid/pri\rvate?token=secret",
        )
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError("invalid URL resolved the HTTP pool"),
        ) as get_pools:
            for invalid_url in invalid_urls:
                with self.subTest(url=invalid_url):
                    error = _capture_error(
                        lambda invalid_url=invalid_url: http_client.get_json(
                            invalid_url
                        )
                    )
                    self.assertIs(type(error), http_client.HttpsPolicyError)
                    self.assertEqual(
                        str(error.diagnostic),
                        "HTTPS URL policy rejected the request.",
                    )
                    self.assertNotIn(invalid_url, str(error))

        get_pools.assert_not_called()
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertEqual(self.pools.http._workers, [])

    def test_url_policy_fails_closed_for_values_that_are_not_exact_text(
        self,
    ):
        class SubclassedUrl(str):
            pass

        for value in (
            None,
            123,
            b"https://example.invalid/servers",
            SubclassedUrl("https://example.invalid/servers"),
        ):
            with self.subTest(value=type(value).__name__):
                error = _capture_error(
                    lambda value=value: http_client.validate_https_url(
                        value
                    )
                )

                self.assertIs(type(error), http_client.HttpsPolicyError)
                self.assertEqual(
                    str(error.diagnostic),
                    "HTTPS URL policy rejected the request.",
                )

    def test_open_https_response_rejects_invalid_initial_url_before_session_or_pool(
        self,
    ):
        invalid_url = (
            "http://user:password@plaintext.invalid/private?token=secret"
        )
        deadline = time.monotonic() + 1
        self.session_constructor.side_effect = AssertionError(
            "invalid direct response URL constructed a Session"
        )

        def enter_response_context():
            with http_client.open_https_response(
                invalid_url,
                timeout=(2, 9),
                deadline=deadline,
            ):
                raise AssertionError("invalid URL yielded an HTTP response")

        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError(
                "direct response URL resolved the HTTP pool"
            ),
        ) as get_pools:
            error = _capture_error(enter_response_context)

        self.assertIs(type(error), http_client.HttpsPolicyError)
        self.assertEqual(
            str(error.diagnostic),
            "HTTPS URL policy rejected the request.",
        )
        self.assertNotIn(invalid_url, str(error))
        get_pools.assert_not_called()
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertEqual(self.pools.http._workers, [])

    def test_every_hop_timeout_is_clamped_to_the_remaining_deadline(self):
        redirect = _http_response(
            b'{"redirect_secret": "first hop"}',
            status_code=302,
            headers={"Location": "/final"},
        )
        final_response = _http_response(b"null")
        session = _FakeSession((redirect, final_response))
        self._set_sessions(session)
        # An integer-valued deadline keeps every clamped timeout exact.
        deadline = float(int(time.monotonic()) + 30)
        # One reading per deadline check: the request gate, both hops, the
        # response context, the single chunk, the decode, and the parse.
        monotonic = mock.Mock(
            side_effect=[
                deadline + offset
                for offset in (-30.0, -8.0, -3.0, -3.0, -3.0, -3.0, -3.0)
            ]
        )

        with mock.patch.object(
            http_client,
            "time",
            SimpleNamespace(monotonic=monotonic),
        ):
            result = http_client.get_json(
                "https://example.invalid/servers",
                timeout=(5, 15),
                deadline=deadline,
            )

        self.assertIsNone(result)
        self.assertEqual(monotonic.call_count, 7)
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/servers",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 8.0),
                    },
                ),
                (
                    "https://example.invalid/final",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (3.0, 3.0),
                    },
                ),
            ],
        )
        self.direct_request.assert_not_called()

    def test_scalar_and_absent_hop_timeouts_use_the_remaining_deadline(
        self,
    ):
        cases = ((None, 6.0), (2.5, 2.5), (9.0, 6.0))
        for supplied_timeout, expected_timeout in cases:
            with self.subTest(timeout=supplied_timeout):
                session = _FakeSession((_http_response(b"null"),))
                self._set_sessions(session)
                deadline = float(int(time.monotonic()) + 30)
                monotonic = mock.Mock(
                    side_effect=[
                        deadline + offset
                        for offset in (-30.0, -6.0, -6.0, -6.0, -6.0, -6.0)
                    ]
                )

                with mock.patch.object(
                    http_client,
                    "time",
                    SimpleNamespace(monotonic=monotonic),
                ):
                    result = http_client.get_json(
                        "https://example.invalid/servers",
                        timeout=supplied_timeout,
                        deadline=deadline,
                    )

                self.assertIsNone(result)
                self.assertEqual(
                    session.calls,
                    [
                        (
                            "https://example.invalid/servers",
                            {
                                "allow_redirects": False,
                                "stream": True,
                                "timeout": expected_timeout,
                            },
                        )
                    ],
                )

    def test_charset_naming_no_codec_is_one_isolated_decode_failure(self):
        undecodable_body = b'{"secret": "do not report"}'
        cases = ("unknown-secret-codec", "utf\x00-8")
        for encoding in cases:
            with self.subTest(encoding=encoding):
                response = _http_response(
                    undecodable_body,
                    encoding=encoding,
                )
                session = _FakeSession((response,))
                self._set_sessions(session)

                error = _capture_error(
                    lambda: http_client.get_json(
                        "https://example.invalid/servers"
                    )
                )

                self.assertIs(type(error), http_client.UnknownEncodingError)
                self.assertEqual(
                    str(error),
                    "HTTP response uses an unknown character encoding",
                )
                self.assertIsNone(error.__cause__)
                self.assertTrue(error.__suppress_context__)
                self.assertIs(
                    http_client.describe_http_failure(error).kind,
                    http_client.HttpFailureKind.DECODE,
                )
                self.assertTrue(response.was_closed)

    def test_non_text_codec_charset_is_one_isolated_decode_failure(self):
        # A binary transform such as base64 resolves through codecs.lookup and
        # only bytes.decode rejects it, so the guard has to cover both steps.
        response = _http_response(b'{"secret": "do not report"}', encoding="base64")
        session = _FakeSession((response,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assertIs(type(error), http_client.UnknownEncodingError)
        self.assertEqual(
            str(error),
            "HTTP response uses an unknown character encoding",
        )
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertIs(
            http_client.describe_http_failure(error).kind,
            http_client.HttpFailureKind.DECODE,
        )
        self.assertTrue(response.was_closed)

    def assert_isolated_decode_failure(self, error, response, *, payload=None):
        """Assert one decode-phase failure is classified and payload-free."""
        self.assertIs(type(error), http_client.ResponseDecodeError)
        self.assertEqual(str(error), "HTTP response decoding failed.")
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertIs(
            http_client.describe_http_failure(error).kind,
            http_client.HttpFailureKind.DECODE,
        )
        if payload is not None:
            # Suppressed context never renders, so the body that provoked the
            # failure cannot reach a log or a traceback through it either.
            rendered = "".join(
                traceback.format_exception(
                    type(error),
                    error,
                    error.__traceback__,
                )
            )
            self.assertNotIn(payload, rendered)
        self.assertTrue(response.was_closed)

    def test_undecodable_body_is_one_isolated_decode_failure(self):
        response = _http_response(b'"\xff"', encoding="utf-8")
        session = _FakeSession((response,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assert_isolated_decode_failure(error, response)

    def test_bare_unicode_error_codec_is_one_isolated_decode_failure(self):
        # `idna` and `punycode` raise the bare base class before CPython
        # 3.14, which no `UnicodeDecodeError` clause would classify. A codec
        # registered for this test reproduces that on every interpreter.
        encoding = "obozrenie-test-bare-unicode-error"

        def decode(_data, _errors="strict"):
            raise UnicodeError("codec-message-secret")

        def search(name):
            if name != encoding.replace("-", "_"):
                return None
            return codecs.CodecInfo(
                name=name,
                encode=lambda text, errors="strict": (b"", 0),
                decode=decode,
            )

        codecs.register(search)
        self.addCleanup(codecs.unregister, search)
        response = _http_response(
            b'{"secret": "do not report"}',
            encoding=encoding,
        )
        session = _FakeSession((response,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assert_isolated_decode_failure(
            error,
            response,
            payload="codec-message-secret",
        )

    def test_digit_limited_integer_body_is_one_isolated_decode_failure(self):
        # 5 KB is far under every size limit, but a decimal integer this long
        # exceeds CPython's integer string-digit limit, which `json.loads`
        # reports as a bare `ValueError` rather than a `JSONDecodeError`.
        digits = "9" * 5000
        response = _http_response(f"[{digits}]".encode("utf-8"))
        session = _FakeSession((response,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assert_isolated_decode_failure(error, response, payload=digits)

    def test_deeply_nested_body_is_one_isolated_decode_failure(self):
        response = _http_response(b"[" * 200000)
        session = _FakeSession((response,))
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assert_isolated_decode_failure(error, response)

    def test_open_https_response_reports_an_expired_deadline_as_a_timeout(
        self,
    ):
        session = _FakeSession()
        self._set_sessions(session)

        def enter_response_context():
            with http_client.open_https_response(
                "https://example.invalid/servers",
                timeout=(5, 15),
                deadline=time.monotonic() - 1,
            ):
                raise AssertionError("expired deadline yielded a response")

        error = _capture_error(enter_response_context)

        self.assertIs(type(error), requests.Timeout)
        self.assertEqual(str(error), "HTTP operation deadline exceeded.")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(session.calls, [])
        self.assertTrue(session.was_closed)
        self.direct_request.assert_not_called()

    def test_redirect_responses_close_unread_and_final_body_uses_original_limit(
        self,
    ):
        first_redirect = _http_response(
            b'"first redirect payload must stay unread"',
            status_code=301,
            headers={"Location": "/second"},
        )
        second_redirect = _http_response(
            b'"second redirect payload must stay unread"',
            status_code=308,
            headers={"Location": "/final"},
        )
        final_response = _http_response(b"null ")
        session = _FakeSession(
            (first_redirect, second_redirect, final_response)
        )
        self._set_sessions(session)

        error = _capture_error(
            lambda: http_client.get_json(
                "https://example.invalid/first",
                max_bytes=4,
            )
        )

        self.assertIs(type(error), http_client.ResponseTooLargeError)
        self.assertEqual(str(error), "HTTP response exceeds 4 bytes")
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(first_redirect.was_closed)
        self.assertTrue(second_redirect.was_closed)
        self.assertFalse(first_redirect.raw.was_read)
        self.assertFalse(second_redirect.raw.was_read)
        self.assertTrue(final_response.raw.was_read)
        self.assertTrue(final_response.was_closed)
        self.assertTrue(session.was_closed)
        self.direct_request.assert_not_called()

    def test_tls_failure_is_distinct_from_connection_failure(self):
        class TlsTimeoutError(
            requests.exceptions.SSLError,
            requests.Timeout,
        ):
            pass

        tls_error = requests.exceptions.SSLError(
            "tls-certificate-secret.example.invalid"
        )
        connection_error = requests.ConnectionError(
            "connection-secret.example.invalid"
        )
        tls_session = _FakeSession((tls_error,))
        connection_session = _FakeSession((connection_error,))
        self._set_sessions(tls_session, connection_session)

        raised_tls = _capture_error(
            lambda: http_client.get_json("https://example.invalid/tls")
        )
        raised_connection = _capture_error(
            lambda: http_client.get_json("https://example.invalid/connection")
        )

        self.assertIs(raised_tls, tls_error)
        self.assertIs(raised_connection, connection_error)
        self.assertIs(
            http_client.describe_http_failure(raised_tls).kind,
            http_client.HttpFailureKind.TLS,
        )
        self.assertIs(
            http_client.describe_http_failure(raised_connection).kind,
            http_client.HttpFailureKind.CONNECTION,
        )
        self.assertIs(
            http_client.describe_http_failure(
                TlsTimeoutError("tls-timeout-secret.example.invalid")
            ).kind,
            http_client.HttpFailureKind.TLS,
        )
        self.assertTrue(tls_session.was_closed)
        self.assertTrue(connection_session.was_closed)
        self.direct_request.assert_not_called()

    def test_tls_failure_description_is_payload_free_without_inspection(self):
        secret = "hostile-tls-secret-token"

        class HostileTlsError(requests.exceptions.SSLError):
            def __init__(self):
                self.payload = secret
                self.request = SimpleNamespace(
                    url=f"https://user:{secret}@example.invalid/private"
                )
                self.response = None

            @property
            def args(self):
                raise AssertionError("TLS exception args inspected")

            def __str__(self):
                raise AssertionError("TLS exception stringified")

            def __repr__(self):
                raise AssertionError("TLS exception represented")

        failure = http_client.describe_http_failure(HostileTlsError())

        self.assertIsNotNone(failure)
        self.assertIs(failure.kind, http_client.HttpFailureKind.TLS)
        self.assertIsNone(failure.status_code)
        self.assertIsInstance(failure.diagnostic, SafeDiagnostic)
        self.assertEqual(
            str(failure.diagnostic),
            "HTTPS TLS connection failed.",
        )
        self.assertNotIn(secret, str(failure.diagnostic))

    def test_http_failure_description_is_payload_free_and_identity_preserving(
        self,
    ):
        secret = "classification-secret-token"

        class HostileRequestError(requests.RequestException):
            def __init__(self):
                self.request = SimpleNamespace(
                    url=f"https://user:{secret}@example.invalid/private"
                )
                self.response = None

            @property
            def args(self):
                raise AssertionError("request exception args inspected")

            def __str__(self):
                raise AssertionError("request exception stringified")

            def __repr__(self):
                raise AssertionError("request exception represented")

        class HostileHttpError(requests.HTTPError):
            def __init__(self, response):
                self.request = SimpleNamespace(
                    url=f"https://user:{secret}@example.invalid/private"
                )
                self.response = response

            @property
            def args(self):
                raise AssertionError("HTTP error args inspected")

            def __str__(self):
                raise AssertionError("HTTP error stringified")

            def __repr__(self):
                raise AssertionError("HTTP error represented")

        status_response = _http_response(
            b"",
            status_code=503,
            url=f"https://user:{secret}@example.invalid/private?token={secret}",
        )
        status_error = HostileHttpError(status_response)
        policy_diagnostic = SafeDiagnostic("Declared redirect diagnostic.")
        policy_error = http_client.HttpsPolicyError(policy_diagnostic)
        size_diagnostic = SafeDiagnostic("Declared size diagnostic.")
        size_error = http_client.ResponseTooLargeError(size_diagnostic)
        encoding_diagnostic = SafeDiagnostic("Declared encoding diagnostic.")
        encoding_error = http_client.UnknownEncodingError(encoding_diagnostic)
        cases = (
            (
                requests.Timeout(secret),
                http_client.HttpFailureKind.TIMEOUT,
                None,
            ),
            (
                requests.ConnectionError(secret),
                http_client.HttpFailureKind.CONNECTION,
                None,
            ),
            (
                requests.exceptions.ChunkedEncodingError(secret),
                http_client.HttpFailureKind.RESPONSE_INTEGRITY,
                None,
            ),
            (
                requests.exceptions.ContentDecodingError(secret),
                http_client.HttpFailureKind.RESPONSE_INTEGRITY,
                None,
            ),
            (
                UnicodeDecodeError("utf-8", b"\xff", 0, 1, secret),
                http_client.HttpFailureKind.DECODE,
                None,
            ),
            (
                json.JSONDecodeError(secret, f'{{"token": "{secret}"}}', 1),
                http_client.HttpFailureKind.DECODE,
                None,
            ),
            (
                requests.exceptions.InvalidURL(secret),
                http_client.HttpFailureKind.INVALID_REQUEST,
                None,
            ),
            (
                requests.exceptions.InvalidSchema(secret),
                http_client.HttpFailureKind.INVALID_REQUEST,
                None,
            ),
            (
                requests.exceptions.MissingSchema(secret),
                http_client.HttpFailureKind.INVALID_REQUEST,
                None,
            ),
            (
                requests.exceptions.InvalidHeader(secret),
                http_client.HttpFailureKind.INVALID_REQUEST,
                None,
            ),
            (
                policy_error,
                http_client.HttpFailureKind.REDIRECT_POLICY,
                None,
            ),
            (status_error, http_client.HttpFailureKind.STATUS, 503),
            (
                size_error,
                http_client.HttpFailureKind.RESPONSE_TOO_LARGE,
                None,
            ),
            (
                encoding_error,
                http_client.HttpFailureKind.DECODE,
                None,
            ),
            (
                HostileRequestError(),
                http_client.HttpFailureKind.REQUEST,
                None,
            ),
        )
        for error, expected_kind, expected_status in cases:
            with self.subTest(kind=expected_kind):
                failure = http_client.describe_http_failure(error)
                self.assertIsNotNone(failure)
                self.assertIs(failure.kind, expected_kind)
                self.assertEqual(failure.status_code, expected_status)
                self.assertIsInstance(failure.diagnostic, SafeDiagnostic)
                self.assertNotIn(secret, str(failure.diagnostic))

        self.assertIs(
            http_client.describe_http_failure(policy_error).diagnostic,
            policy_diagnostic,
        )
        self.assertIs(
            http_client.describe_http_failure(size_error).diagnostic,
            size_diagnostic,
        )
        self.assertIs(
            http_client.describe_http_failure(encoding_error).diagnostic,
            encoding_diagnostic,
        )

        class HostileUnknownError(Exception):
            def __str__(self):
                raise AssertionError("unknown exception stringified")

            def __repr__(self):
                raise AssertionError("unknown exception represented")

        self.assertIsNone(
            http_client.describe_http_failure(HostileUnknownError(secret))
        )

    def test_pool_infrastructure_errors_are_never_http_failures(self):
        class InfrastructureTimeout(
            network_calls.CallDeadlineExceeded,
            requests.Timeout,
        ):
            pass

        class InfrastructureConnectionError(
            requests.ConnectionError,
            network_calls.PoolUnavailableError,
        ):
            pass

        class InfrastructureSaturation(
            network_calls.PoolSaturatedError,
            requests.exceptions.SSLError,
        ):
            pass

        for error in (
            network_calls.CallDeadlineExceeded("pool deadline"),
            network_calls.PoolSaturatedError("pool saturated"),
            network_calls.PoolUnavailableError("pool unavailable"),
            InfrastructureTimeout("derived pool deadline"),
            InfrastructureConnectionError("derived pool unavailability"),
            InfrastructureSaturation("derived pool saturation"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(http_client.describe_http_failure(error))

    def test_complete_requests_lifecycle_runs_in_dynamic_http_pool(self):
        stages = []
        recording = False

        class LifecycleResponse(_HttpResponse):
            def __init__(self):
                self._encoding_value = None
                super().__init__()
                self.status_code = 200
                self.reason = "OK"
                self.url = "https://example.invalid/servers"
                self.raw = _ResponseBody(b"")

            def record(self, stage):
                if recording:
                    stages.append((stage, threading.current_thread()))

            def __enter__(self):
                self.record("response context")
                return super().__enter__()

            def raise_for_status(self):
                self.record("status")
                return super().raise_for_status()

            def iter_content(self, chunk_size=1, decode_unicode=False):
                self.record("bounded stream")
                self.assert_chunk_size = chunk_size
                self.assert_decode_unicode = decode_unicode
                yield b'{"servers": []}'

            @property
            def encoding(self):
                self.record("encoding")
                return self._encoding_value

            @encoding.setter
            def encoding(self, value):
                self._encoding_value = value

            def close(self):
                self.record("response closure")
                super().close()

        response = LifecycleResponse()
        recording = True
        real_json_loads = json.loads

        def request_json(*args, **kwargs):
            stages.append(("request and redirects", threading.current_thread()))
            return response

        def parse_json(text):
            stages.append(("JSON", threading.current_thread()))
            return real_json_loads(text)

        caller_thread = threading.current_thread()
        session = _FakeSession(request=request_json)
        self._set_sessions(session)
        with mock.patch.object(
            http_client.json,
            "loads",
            side_effect=parse_json,
        ):
            result = http_client.get_json(
                "https://example.invalid/servers"
            )

        self.assertEqual(result, {"servers": []})
        self.assertEqual(
            [stage for stage, _thread in stages],
            [
                "request and redirects",
                "response context",
                "status",
                "bounded stream",
                "encoding",
                "JSON",
                "response closure",
            ],
        )
        lifecycle_threads = {thread for _stage, thread in stages}
        self.assertEqual(len(lifecycle_threads), 1)
        worker = lifecycle_threads.pop()
        self.assertIsNot(worker, caller_thread)
        self.assertTrue(worker.daemon)
        self.assertEqual(worker.name, "obozrenie-http")
        self.assertEqual(response.assert_chunk_size, 64 * 1024)
        self.assertIs(response.assert_decode_unicode, False)
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/servers",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                )
            ],
        )
        self.direct_request.assert_not_called()

    def test_default_deadline_uses_http_operation_timeout_once(self):
        exact_result = object()

        class RecordingPool:
            def __init__(self):
                self.ensure_calls = 0
                self.calls = []

            def ensure_started(self):
                self.ensure_calls += 1

            def call_until(self, deadline, function, /, *args, **kwargs):
                self.calls.append((deadline, function, args, kwargs))
                return exact_result

        pool = RecordingPool()
        pools = network_calls.NetworkCallPools(
            dns=mock.Mock(),
            http=pool,
            icmp=mock.Mock(),
        )
        monotonic = mock.Mock(return_value=100.0)
        fake_time = SimpleNamespace(monotonic=monotonic)
        with (
            mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools),
            mock.patch.object(http_client, "time", fake_time, create=True),
            mock.patch.object(
                http_client,
                "_HTTP_OPERATION_TIMEOUT_SECONDS",
                7.5,
                create=True,
            ),
        ):
            result = http_client.get_json(
                "https://example.invalid/default-deadline"
            )

        self.assertIs(result, exact_result)
        self.assertEqual(pool.ensure_calls, 1)
        self.assertEqual(len(pool.calls), 1)
        deadline, _function, args, kwargs = pool.calls[0]
        self.assertEqual(deadline, 107.5)
        self.assertEqual(
            args,
            (
                "https://example.invalid/default-deadline",
                (5, 15),
                16 * 1024 * 1024,
                107.5,
            ),
        )
        self.assertEqual(kwargs, {})

    def test_supplied_absolute_deadline_is_passed_unchanged(self):
        exact_result = object()
        supplied_deadline = time.monotonic() + 10
        calls = []

        def return_exact(deadline, function, /, *args, **kwargs):
            calls.append((deadline, function, args, kwargs))
            return exact_result

        with mock.patch.object(
            self.pools.http,
            "call_until",
            side_effect=return_exact,
        ):
            error = None
            try:
                result = http_client.get_json(
                    "https://example.invalid/supplied-deadline",
                    timeout=(2, 9),
                    max_bytes=123,
                    deadline=supplied_deadline,
                )
            except BaseException as raised:
                error = raised
                result = None

        self.assertIsNone(error)
        self.assertIs(result, exact_result)
        self.assertEqual(len(calls), 1)
        deadline, _function, args, kwargs = calls[0]
        self.assertIs(deadline, supplied_deadline)
        self.assertEqual(
            args,
            (
                "https://example.invalid/supplied-deadline",
                (2, 9),
                123,
                supplied_deadline,
            ),
        )
        self.assertIs(args[-1], supplied_deadline)
        self.assertEqual(kwargs, {})

    def test_malformed_deadline_fails_before_registry_request_or_worker(self):
        now = time.monotonic()
        invalid_deadlines = (
            ("integer", int(now + 10), TypeError),
            ("boolean", True, TypeError),
            ("string", str(now + 10), TypeError),
            ("float subclass", _FloatDeadline(now + 10), TypeError),
            ("not a number", float("nan"), ValueError),
            ("positive infinity", float("inf"), ValueError),
            ("negative infinity", float("-inf"), ValueError),
            (
                "oversized finite future",
                now + (threading.TIMEOUT_MAX * 2.0),
                ValueError,
            ),
        )
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError(
                "invalid HTTP deadline resolved the registry"
            ),
        ) as get_pools:
            for label, deadline, error_type in invalid_deadlines:
                with self.subTest(deadline=label):
                    error = _capture_error(
                        lambda deadline=deadline: http_client.get_json(
                            "https://example.invalid/invalid-deadline",
                            deadline=deadline,
                        )
                    )
                    self.assertIs(type(error), error_type)

        get_pools.assert_not_called()
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertEqual(self.pools.http._workers, [])

    def test_invalid_computed_deadline_fails_before_registry_or_worker(self):
        with (
            mock.patch.object(
                http_client,
                "_HTTP_OPERATION_TIMEOUT_SECONDS",
                float("inf"),
            ),
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "invalid computed deadline resolved the registry"
                ),
            ) as get_pools,
        ):
            error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/invalid-default"
                )
            )

        self.assertIs(type(error), ValueError)
        get_pools.assert_not_called()
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertEqual(self.pools.http._workers, [])

    def test_expired_deadline_prevents_request_and_pool_startup(self):
        error = _capture_error(
            lambda: http_client.get_json(
                "https://example.invalid/expired",
                deadline=time.monotonic() - 1,
            )
        )

        self.assertIs(type(error), requests.Timeout)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertEqual(self.pools.http._workers, [])

    def test_deadline_after_response_entry_closes_without_reading(self):
        response = _http_response(b'{"secret": "never read"}')
        error, session, monotonic = self._call_with_deadline_clock(
            response,
            (-1, -1, 0),
        )

        self.assertIs(type(error), requests.Timeout)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertTrue(response.was_entered)
        self.assertTrue(response.was_closed)
        self.assertFalse(response.raw.was_read)
        self.assertEqual(monotonic.call_count, 3)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.was_closed)

    def test_deadline_between_chunks_wins_before_size_validation(self):
        response = _http_response(b"")
        response.iter_content = mock.Mock(return_value=iter((b"null", b" ")))
        error, _session, monotonic = self._call_with_deadline_clock(
            response,
            (-1, -1, -1, -1, 0),
            max_bytes=4,
        )

        self.assertIs(type(error), requests.Timeout)
        self.assertTrue(response.was_closed)
        self.assertEqual(monotonic.call_count, 5)
        response.iter_content.assert_called_once_with(chunk_size=64 * 1024)

    def test_deadline_before_decode_wins_over_unknown_encoding(self):
        response = _http_response(b"null", encoding="unknown-secret-codec")
        error, _session, monotonic = self._call_with_deadline_clock(
            response,
            (-1, -1, -1, -1, 0),
        )

        self.assertIs(type(error), requests.Timeout)
        self.assertTrue(response.was_closed)
        self.assertEqual(monotonic.call_count, 5)

    def test_deadline_before_parse_does_not_publish_decoded_text(self):
        response = _http_response(b"null")
        with mock.patch.object(
            http_client.json,
            "loads",
            side_effect=AssertionError("expired JSON text was parsed"),
        ) as loads:
            error, _session, monotonic = self._call_with_deadline_clock(
                response,
                (-1, -1, -1, -1, -1, 0),
            )

        self.assertIs(type(error), requests.Timeout)
        self.assertTrue(response.was_closed)
        self.assertEqual(monotonic.call_count, 6)
        loads.assert_not_called()

    def test_blocked_request_times_out_before_release_and_closes_late_response(self):
        request_started = threading.Event()
        release_request = threading.Event()
        response = _http_response(b'{"secret": "never publish"}')
        request_threads = []

        def blocked_request(*_args, **_kwargs):
            request_threads.append(threading.current_thread())
            request_started.set()
            if not release_request.wait(2):
                raise AssertionError("test did not release blocked HTTP request")
            return response

        self.session_request = blocked_request
        with (
            mock.patch.object(
                http_client,
                "_HTTP_OPERATION_TIMEOUT_SECONDS",
                0.02,
                create=True,
            ),
            mock.patch.object(
                http_client.json,
                "loads",
                side_effect=AssertionError("late response body was published"),
            ) as loads,
        ):
            caller, finished, outcomes = _start_call(
                lambda: http_client.get_json(
                    "https://blocked.example/servers"
                )
            )
            try:
                request_entered = request_started.wait(1)
                completed_before_release = finished.wait(0.2)
                outcome_before_release = list(outcomes)
            finally:
                release_request.set()
                caller.join(1)

        response_closed = _wait_until(lambda: response.was_closed)
        self.assertTrue(request_entered)
        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(outcome_before_release), 1)
        succeeded, error = outcome_before_release[0]
        self.assertFalse(succeeded)
        self.assertIs(type(error), requests.Timeout)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertEqual(
            self._requested_urls(),
            ["https://blocked.example/servers"],
        )
        self.assertEqual(len(request_threads), 1)
        self.assertTrue(request_threads[0].daemon)
        self.assertEqual(request_threads[0].name, "obozrenie-http")
        self.assertTrue(response_closed)
        self.assertTrue(response.was_entered)
        self.assertFalse(response.raw.was_read)
        loads.assert_not_called()

    def test_four_blockers_hold_slots_and_fifth_call_fails_without_request(self):
        release_requests = threading.Event()
        request_condition = threading.Condition()
        request_count = 0

        def blocked_request(*_args, **_kwargs):
            nonlocal request_count
            with request_condition:
                request_count += 1
                request_condition.notify_all()
            if not release_requests.wait(2):
                raise AssertionError("test did not release saturated HTTP worker")
            return _http_response(b"null")

        callers = []
        finished_events = []
        outcomes = []
        fifth_elapsed = []
        self.pools.http.ensure_started()
        self.session_request = blocked_request
        with mock.patch.object(
            http_client,
            "_HTTP_OPERATION_TIMEOUT_SECONDS",
            0.02,
            create=True,
        ):
            try:
                for index in range(4):
                    caller, finished, call_outcomes = _start_call(
                        lambda: http_client.get_json(
                            "https://blocked.example/servers"
                        ),
                        name=f"test-http-blocker-{index}",
                    )
                    callers.append(caller)
                    finished_events.append(finished)
                    outcomes.append(call_outcomes)
                    with request_condition:
                        request_entered = request_condition.wait_for(
                            lambda: request_count >= index + 1,
                            timeout=1,
                        )
                    self.assertTrue(request_entered)
                    self.assertTrue(finished.wait(0.2))

                def fifth_call():
                    started = time.monotonic()
                    try:
                        return http_client.get_json(
                            "https://blocked.example/fifth"
                        )
                    finally:
                        fifth_elapsed.append(time.monotonic() - started)

                fifth_caller, fifth_finished, fifth_outcomes = _start_call(
                    fifth_call,
                    name="test-http-saturated-caller",
                )
                callers.append(fifth_caller)
                finished_events.append(fifth_finished)
                outcomes.append(fifth_outcomes)
                fifth_completed_before_release = fifth_finished.wait(0.2)
                calls_before_release = len(self._requested_urls())
                with self.pools.http._state_lock:
                    active_before_release = len(self.pools.http._active)
            finally:
                release_requests.set()
                for caller in callers:
                    caller.join(1)

        self.assertTrue(fifth_completed_before_release)
        self.assertEqual(calls_before_release, 4)
        self.assertEqual(active_before_release, 4)
        self.assertEqual(len(fifth_elapsed), 1)
        self.assertLess(fifth_elapsed[0], 0.02)
        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(len(outcomes), 5)
        errors = []
        for call_outcomes in outcomes:
            self.assertEqual(len(call_outcomes), 1)
            succeeded, error = call_outcomes[0]
            self.assertFalse(succeeded)
            self.assertIs(type(error), requests.Timeout)
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
            errors.append(error)
        self.assertEqual(len({id(error) for error in errors}), 5)

    def test_completion_published_before_false_wait_returns_exact_json_result(self):
        exact_result = _WeakJsonValue()
        waited = []
        real_init = network_calls.BoundedCall.__init__

        class FalseWaitAfterCompletion:
            def __init__(self, event):
                self._event = event

            def set(self):
                self._event.set()

            def wait(self, timeout=None):
                completed = self._event.wait(timeout)
                waited.append(completed)
                return False

            def is_set(self):
                return self._event.is_set()

        def install_false_wait(call, *args, **kwargs):
            real_init(call, *args, **kwargs)
            call._event = FalseWaitAfterCompletion(call._event)

        self._set_sessions(_FakeSession((_http_response(b"null"),)))
        with (
            mock.patch.object(
                network_calls.BoundedCall,
                "__init__",
                new=install_false_wait,
            ),
            mock.patch.object(
                http_client.json,
                "loads",
                return_value=exact_result,
            ),
        ):
            result = http_client.get_json(
                "https://example.invalid/completion-race"
            )

        self.assertIs(result, exact_result)
        self.assertEqual(waited, [True])

    def _assert_late_json_outcome_is_discarded(self, *, raises):
        parse_started = threading.Event()
        release_parse = threading.Event()
        references = []
        response = _http_response(b"null")

        def delayed_parse(_text):
            parse_started.set()
            if not release_parse.wait(2):
                raise AssertionError("test did not release blocked JSON parse")
            if raises:
                error = _WeakJsonError(
                    "late JSON operation failed",
                    bytearray(1024 * 1024),
                )
                references.append(weakref.ref(error))
                raise error
            value = _WeakJsonValue()
            references.append(weakref.ref(value))
            return value

        self._set_sessions(_FakeSession((response,)))
        with (
            mock.patch.object(
                http_client,
                "_HTTP_OPERATION_TIMEOUT_SECONDS",
                0.02,
                create=True,
            ),
            mock.patch.object(
                http_client.json,
                "loads",
                side_effect=delayed_parse,
            ),
        ):
            caller, finished, outcomes = _start_call(
                lambda: http_client.get_json(
                    "https://example.invalid/late-json"
                )
            )
            try:
                parser_entered = parse_started.wait(1)
                completed_before_release = finished.wait(0.2)
                outcome_before_release = list(outcomes)
            finally:
                release_parse.set()
                caller.join(1)

        worker_finished = _wait_until(lambda: not self.pools.http._active)

        def outcome_was_collected():
            gc.collect()
            return bool(references) and references[0]() is None

        collected = _wait_until(outcome_was_collected)
        self.assertTrue(parser_entered)
        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(outcome_before_release), 1)
        succeeded, error = outcome_before_release[0]
        self.assertFalse(succeeded)
        self.assertIs(type(error), requests.Timeout)
        self.assertTrue(worker_finished)
        self.assertEqual(len(references), 1)
        self.assertTrue(collected)
        self.assertIsNone(references[0]())
        self.assertTrue(response.was_closed)

    def test_timeout_discards_late_weak_json_result(self):
        self._assert_late_json_outcome_is_discarded(raises=False)

    def test_timeout_discards_late_weak_operation_exception(self):
        self._assert_late_json_outcome_is_discarded(raises=True)

    def test_worker_requests_timeout_is_propagated_by_identity(self):
        worker_error = requests.Timeout("worker transport timeout")
        request_threads = []

        def fail_request(*_args, **_kwargs):
            request_threads.append(threading.current_thread())
            raise worker_error

        self.session_request = fail_request

        raised = _capture_error(
            lambda: http_client.get_json(
                "https://example.invalid/worker-timeout"
            )
        )

        self.assertIs(raised, worker_error)
        self.assertEqual(
            self._requested_urls(),
            ["https://example.invalid/worker-timeout"],
        )
        self.assertEqual(len(request_threads), 1)
        self.assertIsNot(request_threads[0], threading.current_thread())
        self.assertTrue(request_threads[0].daemon)
        self.assertEqual(request_threads[0].name, "obozrenie-http")

    def test_exact_call_phase_infrastructure_errors_are_fresh_requests_errors(self):
        cases = (
            (
                network_calls.CallDeadlineExceeded("deadline"),
                requests.Timeout,
            ),
            (
                network_calls.PoolSaturatedError("saturated"),
                requests.Timeout,
            ),
            (
                network_calls.PoolUnavailableError("closed"),
                requests.ConnectionError,
            ),
        )
        for infrastructure_error, requests_error_type in cases:
            with self.subTest(error=type(infrastructure_error).__name__):
                with mock.patch.object(
                    self.pools.http,
                    "call_until",
                    side_effect=infrastructure_error,
                ):
                    first_error = _capture_error(
                        lambda: http_client.get_json(
                            "https://example.invalid/first-map-error"
                        )
                    )
                    second_error = _capture_error(
                        lambda: http_client.get_json(
                            "https://example.invalid/second-map-error"
                        )
                    )

                self.assertIs(type(first_error), requests_error_type)
                self.assertIs(type(second_error), requests_error_type)
                self.assertIsNot(first_error, second_error)
                self.assertIsNot(first_error, infrastructure_error)
                self.assertIsNot(second_error, infrastructure_error)
                for error in (first_error, second_error):
                    self.assertIsNone(error.__cause__)
                    self.assertTrue(error.__suppress_context__)

    def test_derived_and_unrelated_call_phase_errors_propagate_by_identity(self):
        class DerivedDeadline(network_calls.CallDeadlineExceeded):
            pass

        class DerivedSaturated(network_calls.PoolSaturatedError):
            pass

        class DerivedUnavailable(network_calls.PoolUnavailableError):
            pass

        class InfrastructureRequestsTimeout(
            network_calls.CallDeadlineExceeded,
            requests.Timeout,
        ):
            pass

        errors = (
            DerivedDeadline("derived deadline"),
            DerivedSaturated("derived saturation"),
            DerivedUnavailable("derived closure"),
            InfrastructureRequestsTimeout("multiple inheritance"),
            RuntimeError("unrelated map failure"),
        )
        for map_error in errors:
            with self.subTest(error=type(map_error).__name__):
                with mock.patch.object(
                    self.pools.http,
                    "call_until",
                    side_effect=map_error,
                ):
                    raised = _capture_error(
                        lambda: http_client.get_json(
                            "https://example.invalid/unexpected-map-error"
                        )
                    )

                self.assertIs(raised, map_error)

    def test_startup_base_exception_and_sticky_failure_are_fresh_connection_errors(self):
        initial_error = _StartupAbort(
            "startup payload",
            bytearray(1024 * 1024),
        )
        with mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ) as thread_factory:
            first_error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/first-startup"
                )
            )
            later_error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/later-startup"
                )
            )

        self.assertEqual(thread_factory.call_count, 1)
        self.assertTrue(self.pools.http.startup_failed)
        self.assertEqual(self.pools.http._workers, [])
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()
        self.assertIs(type(first_error), requests.ConnectionError)
        self.assertIs(type(later_error), requests.ConnectionError)
        self.assertIsNot(first_error, later_error)
        self.assertIsNot(first_error, initial_error)
        self.assertIsNot(later_error, initial_error)
        for error in (first_error, later_error):
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
            self.assertNotIn("startup payload", str(error))

    def test_call_phase_keyboard_interrupt_propagates_after_sticky_startup_failure(self):
        initial_error = _StartupAbort("initial startup interruption")
        caller_interruption = KeyboardInterrupt("caller interrupted HTTP wait")
        with mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ):
            startup_error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/failed-startup"
                )
            )

        self.assertIs(type(startup_error), requests.ConnectionError)
        self.assertTrue(self.pools.http.startup_failed)
        with (
            mock.patch.object(
                self.pools.http,
                "ensure_started",
                return_value=None,
            ),
            mock.patch.object(
                self.pools.http,
                "call_until",
                side_effect=caller_interruption,
            ) as call_until,
        ):
            raised = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/caller-interruption"
                )
            )

        self.assertIs(raised, caller_interruption)
        call_until.assert_called_once()
        self.assertTrue(self.pools.http.startup_failed)

    def test_close_between_startup_and_call_is_fresh_connection_error(self):
        self.pools.http.ensure_started()
        real_call_until = self.pools.http.call_until

        def close_then_call(*args, **kwargs):
            self.pools.http.begin_close()
            return real_call_until(*args, **kwargs)

        with mock.patch.object(
            self.pools.http,
            "call_until",
            side_effect=close_then_call,
        ):
            error = _capture_error(
                lambda: http_client.get_json(
                    "https://example.invalid/closed-pool"
                )
            )

        self.assertIs(type(error), requests.ConnectionError)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.session_constructor.assert_not_called()
        self.direct_request.assert_not_called()

    def test_running_close_wakes_caller_and_closes_late_response(self):
        request_started = threading.Event()
        release_request = threading.Event()
        response = _http_response(b'{"secret": "never publish"}')

        def blocked_request(*_args, **_kwargs):
            request_started.set()
            if not release_request.wait(2):
                raise AssertionError("test did not release running HTTP request")
            return response

        clock = SimpleNamespace(now=time.monotonic())
        deadline = clock.now + 10.0
        fake_time = SimpleNamespace(monotonic=lambda: clock.now)
        self.session_request = blocked_request
        with (
            mock.patch.object(http_client, "time", fake_time),
            mock.patch.object(
                http_client.json,
                "loads",
                side_effect=AssertionError("closed call published late JSON"),
            ) as loads,
        ):
            caller, finished, outcomes = _start_call(
                lambda: http_client.get_json(
                    "https://blocked.example/running-close",
                    deadline=deadline,
                ),
                name="test-http-running-close-caller",
            )
            try:
                worker_claimed = request_started.wait(1)
                self.pools.http.begin_close()
                completed_before_release = finished.wait(0.2)
                outcome_before_release = list(outcomes)
                response_before_release = (
                    response.was_entered,
                    response.raw.was_read,
                    response.was_closed,
                )
                clock.now = deadline
            finally:
                release_request.set()
                caller.join(1)
            response_closed = _wait_until(lambda: response.was_closed)

            def token_returned():
                with self.pools.http._state_lock:
                    return (
                        not self.pools.http._active
                        and len(self.pools.http._available_tokens)
                        == self.pools.http.capacity
                    )

            capacity_returned = _wait_until(token_returned)
        self.assertTrue(worker_claimed)
        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertEqual(response_before_release, (False, False, False))
        self.assertEqual(len(outcome_before_release), 1)
        succeeded, error = outcome_before_release[0]
        self.assertFalse(succeeded)
        self.assertIs(type(error), requests.ConnectionError)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertEqual(
            self._requested_urls(),
            ["https://blocked.example/running-close"],
        )
        self.assertTrue(response_closed)
        self.assertTrue(response.was_entered)
        self.assertFalse(response.raw.was_read)
        loads.assert_not_called()
        self.assertTrue(capacity_returned)

    def test_default_timeout_and_streaming_are_used(self):
        session = _FakeSession((_http_response(b'{"servers": []}'),))
        self._set_sessions(session)

        result = http_client.get_json("https://example.invalid/servers")

        self.assertEqual(result, {"servers": []})
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/servers",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (5, 15),
                    },
                )
            ],
        )
        self.assertTrue(session.was_entered)
        self.assertTrue(session.was_closed)
        self.direct_request.assert_not_called()

    def test_custom_timeout_is_forwarded(self):
        session = _FakeSession((_http_response(b"null"),))
        self._set_sessions(session)

        result = http_client.get_json(
            "https://example.invalid/servers",
            timeout=(2, 9),
        )

        self.assertIsNone(result)
        self.assertEqual(
            session.calls,
            [
                (
                    "https://example.invalid/servers",
                    {
                        "allow_redirects": False,
                        "stream": True,
                        "timeout": (2, 9),
                    },
                )
            ],
        )
        self.direct_request.assert_not_called()

    def test_http_status_error_propagates_before_body_read(self):
        response = _http_response(b'{"error": "unavailable"}', status_code=503)
        session = _FakeSession((response,))
        self._set_sessions(session)

        with self.assertRaises(requests.HTTPError) as raised:
            http_client.get_json("https://example.invalid/servers")

        self.assertIs(raised.exception.response, response)
        self.assertFalse(response.raw.was_read)
        self.assertTrue(response.was_closed)
        self.assertTrue(session.was_closed)

    def test_body_at_default_byte_limit_is_accepted(self):
        max_bytes = 16 * 1024 * 1024
        body = b'"' + (b"x" * (max_bytes - 2)) + b'"'
        self._set_sessions(_FakeSession((_http_response(body),)))

        result = http_client.get_json("https://example.invalid/servers")

        self.assertIsInstance(result, str)
        self.assertEqual(len(result), max_bytes - 2)
        self.assertEqual(result[:1], "x")
        self.assertEqual(result[-1:], "x")

    def test_body_one_byte_over_default_limit_is_rejected(self):
        max_bytes = 16 * 1024 * 1024
        response = _http_response(b'"' + (b"x" * (max_bytes - 1)) + b'"')
        self._set_sessions(_FakeSession((response,)))

        with self.assertRaises(ValueError):
            http_client.get_json("https://example.invalid/servers")

        self.assertTrue(response.was_closed)

    def test_custom_max_bytes_controls_exact_boundary_and_closure(self):
        at_limit = _http_response(b"null")
        over_limit = _http_response(b"null ")
        first_session = _FakeSession((at_limit,))
        second_session = _FakeSession((over_limit,))
        self._set_sessions(first_session, second_session)

        self.assertIsNone(
            http_client.get_json(
                "https://example.invalid/servers",
                max_bytes=4,
            )
        )
        with self.assertRaises(ValueError) as raised:
            http_client.get_json(
                "https://example.invalid/servers",
                max_bytes=4,
            )

        self.assertEqual(str(raised.exception), "HTTP response exceeds 4 bytes")
        self.assertTrue(at_limit.was_closed)
        self.assertTrue(over_limit.was_closed)
        secure_call = (
            "https://example.invalid/servers",
            {"allow_redirects": False, "stream": True, "timeout": (5, 15)},
        )
        self.assertEqual(first_session.calls, [secure_call])
        self.assertEqual(second_session.calls, [secure_call])
        self.assertTrue(first_session.was_closed)
        self.assertTrue(second_session.was_closed)

    def test_malformed_json_is_one_isolated_decode_failure(self):
        response = _http_response(b'{"servers": ]')
        self._set_sessions(_FakeSession((response,)))

        error = _capture_error(
            lambda: http_client.get_json("https://example.invalid/servers")
        )

        self.assert_isolated_decode_failure(error, response)

    def test_response_encoding_is_used(self):
        self._set_sessions(
            _FakeSession((
                _http_response(
                    b'{"word": "caf\xe9"}',
                    encoding="iso-8859-1",
                ),
            ))
        )

        result = http_client.get_json("https://example.invalid/servers")

        self.assertEqual(result, {"word": "caf\u00e9"})

    def test_missing_response_encoding_uses_utf8(self):
        self._set_sessions(
            _FakeSession((_http_response(b'{"word": "caf\xc3\xa9"}'),))
        )

        result = http_client.get_json("https://example.invalid/servers")

        self.assertEqual(result, {"word": "caf\u00e9"})

    def test_response_is_closed_after_successful_read(self):
        response = _http_response(b"[]")
        session = _FakeSession((response,))
        self._set_sessions(session)

        self.assertEqual(
            http_client.get_json("https://example.invalid/servers"),
            [],
        )
        self.assertTrue(response.was_closed)
        self.assertTrue(session.was_closed)


class RttTests(unittest.TestCase):
    def setUp(self):
        self.pools = self.enterContext(installed_network_pools())
        self.enterContext(
            mock.patch.object(
                ping,
                "_RTT_BATCH_TIMEOUT_SECONDS",
                0.02,
                create=True,
            )
        )

    def test_default_deadline_and_first_seen_hosts_use_one_ordered_map(self):
        first = Endpoint("second.example", 1)
        second = Endpoint("first.example", 2)
        same_host = Endpoint("second.example", 3)
        calls = []

        def map_hosts(deadline, function, hosts, /):
            calls.append((deadline, function, tuple(hosts)))
            return [
                network_calls.CallOutcome.success(20),
                network_calls.CallOutcome.success(10),
            ]

        monotonic = mock.Mock(return_value=100.0)
        fake_time = SimpleNamespace(monotonic=monotonic)
        with (
            mock.patch.object(ping, "time", fake_time, create=True),
            mock.patch.object(
                self.pools.icmp,
                "map_until",
                side_effect=map_hosts,
            ),
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError("recorded map invoked ICMP"),
            ) as ping_call,
        ):
            measured = ping.measure_rtt((first, second, same_host))

        self.assertEqual(
            list(measured.items()),
            [(first, 20), (second, 10), (same_host, 20)],
        )
        self.assertEqual(
            calls,
            [(100.02, ping._measure_host, ("second.example", "first.example"))],
        )
        monotonic.assert_called_once_with()
        ping_call.assert_not_called()

    def test_supplied_absolute_deadline_is_forwarded_unchanged(self):
        endpoint = Endpoint("deadline.example", 1)
        supplied_deadline = float(time.monotonic() + 1)
        calls = []

        def map_host(deadline, function, hosts, /):
            calls.append((deadline, function, tuple(hosts)))
            return [network_calls.CallOutcome.success(7)]

        fake_time = SimpleNamespace(
            monotonic=mock.Mock(
                side_effect=AssertionError("supplied RTT deadline was replaced")
            )
        )
        with (
            mock.patch.object(ping, "time", fake_time, create=True),
            mock.patch.object(
                self.pools.icmp,
                "map_until",
                side_effect=map_host,
            ),
        ):
            measured = ping.measure_rtt(
                [endpoint],
                deadline=supplied_deadline,
            )

        self.assertEqual(measured, {endpoint: 7})
        self.assertEqual(len(calls), 1)
        deadline, function, hosts = calls[0]
        self.assertIs(deadline, supplied_deadline)
        self.assertIs(function, ping._measure_host)
        self.assertEqual(hosts, ("deadline.example",))

    def test_malformed_deadline_fails_before_registry_or_icmp_workers(self):
        now = time.monotonic()
        invalid_deadlines = (
            ("integer", int(now + 10), TypeError),
            ("boolean", True, TypeError),
            ("string", str(now + 10), TypeError),
            ("float subclass", _FloatDeadline(now + 10), TypeError),
            ("not a number", float("nan"), ValueError),
            ("positive infinity", float("inf"), ValueError),
            ("negative infinity", float("-inf"), ValueError),
            (
                "oversized finite future",
                now + (threading.TIMEOUT_MAX * 2.0),
                ValueError,
            ),
        )
        endpoint = Endpoint("invalid-deadline.example", 1)
        with (
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "invalid RTT deadline resolved the registry"
                ),
            ) as get_pools,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError("invalid RTT deadline reached ICMP"),
            ) as ping_call,
        ):
            for label, deadline, error_type in invalid_deadlines:
                with self.subTest(deadline=label):
                    error = _capture_error(
                        lambda deadline=deadline: ping.measure_rtt(
                            [endpoint],
                            deadline=deadline,
                        )
                    )
                    self.assertIs(type(error), error_type)

        get_pools.assert_not_called()
        ping_call.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_malformed_computed_deadline_fails_before_registry_or_icmp_workers(self):
        malformed_clocks = (
            ("not a number", float("nan")),
            ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")),
            (
                "oversized finite future",
                time.monotonic() + (threading.TIMEOUT_MAX * 2.0),
            ),
        )
        endpoint = Endpoint("invalid-clock.example", 1)
        with (
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "invalid computed RTT deadline resolved the registry"
                ),
            ) as get_pools,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError(
                    "invalid computed RTT deadline reached ICMP"
                ),
            ) as ping_call,
        ):
            for label, clock_value in malformed_clocks:
                fake_time = SimpleNamespace(
                    monotonic=mock.Mock(return_value=clock_value)
                )
                with self.subTest(clock=label), mock.patch.object(
                    ping,
                    "time",
                    fake_time,
                ):
                    error = _capture_error(
                        lambda: ping.measure_rtt([endpoint])
                    )
                    self.assertIs(type(error), ValueError)

        get_pools.assert_not_called()
        ping_call.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_measure_rtt_rejects_invalid_deadline_before_endpoint_iteration(self):
        invalid_deadlines = (
            ("wrong type", 1, TypeError),
            ("not a number", float("nan"), ValueError),
        )
        with (
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "invalid measure_rtt deadline resolved the registry"
                ),
            ) as get_pools,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError(
                    "invalid measure_rtt deadline reached ICMP"
                ),
            ) as ping_call,
        ):
            for label, deadline, error_type in invalid_deadlines:
                with self.subTest(deadline=label):
                    endpoints = _ExplodingIterable()
                    error = _capture_error(
                        lambda endpoints=endpoints, deadline=deadline: (
                            ping.measure_rtt(endpoints, deadline=deadline)
                        )
                    )
                    self.assertIs(type(error), error_type)
                    self.assertFalse(endpoints.iterated)

        get_pools.assert_not_called()
        ping_call.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_add_rtt_info_rejects_invalid_deadline_before_server_iteration(self):
        invalid_deadlines = (
            ("wrong type", 1, TypeError),
            ("not a number", float("nan"), ValueError),
        )
        with (
            mock.patch.object(
                ping,
                "measure_rtt",
                side_effect=AssertionError(
                    "add_rtt_info delegated before validating its own deadline"
                ),
            ) as measure_rtt,
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "invalid add_rtt_info deadline resolved the registry"
                ),
            ) as get_pools,
        ):
            for label, deadline, error_type in invalid_deadlines:
                with self.subTest(deadline=label):
                    servers = _ExplodingIterable()
                    error = _capture_error(
                        lambda servers=servers, deadline=deadline: (
                            ping.add_rtt_info(servers, deadline=deadline)
                        )
                    )
                    self.assertIs(type(error), error_type)
                    self.assertFalse(servers.iterated)

        measure_rtt.assert_not_called()
        get_pools.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_empty_iterables_validate_supplied_deadline_before_fast_path(self):
        invalid_deadlines = (
            ("wrong type", 1, TypeError),
            ("not a number", float("nan"), ValueError),
        )
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError(
                "empty RTT input with an invalid deadline resolved the registry"
            ),
        ) as get_pools:
            for label, deadline, error_type in invalid_deadlines:
                with self.subTest(facade="measure_rtt", deadline=label):
                    error = _capture_error(
                        lambda deadline=deadline: ping.measure_rtt(
                            iter(()),
                            deadline=deadline,
                        )
                    )
                    self.assertIs(type(error), error_type)
                with self.subTest(facade="add_rtt_info", deadline=label):
                    error = _capture_error(
                        lambda deadline=deadline: ping.add_rtt_info(
                            iter(()),
                            deadline=deadline,
                        )
                    )
                    self.assertIs(type(error), error_type)

        get_pools.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_empty_inputs_without_deadline_do_not_read_clock_or_registry(self):
        fake_time = SimpleNamespace(
            monotonic=mock.Mock(
                side_effect=AssertionError(
                    "empty RTT input with no deadline read the clock"
                )
            )
        )
        with (
            mock.patch.object(ping, "time", fake_time, create=True),
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "empty RTT input with no deadline resolved the registry"
                ),
            ) as get_pools,
        ):
            self.assertEqual(ping.measure_rtt(iter(())), {})
            self.assertEqual(ping.add_rtt_info(iter(())), [])

        get_pools.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_empty_iterables_with_valid_supplied_deadline_do_not_touch_the_registry(self):
        # A valid supplied deadline still passes through
        # network_calls.validate_deadline, which reads its own module's
        # clock to check the TIMEOUT_MAX bound (obozrenie/network_calls.py's
        # own concern, not asserted here); only the registry/pool access is
        # guaranteed to stay unreached for an empty batch.
        valid_deadline = time.monotonic() + 1
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError(
                "empty RTT input with a valid supplied deadline resolved "
                "the registry"
            ),
        ) as get_pools:
            self.assertEqual(
                ping.measure_rtt(iter(()), deadline=valid_deadline),
                {},
            )
            self.assertEqual(
                ping.add_rtt_info(iter(()), deadline=valid_deadline),
                [],
            )

        get_pools.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    def test_finite_past_and_timeout_boundary_deadlines_are_forwarded_unchanged(self):
        deadlines = (
            float("-1e300"),
            float("100.0"),
            float("101.5"),
            float(100.0 + threading.TIMEOUT_MAX),
        )
        endpoint = Endpoint("finite-deadline.example", 1)
        received = []

        def map_host(deadline, function, hosts, /):
            received.append((deadline, function, tuple(hosts)))
            return [network_calls.CallOutcome.success(7)]

        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))
        with (
            mock.patch.object(network_calls, "time", fake_time),
            mock.patch.object(
                self.pools.icmp,
                "map_until",
                side_effect=map_host,
            ),
        ):
            results = [
                ping.measure_rtt([endpoint], deadline=deadline)
                for deadline in deadlines
            ]

        self.assertEqual(results, [{endpoint: 7}] * len(deadlines))
        self.assertEqual(len(received), len(deadlines))
        for supplied, (forwarded, function, hosts) in zip(
            deadlines,
            received,
            strict=True,
        ):
            self.assertIs(forwarded, supplied)
            self.assertIs(function, ping._measure_host)
            self.assertEqual(hosts, ("finite-deadline.example",))

    def test_blocked_icmp_dns_returns_all_sentinels_before_release(self):
        self.pools.icmp.ensure_started()
        condition = threading.Condition()
        release = threading.Event()
        all_returned = threading.Event()
        entered = 0
        returned = 0

        def blocked_ping(host, **_options):
            nonlocal entered, returned
            with condition:
                entered += 1
                condition.notify_all()
            if not release.wait(2):
                raise AssertionError("test did not release blocked ICMP DNS")
            with condition:
                returned += 1
                if returned == 2:
                    all_returned.set()
            return Host(host, 1, [4.0])

        endpoints = [
            Endpoint("blocked-one.example", 1),
            Endpoint("blocked-one.example", 2),
            Endpoint("blocked-two.example", 3),
        ]
        with mock.patch(
            "obozrenie.ping.icmp_ping",
            side_effect=blocked_ping,
        ) as ping_call:
            caller, finished, outcomes = _start_call(
                lambda: ping.measure_rtt(endpoints),
                name="test-blocked-icmp-dns-caller",
            )
            try:
                with condition:
                    all_entered = condition.wait_for(
                        lambda: entered == 2,
                        timeout=1,
                    )
                completed_before_release = finished.wait(0.2)
                outcomes_before_release = list(outcomes)
            finally:
                release.set()
                caller.join(1)

        late_calls_returned = all_returned.wait(1)
        self.assertTrue(all_entered)
        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertTrue(late_calls_returned)
        self.assertEqual(
            outcomes_before_release,
            [
                (
                    True,
                    {
                        endpoints[0]: 9999,
                        endpoints[1]: 9999,
                        endpoints[2]: 9999,
                    },
                )
            ],
        )
        self.assertEqual(ping_call.call_count, 2)

    def test_icmp_pool_workers_are_named_daemons_without_an_executor(self):
        endpoint = Endpoint("worker.example", 1)
        with (
            mock.patch.object(
                ping,
                "ThreadPoolExecutor",
                create=True,
                side_effect=AssertionError("RTT constructed ThreadPoolExecutor"),
            ) as executor,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                return_value=Host("worker.example", 1, [2.0]),
            ),
        ):
            measured = ping.measure_rtt(
                [endpoint],
                deadline=time.monotonic() + 1,
            )

        workers = list(self.pools.icmp._workers)
        self.assertEqual(measured, {endpoint: 2})
        executor.assert_not_called()
        self.assertEqual(len(workers), 32)
        self.assertTrue(all(worker.daemon for worker in workers))
        self.assertEqual({worker.name for worker in workers}, {"obozrenie-icmp"})

    def test_32_stuck_calls_hold_every_slot_and_more_hosts_fail_fast(self):
        self.pools.icmp.ensure_started()
        condition = threading.Condition()
        release = threading.Event()
        entered = 0

        def blocked_ping(host, **_options):
            nonlocal entered
            with condition:
                entered += 1
                condition.notify_all()
            if not release.wait(2):
                raise AssertionError("test did not release saturated ICMP worker")
            return Host(host, 1, [1.0])

        blocked_endpoints = [
            Endpoint(f"blocked-{index}.example", index + 1)
            for index in range(32)
        ]
        extra_endpoints = [
            Endpoint(f"extra-{index}.example", index + 100)
            for index in range(8)
        ]
        extra_measured = None
        extra_elapsed = None
        with mock.patch(
            "obozrenie.ping.icmp_ping",
            side_effect=blocked_ping,
        ) as ping_call:
            caller, finished, outcomes = _start_call(
                lambda: ping.measure_rtt(blocked_endpoints),
                name="test-saturated-icmp-caller",
            )
            try:
                with condition:
                    all_entered = condition.wait_for(
                        lambda: entered == 32,
                        timeout=1,
                    )
                first_completed = finished.wait(0.2)
                if first_completed:
                    started = time.monotonic()
                    extra_measured = ping.measure_rtt(
                        extra_endpoints,
                        deadline=time.monotonic() + 1,
                    )
                    extra_elapsed = time.monotonic() - started
                calls_before_release = ping_call.call_count
            finally:
                release.set()
                caller.join(1)

        self.assertTrue(all_entered)
        self.assertTrue(first_completed)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0][0])
        self.assertEqual(set(outcomes[0][1].values()), {9999})
        self.assertEqual(
            extra_measured,
            {endpoint: 9999 for endpoint in extra_endpoints},
        )
        self.assertIsNotNone(extra_elapsed)
        self.assertLess(extra_elapsed, 0.2)
        self.assertEqual(calls_before_release, 32)

    def test_late_rtt_cannot_mutate_returned_server_replacement(self):
        self.pools.icmp.ensure_started()
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def blocked_ping(host, **_options):
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release late ICMP result")
            returned.set()
            return Host(host, 1, [3.0])

        server = Server(
            endpoint=Endpoint("late.example", 30000),
            game_id="g",
            ping=77,
            country="CA",
        )
        with mock.patch(
            "obozrenie.ping.icmp_ping",
            side_effect=blocked_ping,
        ):
            caller, finished, outcomes = _start_call(
                lambda: ping.add_rtt_info([server]),
                name="test-late-icmp-server-caller",
            )
            try:
                entered = started.wait(1)
                completed_before_release = finished.wait(0.2)
                outcomes_before_release = list(outcomes)
            finally:
                release.set()
                caller.join(1)

        late_call_returned = returned.wait(1)
        expected = [
            Server(
                endpoint=Endpoint("late.example", 30000),
                game_id="g",
                ping=9999,
                country="CA",
            )
        ]
        self.assertTrue(entered)
        self.assertTrue(completed_before_release)
        self.assertTrue(late_call_returned)
        self.assertFalse(caller.is_alive())
        self.assertEqual(outcomes_before_release, [(True, expected)])
        self.assertEqual(outcomes[0][1], expected)
        self.assertEqual(server.ping, 77)

    def test_infrastructure_outcomes_become_sentinels(self):
        endpoints = [
            Endpoint("deadline.example", 1),
            Endpoint("saturated.example", 2),
            Endpoint("unavailable.example", 3),
            Endpoint("working.example", 4),
        ]
        outcomes = [
            network_calls.CallOutcome.failure(
                network_calls.CallDeadlineExceeded("deadline")
            ),
            network_calls.CallOutcome.failure(
                network_calls.PoolSaturatedError("saturated")
            ),
            network_calls.CallOutcome.failure(
                network_calls.PoolUnavailableError("unavailable")
            ),
            network_calls.CallOutcome.success(6),
        ]
        with mock.patch.object(
            self.pools.icmp,
            "map_until",
            return_value=outcomes,
        ):
            measured = ping.measure_rtt(
                endpoints,
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(
            measured,
            {
                endpoints[0]: 9999,
                endpoints[1]: 9999,
                endpoints[2]: 9999,
                endpoints[3]: 6,
            },
        )

    def test_infrastructure_outcome_subclass_propagates_by_identity(self):
        class UnexpectedUnavailable(network_calls.PoolUnavailableError):
            pass

        operation_error = UnexpectedUnavailable("unexpected operation error")
        endpoint = Endpoint("unexpected-outcome.example", 1)
        with mock.patch.object(
            self.pools.icmp,
            "map_until",
            return_value=[network_calls.CallOutcome.failure(operation_error)],
        ):
            raised = _capture_error(
                lambda: ping.measure_rtt(
                    [endpoint],
                    deadline=time.monotonic() + 1,
                )
            )

        self.assertIs(raised, operation_error)

    def test_startup_base_exception_and_sticky_failure_become_sentinels(self):
        initial_error = _StartupAbort(
            "startup payload",
            bytearray(1024 * 1024),
        )
        first = Endpoint("first-startup.example", 1)
        second = Endpoint("second-startup.example", 2)
        later = Endpoint("later-startup.example", 3)
        with (
            mock.patch.object(
                network_calls.threading,
                "Thread",
                side_effect=initial_error,
            ) as thread_factory,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError("startup failure reached ICMP"),
            ) as ping_call,
        ):
            first_measured = ping.measure_rtt(
                [first, second],
                deadline=time.monotonic() + 1,
            )
            later_measured = ping.measure_rtt(
                [later],
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(first_measured, {first: 9999, second: 9999})
        self.assertEqual(later_measured, {later: 9999})
        self.assertEqual(thread_factory.call_count, 1)
        self.assertTrue(self.pools.icmp.startup_failed)
        self.assertEqual(self.pools.icmp._workers, [])
        ping_call.assert_not_called()

    def test_close_between_startup_and_map_becomes_sentinels(self):
        endpoint = Endpoint("closed.example", 1)
        self.pools.icmp.ensure_started()
        real_map_until = self.pools.icmp.map_until

        def close_then_map(*args):
            self.pools.icmp.begin_close()
            return real_map_until(*args)

        with (
            mock.patch.object(
                self.pools.icmp,
                "map_until",
                side_effect=close_then_map,
            ) as map_until,
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError("closed pool reached ICMP"),
            ) as ping_call,
        ):
            measured = ping.measure_rtt(
                [endpoint],
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(measured, {endpoint: 9999})
        map_until.assert_called_once()
        ping_call.assert_not_called()

    def test_exact_map_unavailable_error_becomes_all_sentinels(self):
        map_error = network_calls.PoolUnavailableError("closed during map")
        pool = _MapErrorPool(map_error)
        pools = network_calls.NetworkCallPools(
            dns=mock.Mock(),
            http=mock.Mock(),
            icmp=pool,
        )
        first = Endpoint("closed-map.example", 1)
        second = Endpoint("closed-map.example", 2)
        with (
            mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools),
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=AssertionError("map failure reached ICMP"),
            ) as ping_call,
        ):
            measured = ping.measure_rtt(
                [first, second],
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(measured, {first: 9999, second: 9999})
        self.assertEqual(pool.ensure_calls, 1)
        self.assertEqual(len(pool.map_calls), 1)
        ping_call.assert_not_called()

    def test_map_unavailable_subclass_propagates_by_identity(self):
        class UnexpectedUnavailable(network_calls.PoolUnavailableError):
            pass

        map_error = UnexpectedUnavailable("unexpected map failure")
        pool = _MapErrorPool(map_error)
        pools = network_calls.NetworkCallPools(
            dns=mock.Mock(),
            http=mock.Mock(),
            icmp=pool,
        )
        endpoint = Endpoint("unexpected-map.example", 1)
        raised = None
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            try:
                ping.measure_rtt(
                    [endpoint],
                    deadline=time.monotonic() + 1,
                )
            except BaseException as error:
                raised = error

        self.assertIs(raised, map_error)
        self.assertEqual(pool.ensure_calls, 1)
        self.assertEqual(len(pool.map_calls), 1)

    def test_unexpected_map_runtime_error_propagates_by_identity(self):
        map_error = RuntimeError("unexpected map programming error")
        pool = _MapErrorPool(map_error)
        pools = network_calls.NetworkCallPools(
            dns=mock.Mock(),
            http=mock.Mock(),
            icmp=pool,
        )
        endpoint = Endpoint("runtime-map.example", 1)
        raised = None
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            try:
                ping.measure_rtt(
                    [endpoint],
                    deadline=time.monotonic() + 1,
                )
            except BaseException as error:
                raised = error

        self.assertIs(raised, map_error)
        self.assertEqual(pool.ensure_calls, 1)
        self.assertEqual(len(pool.map_calls), 1)

    def test_map_caller_interruption_propagates_after_sticky_startup_failure(self):
        initial_error = _StartupAbort("initial ICMP startup failure")
        first = Endpoint("startup.example", 1)
        interrupted = Endpoint("interrupted.example", 2)
        with mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ):
            self.assertEqual(
                ping.measure_rtt(
                    [first],
                    deadline=time.monotonic() + 1,
                ),
                {first: 9999},
            )

        caller_interruption = KeyboardInterrupt("caller interrupted ICMP map")
        with (
            mock.patch.object(
                self.pools.icmp,
                "ensure_started",
                return_value=None,
            ),
            mock.patch.object(
                self.pools.icmp,
                "map_until",
                side_effect=caller_interruption,
            ) as map_until,
        ):
            raised = _capture_error(
                lambda: ping.measure_rtt(
                    [interrupted],
                    deadline=time.monotonic() + 1,
                )
            )

        self.assertIs(raised, caller_interruption)
        map_until.assert_called_once()

    def test_unexpected_measurement_runtime_error_propagates_by_identity(self):
        operation_error = RuntimeError("RTT programming error")
        endpoint = Endpoint("programming-error.example", 1)
        with mock.patch.object(
            ping,
            "_measure_host",
            side_effect=operation_error,
        ):
            raised = _capture_error(
                lambda: ping.measure_rtt(
                    [endpoint],
                    deadline=time.monotonic() + 1,
                )
            )

        self.assertIs(raised, operation_error)

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_each_endpoint_gets_rounded_rtt_or_sentinel(self, ping_call):
        outcomes = {
            "one.example": Host("one.example", 1, [12.6]),
            "192.0.2.1": ICMPLibError("blocked"),
            "2001:db8::1": Host("2001:db8::1", 1, []),
        }

        def ping_host(host, **_options):
            outcome = outcomes[host]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        ping_call.side_effect = ping_host
        first = Endpoint("one.example", 1)
        same_host = Endpoint("one.example", 2)
        ipv4 = Endpoint("192.0.2.1", 3)
        ipv6 = Endpoint("2001:db8::1", 4)

        measured = ping.measure_rtt(
            (endpoint for endpoint in (first, same_host, ipv4, ipv6)),
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(
            measured,
            {first: 13, same_host: 13, ipv4: 9999, ipv6: 9999},
        )
        self.assertEqual(ping_call.call_count, 3)

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_one_host_failure_does_not_cancel_other_results(self, ping_call):
        outcomes = {
            "icmp.example": ICMPLibError("ICMP failure"),
            "os.example": OSError("socket failure"),
            "resolution.example": socket.gaierror("name lookup failure"),
            "value.example": ValueError("invalid address"),
            "type.example": TypeError("invalid host type"),
            "working.example": Host("working.example", 1, [8.8]),
        }

        def ping_host(host, **_options):
            outcome = outcomes[host]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        ping_call.side_effect = ping_host
        icmp = Endpoint("icmp.example", 1)
        os_error = Endpoint("os.example", 2)
        resolution = Endpoint("resolution.example", 3)
        value = Endpoint("value.example", 4)
        type_error = Endpoint("type.example", 5)
        working = Endpoint("working.example", 6)

        measured = ping.measure_rtt(
            [icmp, os_error, resolution, value, type_error, working],
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(
            measured,
            {
                icmp: 9999,
                os_error: 9999,
                resolution: 9999,
                value: 9999,
                type_error: 9999,
                working: 9,
            },
        )

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_icmp_request_is_single_bounded_and_unprivileged(self, ping_call):
        operation_threads = []

        def probe(host, **_options):
            operation_threads.append(threading.current_thread())
            return Host(host, 1, [4.6])

        ping_call.side_effect = probe
        endpoint = Endpoint("2001:db8::2", 27015)

        self.assertEqual(
            ping.measure_rtt(
                [endpoint],
                deadline=time.monotonic() + 1,
            ),
            {endpoint: 5},
        )
        ping_call.assert_called_once_with(
            "2001:db8::2",
            count=1,
            interval=0,
            timeout=1,
            privileged=False,
        )
        self.assertEqual(len(operation_threads), 1)
        self.assertIsNot(operation_threads[0], threading.current_thread())
        self.assertTrue(operation_threads[0].daemon)
        self.assertEqual(operation_threads[0].name, "obozrenie-icmp")
        self.assertEqual(self.pools.dns._workers, [])

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_empty_inputs_do_not_start_icmp_work(self, ping_call):
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError("empty RTT input started a pool"),
        ):
            self.assertEqual(ping.measure_rtt(iter(())), {})
            self.assertEqual(ping.add_rtt_info(iter(())), [])
        ping_call.assert_not_called()
        self.assertEqual(self.pools.icmp._workers, [])

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_40_fast_hosts_are_bounded_and_expand_duplicate_ports(self, ping_call):
        lock = threading.Lock()
        active = 0
        peak = 0
        call_counts = {}

        def fast_ping(host, **_options):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                call_counts[host] = call_counts.get(host, 0) + 1
            time.sleep(0.003)
            with lock:
                active -= 1
            return Host(host, 1, [1.0])

        ping_call.side_effect = fast_ping
        unique_endpoints = [
            Endpoint(f"host-{index}.example", index + 1)
            for index in range(40)
        ]
        endpoints = [
            unique_endpoints[0],
            unique_endpoints[1],
            Endpoint("host-0.example", 50000),
            *unique_endpoints[2:20],
            Endpoint("host-17.example", 50001),
            *unique_endpoints[20:],
        ]

        measured = ping.measure_rtt(
            endpoints,
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(list(measured), endpoints)
        self.assertEqual(len(measured), 42)
        self.assertEqual(set(measured.values()), {1})
        self.assertEqual(
            call_counts,
            {f"host-{index}.example": 1 for index in range(40)},
        )
        self.assertEqual(ping_call.call_count, 40)
        self.assertLessEqual(peak, 32)

    @mock.patch("obozrenie.ping.icmp_ping")
    def test_add_rtt_info_returns_exact_ordered_replacements(self, ping_call):
        ping_call.side_effect = lambda host, **_options: {
            "192.0.2.2": Host("192.0.2.2", 1, [17.7]),
            "2001:db8::2": Host("2001:db8::2", 1, [42.2]),
        }[host]
        first = Server(
            endpoint=Endpoint("192.0.2.2", 30000),
            game_id="minetest",
            name="First server",
            password=True,
            player_count=2,
            player_limit=20,
            ping=50,
            secure=True,
            country="DE",
            game_mod="mesecons",
            game_type="survival",
            terrain="Forest",
            players=(
                Player("Ada", score=7, ping=12),
                Player("Lin", score=-1, ping=30),
            ),
            rules={
                "mode": "survival",
                "ranked": True,
                "round": 3,
                "motd": None,
            },
        )
        second = Server(
            endpoint=Endpoint("2001:db8::2", 12000),
            game_id="rigsofrods",
            name="Second server",
            password=False,
            player_count=1,
            player_limit=8,
            ping=70,
            secure=False,
            country="CA",
            game_mod="vanilla",
            game_type="race",
            terrain="Desert",
            players=(Player("Grace", score=9, ping=20),),
            rules={"verified": 1, "official": False},
        )

        enriched = ping.add_rtt_info(
            (server for server in [first, second]),
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(
            enriched,
            [
                Server(
                    endpoint=Endpoint("192.0.2.2", 30000),
                    game_id="minetest",
                    name="First server",
                    password=True,
                    player_count=2,
                    player_limit=20,
                    ping=18,
                    secure=True,
                    country="DE",
                    game_mod="mesecons",
                    game_type="survival",
                    terrain="Forest",
                    players=(
                        Player("Ada", score=7, ping=12),
                        Player("Lin", score=-1, ping=30),
                    ),
                    rules={
                        "mode": "survival",
                        "ranked": True,
                        "round": 3,
                        "motd": None,
                    },
                ),
                Server(
                    endpoint=Endpoint("2001:db8::2", 12000),
                    game_id="rigsofrods",
                    name="Second server",
                    password=False,
                    player_count=1,
                    player_limit=8,
                    ping=42,
                    secure=False,
                    country="CA",
                    game_mod="vanilla",
                    game_type="race",
                    terrain="Desert",
                    players=(Player("Grace", score=9, ping=20),),
                    rules={"verified": 1, "official": False},
                ),
            ],
        )
        self.assertEqual((first.ping, second.ping), (50, 70))
        self.assertIsNot(enriched[0], first)
        self.assertIsNot(enriched[1], second)

    @mock.patch(
        "obozrenie.ping.icmp_ping",
        side_effect=PermissionError("ICMP unavailable"),
    )
    def test_capability_failure_returns_replacement_with_sentinel(self, _ping_call):
        server = Server(
            endpoint=Endpoint("example.org", 30000),
            game_id="g",
            country="CA",
        )

        self.assertEqual(
            ping.add_rtt_info(
                [server],
                deadline=time.monotonic() + 1,
            ),
            [
                Server(
                    endpoint=Endpoint("example.org", 30000),
                    game_id="g",
                    ping=9999,
                    country="CA",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
