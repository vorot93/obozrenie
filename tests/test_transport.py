"""Suites that run the shipped HTTP transport over one fake socket.

Every other suite replaces the response or the session; these replace only
the socket, so `requests`, `urllib3` and `http.client` are the shipped
classes — the ones whose buffered reads, buffer locks, chunked framing and
content decoding decide what a deadline, a cancellation or a byte-granular
read can do. The runtime suite is run by discovery from its own directory,
so this module is self-contained: the peer fixtures live here and nothing
imports across test modules.
"""

import contextlib
import gzip
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests
import urllib3

from obozrenie import geoip, helpers, http_client, network_calls
from obozrenie.diagnostics import SafeDiagnostic


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


def _header(declared, *extra_lines):
    """Build one 200 response header declaring `declared` body bytes."""
    lines = ["HTTP/1.1 200 OK", f"Content-Length: {declared}", *extra_lines]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class FakePeer:
    """Stand in for one connected socket, without opening one.

    A peer hands its prepared wire response out one byte per read, so
    `http.client` and `urllib3` perform exactly the byte-granular reads a
    trickling server provokes: one read per header byte, and a body read
    that can return as soon as a single byte has arrived. Once the prepared
    bytes run out the peer stalls — neither closed nor sending — until its
    read side is shut down or its own bounded stall passes, which is what a
    silent connection looks like from above.
    """

    def __init__(self, wire, *, interval=0.0, slow_from=0, stall=1.0):
        self.wire = bytes(wire)
        self.interval = interval
        self.stall = stall
        self.reads = 0
        self.closes = 0
        self.closed = False
        self.shut = threading.Event()
        self.body_started = threading.Event()
        self.file_closed = threading.Event()
        self._slow_from = slow_from
        self._served = 0

    # The part of the socket protocol `http.client` and `urllib3` use.
    def makefile(self, mode="rb", buffering=None):
        return io.BufferedReader(PeerReader(self))

    def settimeout(self, value):
        """Accept the timeout urllib3 sets; this peer bounds itself."""

    def shutdown(self, how):
        """Shut this peer's read side down, exactly as `SHUT_RD` does."""
        self.shut.set()

    def close(self):
        self.closes += 1
        self.closed = True
        self.shut.set()

    def serve(self) -> bytes:
        """Return one byte, end of file, or block as a stalled peer does."""
        self.reads += 1
        if self.shut.is_set():
            return b""
        if self._served >= len(self.wire):
            self.shut.wait(self.stall)
            return b""
        if self._served >= self._slow_from:
            self.body_started.set()
            if self.interval and self.shut.wait(self.interval):
                return b""
        served = self.wire[self._served:self._served + 1]
        self._served += 1
        return served


class PeerReader(io.RawIOBase):
    """The connection file `http.client` reads one peer through."""

    def __init__(self, peer):
        super().__init__()
        self._peer = peer

    def readable(self):
        return True

    def close(self):
        self._peer.file_closed.set()
        super().close()

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("read of a closed connection file")
        if self._peer.closed:
            raise OSError("read from a closed socket")
        served = self._peer.serve()
        if not served:
            return 0
        buffer[0] = served[0]
        return 1


class FakePeerAdapter(requests.adapters.HTTPAdapter):
    """Serve one peer through the transport classes Requests really uses.

    Everything below this adapter is the shipped stack: `http.client` parses
    the status line and the headers, `urllib3` owns the body reads and the
    read-side shutdown, and `requests` wraps both. Only the socket is a
    fake, so no port is ever opened and no timing depends on one.
    """

    def __init__(self, peer):
        super().__init__()
        self._peer = peer

    def send(self, request, **_options):
        peer = self._peer
        parsed = http.client.HTTPResponse(
            peer,
            method=request.method,
            url=request.url,
        )
        # Reading the status line and the headers happens here, inside the
        # send a caller waits for, exactly as it does over a real socket.
        parsed.begin()
        raw = urllib3.HTTPResponse(
            body=parsed,
            headers=urllib3.HTTPHeaderDict(parsed.msg.items()),
            status=parsed.status,
            version=parsed.version,
            reason=parsed.reason,
            preload_content=False,
            decode_content=False,
            original_response=parsed,
            enforce_content_length=True,
            request_method=request.method,
            request_url=request.url,
            connection=peer,
            sock_shutdown=peer.shutdown,
        )
        return self.build_response(request, raw)


