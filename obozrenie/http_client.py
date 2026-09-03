import codecs
import contextlib
import json
import time
import urllib.parse
from dataclasses import dataclass
from enum import Enum, auto

import requests
import urllib3

from obozrenie import network_calls
from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    is_unambiguous_url_text,
)


_HTTP_OPERATION_TIMEOUT_SECONDS = 30.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class HttpFailureKind(Enum):
    TIMEOUT = auto()
    CONNECTION = auto()
    RESPONSE_INTEGRITY = auto()
    DECODE = auto()
    TLS = auto()
    INVALID_REQUEST = auto()
    # The HTTPS policy rejected the configured URL itself or a redirect;
    # the diagnostic says which.
    HTTPS_POLICY = auto()
    STATUS = auto()
    RESPONSE_TOO_LARGE = auto()
    REQUEST = auto()


@dataclass(frozen=True, slots=True)
class HttpFailure:
    kind: HttpFailureKind
    status_code: int | None
    diagnostic: SafeDiagnostic


class HttpsPolicyError(SafeDiagnosticError):
    pass


class ResponseTooLargeError(SafeDiagnosticError, ValueError):
    pass


class UnknownEncodingError(SafeDiagnosticError, ValueError):
    pass


class ResponseDecodeError(SafeDiagnosticError, ValueError):
    pass


def _unknown_encoding_error() -> UnknownEncodingError:
    """Build the one payload-free failure for an unusable response charset."""
    return UnknownEncodingError(
        SafeDiagnostic("HTTP response uses an unknown character encoding")
    )


def _response_decode_error() -> ResponseDecodeError:
    """Build the one payload-free failure for a body that will not decode."""
    return ResponseDecodeError(
        SafeDiagnostic("HTTP response decoding failed.")
    )


def validate_https_url(url: str) -> str:
    if is_unambiguous_url_text(url):
        try:
            parts = urllib.parse.urlsplit(url)
            hostname = parts.hostname
            parts.port
        except ValueError:
            pass
        else:
            if parts.scheme == "https" and hostname:
                return url
    raise HttpsPolicyError(
        SafeDiagnostic("HTTPS URL policy rejected the request.")
    )


@contextlib.contextmanager
def open_https_response(url: str, *, timeout, deadline: float):
    current_url = validate_https_url(url)
    with requests.Session() as session:
        for redirects_followed in range(_MAX_REDIRECTS + 1):
            response = session.get(
                current_url,
                allow_redirects=False,
                stream=True,
                timeout=_hop_timeout(timeout, deadline),
            )
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response.close()
                if not location or redirects_followed == _MAX_REDIRECTS:
                    raise HttpsPolicyError(
                        SafeDiagnostic(
                            "HTTPS redirect policy rejected the response."
                        )
                    )
                current_url = validate_https_url(
                    urllib.parse.urljoin(current_url, location)
                )
                continue
            if 300 <= response.status_code < 400:
                response.close()
                raise HttpsPolicyError(
                    SafeDiagnostic(
                        "HTTPS redirect policy rejected the response."
                    )
                )
            with response:
                response.raise_for_status()
                yield response
                return


def iter_response_body(response, chunk_size: int):
    """Yield one response body in single transport reads.

    `Response.iter_content` fills a whole chunk before it yields, so a
    caller that checks its own limits between chunks reaches no check at all
    while a peer trickles bytes: every read wait is satisfied and the chunk
    never completes. `raw.read1` returns as soon as one transport read has
    delivered something, which is what puts those checks back within one
    read interval. Reading the raw stream also means owning the translation
    Requests performs inside `iter_content`, so the transport's own failures
    still arrive as the families `describe_http_failure` classifies rather
    than as unexpected exceptions. A response whose raw stream offers no
    `read1` keeps the buffered read.
    """
    read1 = getattr(getattr(response, "raw", None), "read1", None)
    if read1 is None:
        yield from response.iter_content(chunk_size=chunk_size)
        return
    while True:
        try:
            # The response is opened undecoded, so the decoding
            # `iter_content` asks for has to be asked for here too.
            chunk = read1(chunk_size, decode_content=True)
        except urllib3.exceptions.ProtocolError as error:
            raise requests.exceptions.ChunkedEncodingError(error) from error
        except urllib3.exceptions.DecodeError as error:
            raise requests.exceptions.ContentDecodingError(error) from error
        except urllib3.exceptions.ReadTimeoutError as error:
            raise requests.exceptions.ConnectionError(error) from error
        except urllib3.exceptions.SSLError as error:
            raise requests.exceptions.SSLError(error) from error
        except urllib3.exceptions.HTTPError as error:
            # Whatever else the transport raises is still the transport's
            # failure, so it reaches the classifier as one.
            raise requests.exceptions.RequestException(error) from error
        if not chunk:
            return
        yield chunk


