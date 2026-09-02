#!/usr/bin/env python3
# This source file is part of Obozrenie
# Copyright 2015 Artem Vorotnikov

# For more information, see https://github.com/obozrenie/obozrenie

# Obozrenie is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.

# Obozrenie is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Obozrenie.  If not, see <http://www.gnu.org/licenses/>.

"""GeoIP database discovery and download for Obozrenie."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from collections.abc import Callable

import maxminddb
import requests
from xdg import BaseDirectory

from obozrenie import helpers, http_client
from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    diagnostic_for_exception,
)
from obozrenie.global_strings import CORECAT_MSG
from obozrenie.network_calls import validate_deadline

DOWNLOAD_URL = "https://cdn.jsdelivr.net/npm/@ip-location-db/dbip-geo-whois-asn-country-mmdb/dbip-geo-whois-asn-country.mmdb"

KNOWN_SYSTEM_PATHS = [
    "/usr/share/GeoIP/dbip-country.mmdb",
    "/usr/share/GeoIP/dbip-country-lite.mmdb",
    "/usr/share/GeoIP/GeoLite2-Country.mmdb",
    "/var/lib/GeoIP/GeoLite2-Country.mmdb",
]

CACHE_DB_PATH = os.path.join(
    BaseDirectory.xdg_cache_home, "obozrenie", "dbip-country.mmdb")

# One transfer may occupy its worker for this long in total, however many
# redirects, reads and retries the transport performs inside it, and however
# slowly the peer sends: the body is read one transport read at a time, a
# watchdog shuts the read side down the moment this limit passes, and the
# phase before the first response exists runs on a helper the worker
# abandons at the same limit. A caller's own deadline can only shorten it.
DOWNLOAD_TIMEOUT_SECONDS = 120.0
# Both the declared and the streamed body are rejected above this size.
DOWNLOAD_MAX_BYTES = 128 * 1024 * 1024
# No blocking read waits longer than this or the time the operation has left,
# whichever is smaller, so a silent peer cannot hold a worker to the deadline.
MAX_READ_TIMEOUT_SECONDS = 15.0

_CONNECT_TIMEOUT_SECONDS = 5.0
_CHUNK_SIZE = 65536
_GEOIP_MSG = CORECAT_MSG + "GeoIP"
_OPENER_THREAD_NAME = "obozrenie-geoip-open"
_CLOSER_THREAD_NAME = "obozrenie-geoip-close"


class _TransferHelperError(SafeDiagnosticError, RuntimeError):
    """A helper a transfer's bounds depend on could not do its work."""


# Every way a transfer can fail without a programming error: transport and
# policy failures from the HTTP boundary, filesystem failures from the
# temporary file, a body that is not a database, and a process that cannot
# spare the threads the bounds are built on.
_TRANSFER_FAILURES = (
    requests.RequestException,
    http_client.HttpsPolicyError,
    maxminddb.InvalidDatabaseError,
    OSError,
    _TransferHelperError,
    # Covers the transfer's own size rejection and the errors a response read
    # raises once cancellation or the deadline watchdog has ended the read
    # under it.
    ValueError,
)

# The one reason a transfer reports when its own deadline ended it.
_DEADLINE_EXCEEDED = SafeDiagnostic("GeoIP download exceeded its time limit.")
# The one reason a transfer reports when this process has no thread to spare.
_NO_HELPER_THREAD = SafeDiagnostic(
    "GeoIP download could not start a helper thread."
)
# The one reason a transfer reports when the helper that opens its response
# ends without producing one.
_OPEN_UNFINISHED = SafeDiagnostic(
    "GeoIP download could not open its response."
)


def find_database():
    """Return the first readable database path, or None.

    Probes the known system locations in order, then the downloaded
    cache copy. First match wins.
    """
    for path in KNOWN_SYSTEM_PATHS + [CACHE_DB_PATH]:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _close_quiet(response) -> None:
    """Close one response from any thread, tolerating a failing close."""
    try:
        response.close()
    except Exception:
        # Closing is part of how every transfer ends; it is never allowed to
        # raise into whichever thread happens to be doing it.
        pass