class RealTransportDownloadTests(unittest.TestCase):
    """The transfer's bounds, proved against the stack that enforces them.

    `DatabaseDownloadTests` replaces the response; these tests replace only
    the socket, so `requests`, `urllib3` and `http.client` are the shipped
    classes — the ones whose buffered reads and buffer locks decide whether
    a deadline or a cancellation can end a read at all.
    """

    URL = "https://peer.invalid/dbip-country.mmdb"

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.destination = os.path.join(self.directory, "dbip-country.mmdb")
        self.debug_msg = self.enterContext(
            mock.patch.object(helpers, "debug_msg")
        )

    def serve(self, peer):
        """Answer every HTTPS request of this test from one fake peer."""
        session_class = requests.Session

        def open_session():
            session = session_class()
            session.mount("https://", FakePeerAdapter(peer))
            return session

        self.enterContext(
            mock.patch.object(http_client.requests, "Session", open_session)
        )
        return peer

    def transfer(self, seconds, **options):
        return geoip.GeoIPDatabaseDownload(
            self.destination,
            time.monotonic() + seconds,
            url=self.URL,
            **options,
        )

    def stub_validation(self):
        return self.enterContext(
            mock.patch.object(geoip, "_validate_database")
        )

    def entries(self):
        return sorted(os.listdir(self.directory))

    def assert_reported_the_deadline(self):
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP download exceeded its time limit."),
                ])
            ],
        )

    def test_trickling_body_cannot_outlast_the_absolute_deadline(self):
        # One byte every 10 ms satisfies every read wait without ever
        # completing a 64 KiB buffered chunk, so a transfer that reads whole
        # chunks reaches no check of its own and ends only when this peer
        # stops — three seconds past a deadline of four tenths of one.
        validate = self.stub_validation()
        header = _header(100000)
        peer = self.serve(
            FakePeer(
                header + b"x" * 200,
                interval=0.01,
                slow_from=len(header),
            )
        )
        transfer = self.transfer(0.4)

        started = time.monotonic()
        result = transfer.run()
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertTrue(peer.body_started.is_set())
        self.assertLess(elapsed, 0.9)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assert_reported_the_deadline()

    def test_cancel_returns_without_waiting_for_the_blocked_reader(self):
        # `Response.close()` closes the buffered reader first, and CPython's
        # buffered close takes the very lock the worker's read holds, so a
        # cancellation that closes would wait out that read on the caller's
        # own thread — here the thread pressing "Not now".
        validate = self.stub_validation()
        header = _header(100000)
        peer = self.serve(
            FakePeer(
                header + b"x" * 40,
                interval=0.05,
                slow_from=len(header),
            )
        )
        transfer = self.transfer(30.0)
        outcome = []

        worker = threading.Thread(
            target=lambda: outcome.append(transfer.run()),
            name="geoip-transfer",
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(peer.body_started.wait(1.0))
            started = time.monotonic()
            transfer.cancel()
            returned_in = time.monotonic() - started
            worker.join(1.0)
            stopped_in = time.monotonic() - started
        finally:
            peer.shut.set()
            worker.join(1.0)

        self.assertLess(returned_in, 0.1)
        self.assertFalse(worker.is_alive())
        self.assertLess(stopped_in, 0.5)
        self.assertEqual(outcome, [None])
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.debug_msg.assert_not_called()

    def test_trickling_header_phase_cannot_outlast_the_deadline(self):
        # A peer that trickles its status line holds whichever thread
        # entered the HTTP boundary, and there is no response to interrupt
        # yet, so only a worker that can abandon that thread is bounded.
        validate = self.stub_validation()
        peer = self.serve(FakePeer(_header(4) + b"body", interval=0.05))
        transfer = self.transfer(0.3)

        started = time.monotonic()
        result = transfer.run()
        elapsed = time.monotonic() - started
        # The abandoned helper finishes the hop it began; it must close what
        # it receives and install nothing.
        peer.interval = 0.0

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.9)
        self.assertTrue(peer.file_closed.wait(1.0))
        self.assertGreaterEqual(peer.closes, 1)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assert_reported_the_deadline()

    def test_byte_granular_reads_reassemble_one_decoded_body(self):
        validated = []
        self.enterContext(
            mock.patch.object(geoip, "_validate_database", validated.append)
        )
        body = b"mmdb-bytes-" * 50
        compressed = gzip.compress(body)
        self.serve(
            FakePeer(
                _header(len(compressed), "Content-Encoding: gzip")
                + compressed
            )
        )
        transfer = self.transfer(5.0)
        progress = []

        result = transfer.run(
            lambda downloaded, total: progress.append((downloaded, total))
        )

        self.assertEqual(result, self.destination)
        [validated_path] = validated
        self.assertNotEqual(validated_path, self.destination)
        with open(self.destination, "rb") as handle:
            self.assertEqual(handle.read(), body)
        self.assertEqual(progress[-1], (len(body), len(compressed)))
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.debug_msg.assert_not_called()