def shutdown_reads(response) -> bool:
    """Interrupt a read another thread is blocked in; report whether it took.

    `raw.shutdown()` shuts the connection's read side down, so a blocked
    read returns end of file at once. It is the only interrupt that leaves
    the buffer an in-progress read holds alone: closing the response closes
    that buffer first, and CPython's buffered close waits for the very lock
    that read owns — exactly what a cancelling caller must never do. A
    response with no read side left to shut down (already closed, its
    connection already back in the pool, or a transport that exposes none)
    reports False, so the caller can fall back to closing it.
    """
    shutdown = getattr(getattr(response, "raw", None), "shutdown", None)
    if shutdown is None:
        return False
    try:
        shutdown()
    except Exception:
        # Interrupting another thread's read is best effort: urllib3
        # refuses when there is no socket (`ValueError`) and when the
        # connection has gone back to the pool (`RuntimeError`), and
        # neither is a failure of the caller that asked for the interrupt.
        return False
    return True


def describe_http_failure(error: BaseException) -> HttpFailure | None:
    # Pool abandonment, saturation and unavailability describe this process,
    # not the endpoint, so they are never an HTTP failure even when a class
    # also inherits from a Requests family.
    if isinstance(
        error,
        (
            network_calls.CallDeadlineExceeded,
            network_calls.PoolSaturatedError,
            network_calls.PoolUnavailableError,
        ),
    ):
        return None
    if isinstance(error, HttpsPolicyError):
        return HttpFailure(
            HttpFailureKind.HTTPS_POLICY,
            None,
            error.diagnostic,
        )
    # The decode phase declares its own failure, so it is recognized before
    # every other `ValueError` family this classifier owns.
    if isinstance(error, ResponseDecodeError):
        return HttpFailure(HttpFailureKind.DECODE, None, error.diagnostic)
    if isinstance(error, ResponseTooLargeError):
        return HttpFailure(
            HttpFailureKind.RESPONSE_TOO_LARGE,
            None,
            error.diagnostic,
        )
    if isinstance(error, UnknownEncodingError):
        return HttpFailure(HttpFailureKind.DECODE, None, error.diagnostic)
    if isinstance(error, requests.exceptions.SSLError):
        return HttpFailure(
            HttpFailureKind.TLS,
            None,
            SafeDiagnostic("HTTPS TLS connection failed."),
        )
    if isinstance(error, requests.Timeout):
        return HttpFailure(
            HttpFailureKind.TIMEOUT,
            None,
            SafeDiagnostic("HTTP request timed out."),
        )
    if isinstance(error, requests.ConnectionError):
        return HttpFailure(
            HttpFailureKind.CONNECTION,
            None,
            SafeDiagnostic("HTTP connection failed."),
        )
    if isinstance(
        error,
        (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ),
    ):
        return HttpFailure(
            HttpFailureKind.RESPONSE_INTEGRITY,
            None,
            SafeDiagnostic("HTTP response integrity validation failed."),
        )
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return HttpFailure(
            HttpFailureKind.DECODE,
            None,
            SafeDiagnostic("HTTP response decoding failed."),
        )
    if isinstance(
        error,
        (
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidHeader,
        ),
    ):
        return HttpFailure(
            HttpFailureKind.INVALID_REQUEST,
            None,
            SafeDiagnostic("HTTP request configuration was invalid."),
        )
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status_code = (
            None if response is None else getattr(response, "status_code", None)
        )
        return HttpFailure(
            HttpFailureKind.STATUS,
            status_code,
            SafeDiagnostic("HTTP response returned an error status."),
        )
    if isinstance(error, requests.RequestException):
        return HttpFailure(
            HttpFailureKind.REQUEST,
            None,
            SafeDiagnostic("HTTP request failed."),
        )
    return None