def _exit_quiet(context) -> None:
    """Leave one entered HTTP context, tolerating a failing exit.

    Exiting is what closes the response and the session behind it. A
    generator context manager resumes on whichever thread calls `__exit__`,
    so a context entered by the opening helper is left here, on the thread
    that ended up owning it.
    """
    try:
        context.__exit__(None, None, None)
    except Exception:
        pass


def _close_detached(response) -> None:
    """Close one response on a helper thread, never on the caller's own.

    Closing reaches `io.BufferedReader.close()`, which waits for the buffer
    lock an in-progress read holds, so closing on the thread that asked for
    the interrupt would hold that thread for as long as that read runs — the
    GTK main thread among them. Only a response whose read side cannot be
    shut down needs this, and the daemon that does it touches nothing but
    the response it was handed.
    """
    closer = threading.Thread(
        target=_close_quiet,
        args=(response,),
        name=_CLOSER_THREAD_NAME,
        daemon=True,
    )
    try:
        closer.start()
    except RuntimeError:
        # Without a thread to spare the response stays open until its own
        # reader leaves the context, a wait the read timeout already bounds.
        pass


def _release(opener, response) -> None:
    """End whatever a worker is waiting on, without waiting for it to end."""
    if opener is not None:
        opener.wake()
    if response is not None and not http_client.shutdown_reads(response):
        _close_detached(response)


def _start_daemon(thread: threading.Thread) -> None:
    """Start one daemon thread, or fail the transfer that depends on it."""
    thread.daemon = True
    try:
        thread.start()
    except RuntimeError:
        # A process out of threads cannot give this transfer the bound it
        # promises, so the transfer ends as the failure it is rather than as
        # an unexpected exception in a worker nobody reports to.
        raise _TransferHelperError(_NO_HELPER_THREAD) from None