class RealTransportJsonTests(unittest.TestCase):
    """`get_json` over the shipped transport, with only the socket faked.

    `_get_json` reads the body through `raw.read1`, so chunked framing,
    trailers and content decoding are `http.client`'s and urllib3's work
    exactly as they were under `iter_content`. These cases prove the decoded
    payload is the same one, delivered one transport read at a time, and
    that a body the peer cuts short is still an integrity failure.
    """

    URL = "https://peer.invalid/servers"
    PAYLOAD = json.dumps(
        {"servers": [{"index": index} for index in range(400)]}
    ).encode("ascii")

    def setUp(self):
        self.enterContext(installed_network_pools())

    @staticmethod
    def wire(*header_lines, body):
        headers = b"".join(
            line + b"\r\n"
            for line in (b"Content-Type: application/json", *header_lines)
        )
        return b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body

    @staticmethod
    def chunked(body, size):
        framed = b"".join(
            f"{len(body[start:start + size]):x}\r\n".encode("ascii")
            + body[start:start + size]
            + b"\r\n"
            for start in range(0, len(body), size)
        )
        return framed + b"0\r\n\r\n"

    def get_json_from(self, wire):
        """Serve `wire` from one fake peer through the owned session.

        The buffered path is patched out, so whatever comes back was read
        through `raw.read1` alone.
        """
        peer = FakePeer(wire, stall=0.2)
        session_class = requests.Session

        def open_session():
            session = session_class()
            session.mount("https://", FakePeerAdapter(peer))
            return session

        with (
            mock.patch.object(
                http_client.requests, "Session", open_session
            ),
            mock.patch.object(
                requests.Response,
                "iter_content",
                side_effect=AssertionError("body read through iter_content"),
            ),
        ):
            return http_client.get_json(self.URL), peer

    def test_bodies_decode_the_same_one_transport_read_at_a_time(self):
        compressed = gzip.compress(self.PAYLOAD)
        cases = {
            "content-length": self.wire(
                b"Content-Length: %d" % len(self.PAYLOAD),
                body=self.PAYLOAD,
            ),
            "chunked": self.wire(
                b"Transfer-Encoding: chunked",
                body=self.chunked(self.PAYLOAD, 500),
            ),
            "chunked with trailer": self.wire(
                b"Transfer-Encoding: chunked",
                body=self.chunked(self.PAYLOAD, 500)[:-2]
                + b"X-Trailer: 1\r\n\r\n",
            ),
            "gzip": self.wire(
                b"Content-Length: %d" % len(compressed),
                b"Content-Encoding: gzip",
                body=compressed,
            ),
            "chunked gzip": self.wire(
                b"Transfer-Encoding: chunked",
                b"Content-Encoding: gzip",
                body=self.chunked(compressed, 77),
            ),
        }
        for label, wire in cases.items():
            with self.subTest(body=label):
                result, _peer = self.get_json_from(wire)

                self.assertEqual(result, json.loads(self.PAYLOAD))

    def test_a_body_the_peer_cuts_short_is_an_integrity_failure(self):
        cases = {
            "content-length": self.wire(
                b"Content-Length: %d" % (len(self.PAYLOAD) + 10),
                body=self.PAYLOAD,
            ),
            "chunked": self.wire(
                b"Transfer-Encoding: chunked",
                body=self.chunked(self.PAYLOAD, 500)[:-5],
            ),
        }
        for label, wire in cases.items():
            with self.subTest(body=label):
                with self.assertRaises(
                    requests.exceptions.ChunkedEncodingError
                ) as raised:
                    self.get_json_from(wire)

                self.assertIs(
                    type(raised.exception),
                    requests.exceptions.ChunkedEncodingError,
                )
                failure = http_client.describe_http_failure(raised.exception)
                self.assertIs(
                    failure.kind, http_client.HttpFailureKind.RESPONSE_INTEGRITY
                )


if __name__ == "__main__":
    unittest.main()