def _deadline_exceeded() -> requests.Timeout:
    """Build the failure every HTTP caller sees when the deadline passes."""
    return requests.Timeout("HTTP operation deadline exceeded.")


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _deadline_exceeded()


def _hop_timeout(timeout, deadline: float):
    """Bound one hop's blocking waits by the time the operation has left.

    No connect or read wait may outlast the operation deadline, so every
    supplied component is clamped to the time remaining and an absent
    component becomes exactly that remainder.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _deadline_exceeded()
    if isinstance(timeout, tuple):
        return tuple(
            _clamped_wait(component, remaining) for component in timeout
        )
    return _clamped_wait(timeout, remaining)


def _clamped_wait(wait, remaining: float):
    return remaining if wait is None else min(wait, remaining)


def _get_json(url, timeout, max_bytes, deadline):
    with open_https_response(
        url,
        timeout=timeout,
        deadline=deadline,
    ) as response:
        _check_deadline(deadline)
        body = bytearray()
        # One transport read at a time, so the deadline and size checks
        # below are reached within one read interval however slowly the
        # peer sends, instead of once per completed chunk.
        for chunk in iter_response_body(response, 64 * 1024):
            _check_deadline(deadline)
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ResponseTooLargeError(
                    SafeDiagnostic(
                        f"HTTP response exceeds {max_bytes} bytes"
                    )
                )
        _check_deadline(deadline)
        # Resolving the codec first keeps every way of naming no codec at all
        # (unknown name, embedded NUL) one classified failure, and leaves a
        # genuine UnicodeDecodeError from the decode itself untouched.
        try:
            codec = codecs.lookup(response.encoding or "utf-8")
        except (LookupError, ValueError):
            raise _unknown_encoding_error() from None
        try:
            # A binary transform such as base64 resolves as a codec and is
            # rejected only here, with a bare LookupError.
            text = body.decode(codec.name)
        except LookupError:
            raise _unknown_encoding_error() from None
        except UnicodeError:
            # This phase owns every way a body refuses to become text, not
            # only `UnicodeDecodeError`: `idna` and `punycode` raise the bare
            # base class before CPython 3.14. An unclassified failure here
            # would escape a caller's per-source isolation, so one master's
            # charset could suppress every other master's results.
            raise _response_decode_error() from None
        _check_deadline(deadline)
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            # `json.loads` raises more than `JSONDecodeError` on bodies a
            # peer chooses: a 5,000-digit integer exceeds CPython's integer
            # string-digit limit as a bare `ValueError`, and deep nesting
            # exhausts the parser as a `RecursionError`. Both are ordinary
            # exceptions this phase classifies rather than letting through.
            raise _response_decode_error() from None


def get_json(
    url: str,
    *,
    timeout=(5, 15),
    max_bytes=16 * 1024 * 1024,
    deadline: float | None = None,
) -> object:
    url = validate_https_url(url)
    if deadline is None:
        deadline = time.monotonic() + _HTTP_OPERATION_TIMEOUT_SECONDS
    deadline = network_calls.validate_deadline(deadline)
    _check_deadline(deadline)

    pool = network_calls.get_network_call_pools().http
    try:
        pool.ensure_started()
    except BaseException:
        raise requests.ConnectionError(
            "HTTP network call pool is unavailable."
        ) from None

    try:
        return pool.call_until(
            deadline,
            _get_json,
            url,
            timeout,
            max_bytes,
            deadline,
        )
    except (
        network_calls.CallDeadlineExceeded,
        network_calls.PoolSaturatedError,
    ) as error:
        if type(error) not in (
            network_calls.CallDeadlineExceeded,
            network_calls.PoolSaturatedError,
        ):
            raise
        raise requests.Timeout("HTTP operation deadline exceeded.") from None
    except network_calls.PoolUnavailableError as error:
        if type(error) is not network_calls.PoolUnavailableError:
            raise
        raise requests.ConnectionError(
            "HTTP network call pool is unavailable."
        ) from None