def _declared_length(response) -> int:
    """Return the body length the response declares, or 0 when unusable.

    An absent, malformed, or negative `Content-Length` simply leaves the size
    unknown, exactly as an unlabelled body does; the streamed byte count still
    bounds the transfer.
    """
    try:
        declared = int(response.headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(declared, 0)


def _too_large(max_bytes: int) -> http_client.ResponseTooLargeError:
    return http_client.ResponseTooLargeError(
        SafeDiagnostic(f"GeoIP database exceeds {max_bytes} bytes")
    )


def _validate_database(path: str) -> None:
    """Prove one file is a readable MaxMind database, or raise.

    Opening the file and reading its metadata is what rejects a truncated or
    non-mmdb body, before anything is installed.
    """
    database = maxminddb.open_database(path)
    try:
        database.metadata()
    finally:
        database.close()


class _ResponseOpener:
    """Open one terminal response on a daemon the worker may abandon.

    A hop's connect, status-line and header phases block on the same
    byte-granular reads its body does, so a peer that trickles a header
    holds whichever thread entered the HTTP boundary — and no watchdog can
    reach it, because there is no response to interrupt yet. The helper
    enters the boundary instead. A worker takes the outcome only until its
    own deadline; past it the worker abandons this opener and returns, and
    the helper closes whatever it opens afterwards. An abandoned helper
    writes no file, installs nothing and holds nothing but its own daemon
    thread, so it can never delay a later offer — though against a peer that
    keeps trickling it may outlive the transfer that started it.

    The entered context is handed over rather than the response alone: a
    generator context manager resumes on whichever thread calls `__exit__`,
    so a claimed response is closed by the worker exactly where it always
    was, and a helper whose response is claimed exits at once.
    """

    def __init__(self, url: str, timeout, deadline: float) -> None:
        self._url = url
        self._timeout = timeout
        self._deadline = deadline
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._settled = False
        self._abandoned = False
        self._opened = None
        self._error = None
        self._thread = threading.Thread(
            target=self._open,
            name=_OPENER_THREAD_NAME,
        )

    def start(self) -> None:
        """Begin opening on this opener's own daemon helper thread."""
        _start_daemon(self._thread)

    def claim(self, timeout: float):
        """Take the opened context and response, or abandon this opener.

        Returns the entered context together with its response, raises
        whatever the open raised, or returns None when the wait ran out or
        was ended from outside. A wait that ends without an outcome abandons
        the helper for good, so a response settled afterwards is closed by
        the helper rather than left open behind everyone's back.
        """
        self._done.wait(max(0.0, timeout))
        with self._lock:
            if not self._settled:
                self._abandoned = True
                return None
            opened, error = self._opened, self._error
        if error is not None:
            raise error
        return opened

    def wake(self) -> None:
        """End a worker's wait for this opener without waiting for it."""
        self._done.set()

    def _open(self) -> None:
        """Enter the HTTP boundary and settle this open's one outcome."""
        opened = None
        error = None
        try:
            try:
                context = http_client.open_https_response(
                    self._url,
                    timeout=self._timeout,
                    deadline=self._deadline,
                )
                opened = (context, context.__enter__())
            except Exception as failure:
                # Whatever the open raised is re-raised verbatim on the
                # thread that claims it, so the classification a transfer
                # failure gets is the one it would have got in place.
                error = failure
            if not self._settle(opened, error) and opened is not None:
                # Nobody will read this response: the transfer ended while
                # the peer was still sending its header. Leaving the context
                # closes the response and the session it was opened on.
                _exit_quiet(opened[0])
        finally:
            # A helper that died before it could settle would otherwise hold
            # its worker waiting for the whole deadline over nothing.
            self._settle(None, _TransferHelperError(_OPEN_UNFINISHED))

    def _settle(self, opened, error) -> bool:
        """Record this open's outcome; report whether a worker will take it."""
        with self._lock:
            if self._abandoned or self._settled:
                return False
            self._opened = opened
            self._error = error
            self._settled = True
        self._done.set()
        return opened is not None


class GeoIPDatabaseDownload:
    """One owned, cancellable, bounded transfer of the country database.

    A transfer streams into its own private same-directory temporary file and
    publishes it with one atomic replacement, so a partial or rejected body is
    never visible and two transfers can never share a file. Its bounds are
    absolute: one operation deadline covers the whole transfer, every blocking
    read waits at most `MAX_READ_TIMEOUT_SECONDS` or the time that deadline
    leaves, and both the declared and the streamed body length are rejected
    above `max_bytes`.

    A peer that trickles bytes is what those bounds are built against, because
    it satisfies every read wait without ever finishing anything. Three things
    hold the deadline against it. The body arrives one transport read at a
    time, so the check before every write is reached within one read interval
    instead of once per completed chunk. A daemon watchdog armed for the
    remaining time shuts the read side down from outside the worker the moment
    the deadline passes, which returns a blocked read at once rather than
    waiting for a chunk, a timeout, or the peer. And the phase before a
    response exists — connect, status line, headers, all equally byte-granular
    and with nothing to interrupt yet — runs on a daemon helper the worker
    abandons when its deadline passes. So `run()` returns by the deadline plus
    the time one interrupted read needs to return; nothing that arrives after
    the deadline is ever processed, and an abandoned helper closes whatever it
    eventually opens, installs nothing, and touches no file.

    `cancel()` belongs to another thread and never blocks it: it stops the
    transfer at its next checkpoint, ends the worker's wait for a response
    that has not arrived, and shuts down the read side of one that has, all
    without waiting for the reader. Closing stays with the thread that owns
    the response. Both cancellation and deadline expiry are checked before
    every write, before validation, and again between validation and the
    replacement, so a stopped transfer installs nothing it has not validated.
    Once that replacement has succeeded the database is installed, and the
    transfer reports its path even to a caller that has already cancelled:
    whether to load it is that caller's own decision, taken at its own
    checkpoint.
    """

    def __init__(
        self,
        destination: str,
        deadline: float,
        *,
        url: str = DOWNLOAD_URL,
        max_bytes: int = DOWNLOAD_MAX_BYTES,
    ) -> None:
        # The operation limit is the transfer's own, so a caller's deadline can
        # only shorten it. Validating first means a malformed deadline fails
        # before any file or connection exists.
        self._deadline = min(
            validate_deadline(deadline),
            time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS,
        )
        self._destination = os.fspath(destination)
        self._url = url
        self._max_bytes = max_bytes
        self._cancelled = threading.Event()
        self._expired = threading.Event()
        self._lock = threading.Lock()
        self._opener = None
        self._response = None
        self._watchdog = None

    @property
    def deadline(self) -> float:
        """Return the absolute monotonic time this transfer may not outlast."""
        return self._deadline

    def cancel(self) -> None:
        """Stop this transfer, releasing but never joining its worker.

        Callers are main threads — the offer dialog's "Not now" and the
        application's own close — so this returns in the time an event set
        and a socket shutdown take, whatever the worker is doing.
        """
        with self._lock:
            # The flag is set before the release, so the worker always sees
            # its own cancellation before the failure that release provokes.
            self._cancelled.set()
            opener, response = self._opener, self._response
        _release(opener, response)

    def run(
        self,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> str | None:
        """Transfer the database and return its installed path, or None.

        `progress_cb` receives the bytes written so far and the declared total,
        which is 0 while the response declares no length. A cancelled, expired,
        oversized, malformed, or failed transfer returns None having removed
        its own temporary file and touched no other file, including any
        database already installed, and logs exactly one payload-free reason
        for it — none at all when the caller cancelled, since that reason is
        the caller's own. The one exception is a cancellation that arrives
        after the atomic replacement: the validated database is installed by
        then, and its path is returned rather than hidden.
        """
        if self._stopped():
            self._report_stop()
            return None

        descriptor = None
        temporary_path = None
        try:
            directory = os.path.dirname(self._destination) or "."
            os.makedirs(directory, exist_ok=True)
            # A unique private sibling: unique so concurrent transfers cannot
            # corrupt each other, private so a half-written database is never
            # world-readable, and a sibling so the replacement is atomic.
            descriptor, temporary_path = tempfile.mkstemp(
                dir=directory,
                prefix=f".{os.path.basename(self._destination)}.",
                suffix=".part",
            )
            os.fchmod(descriptor, 0o600)
            body = os.fdopen(descriptor, "wb")
            descriptor = None
            with body:
                if not self._stream_into(body, progress_cb):
                    self._report_stop()
                    return None
                # The replacement publishes this file under the name the
                # application loads on the next start, so its bytes reach the
                # disk first. A slow flush only delays the checks below, which
                # still refuse to install anything once the deadline has gone.
                body.flush()
                os.fsync(body.fileno())

            if self._stopped():
                self._report_stop()
                return None
            _validate_database(temporary_path)
            # This one check closes both windows the replacement opens: it
            # follows validation and immediately precedes `os.replace`.
            if self._stopped():
                self._report_stop()
                return None
            os.replace(temporary_path, self._destination)
            # The database is installed under its own name, so this transfer no
            # longer owns a temporary file to remove.
            temporary_path = None
            # A cancellation that lands after this point cannot un-install a
            # validated database, so hiding its path would only hide what is
            # already on disk. The caller re-checks before loading it.
            return self._destination
        except _TRANSFER_FAILURES as error:
            self._report(error)
            return None
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                if temporary_path is not None:
                    _remove_quiet(temporary_path)

    def _stream_into(self, body, progress_cb) -> bool:
        """Stream the response into `body`; report whether it completed."""
        opener = _ResponseOpener(
            self._url,
            (_CONNECT_TIMEOUT_SECONDS, self._read_timeout()),
            self._deadline,
        )
        try:
            if self._begin(opener):
                return False
            opened = opener.claim(self._deadline - time.monotonic())
            if opened is None:
                # The wait ended before the peer finished its header, so this
                # transfer is over; the helper closes what it opens next.
                return False
            context, response = opened
            with contextlib.ExitStack() as stack:
                # The helper entered this context; leaving it from here
                # closes the response and its session on this worker's own
                # thread, exactly where they were always closed. The
                # withdrawal is registered after it and so runs before it,
                # leaving no instant in which the watchdog could fire
                # against a response that is already being closed.
                stack.push(context)
                stack.callback(self._end)
                if self._register(response):
                    return False
                declared = _declared_length(response)
                if declared > self._max_bytes:
                    # A body this size is refused on its own word, before one
                    # byte of it is read.
                    raise _too_large(self._max_bytes)
                downloaded = 0
                for chunk in http_client.iter_response_body(
                    response,
                    _CHUNK_SIZE,
                ):
                    if self._stopped():
                        return False
                    downloaded += len(chunk)
                    if downloaded > self._max_bytes:
                        # Rejected before the offending chunk is written, so a
                        # lying declaration cannot fill the cache directory.
                        raise _too_large(self._max_bytes)
                    body.write(chunk)
                    if progress_cb is not None:
                        # A caller's callback is arbitrary work: it never runs
                        # under the lock `cancel()` needs.
                        progress_cb(downloaded, declared)
        finally:
            self._end()
        return True

    def _read_timeout(self) -> float:
        """Bound one blocking read by 15 seconds or the time left.

        A deadline already behind this transfer leaves no wait at all, so the
        bound never turns into a negative timeout the boundary would have to
        reject on its behalf.
        """
        return max(
            0.0,
            min(
                MAX_READ_TIMEOUT_SECONDS,
                self._deadline - time.monotonic(),
            ),
        )

    def _begin(self, opener) -> bool:
        """Arm the deadline, start opening, and report cancellation.

        A cancellation that arrived before anything was opened is reported
        here, so it cannot be lost between the threads. The watchdog is armed
        before the open rather than after it, because the connect,
        status-line and header phases are inside the deadline too: they have
        no response to interrupt, so what the watchdog ends there is the
        worker's own wait for one.
        """
        with self._lock:
            self._opener = opener
            if self._cancelled.is_set():
                return True
            watchdog = threading.Timer(
                max(0.0, self._deadline - time.monotonic()),
                self._expire,
            )
            self._watchdog = watchdog
        _start_daemon(watchdog)
        opener.start()
        return False

    def _register(self, response) -> bool:
        """Publish the response an interrupt may reach; report a stop.

        A cancellation or an expiry that arrived while the header was still
        being read is reported here, so it cannot be lost between the
        threads.
        """
        with self._lock:
            self._opener = None
            self._response = response
            return self._stopped()

    def _expire(self) -> None:
        """End this transfer from outside, the moment its deadline passes."""
        with self._lock:
            opener, response = self._opener, self._response
            if opener is None and response is None:
                # Whatever this watchdog was armed for is already over.
                return
            self._expired.set()
        # Releasing stays outside the lock the worker also takes.
        _release(opener, response)

    def _end(self) -> None:
        """Withdraw this transfer's release targets and disarm its deadline."""
        with self._lock:
            self._opener = None
            self._response = None
            watchdog = self._watchdog
            self._watchdog = None
        if watchdog is not None:
            watchdog.cancel()

    def _stopped(self) -> bool:
        """Report whether cancellation or the deadline forbids going on."""
        if self._cancelled.is_set():
            return True
        if time.monotonic() >= self._deadline:
            # A deadline this transfer notices for itself is recorded exactly
            # as the watchdog records the one it interrupts a read for, so
            # both end the transfer with the same single reason.
            self._expired.set()
        return self._expired.is_set()

    def _report_stop(self) -> None:
        """Log the one reason a stopped transfer produced no database.

        A cancellation is the caller's own doing and describes nothing worth
        reporting; the deadline is this transfer's own limit, and it is
        reported once, at whichever checkpoint noticed it.
        """
        if self._stopped() and not self._cancelled.is_set():
            helpers.debug_msg([_GEOIP_MSG, _DEADLINE_EXCEEDED])

    def _report(self, error: BaseException) -> None:
        """Log one payload-free reason this transfer produced no database."""
        if self._cancelled.is_set():
            # `cancel()` ends the read under the worker, so this failure is
            # the transfer's own doing rather than a transport failure worth
            # reporting.
            return
        if self._expired.is_set():
            # The deadline ended this read, so whatever it raised describes
            # this transfer's own limit rather than the peer.
            helpers.debug_msg([_GEOIP_MSG, _DEADLINE_EXCEEDED])
            return
        failure = http_client.describe_http_failure(error)
        diagnostic = (
            diagnostic_for_exception(error)
            if failure is None
            else failure.diagnostic
        )
        helpers.debug_msg([_GEOIP_MSG, diagnostic])
