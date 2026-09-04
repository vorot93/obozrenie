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

"""App and coordinator behaviour over fakes: no widget is built here."""

import threading
import time
import unittest
from unittest import mock

from obozrenie import gtk_geoip


class _FakeQuestion:
    """The question handle: records dismissal, replays responses."""

    def __init__(self, events, on_accept, on_decline):
        self.events = events
        self.on_accept = on_accept
        self.on_decline = on_decline
        self.dismissed = 0

    def dismiss(self):
        self.dismissed += 1
        self.events.append(("dismiss",))


class _FakeTransferDialog:
    """The transfer handle: records progress and closes, replays Cancel."""

    def __init__(self, events, on_cancel):
        self.events = events
        self.on_cancel = on_cancel
        self.calls = []
        self.closed = 0

    def set_fraction(self, fraction):
        self.calls.append(("set_fraction", fraction))

    def pulse(self):
        self.calls.append(("pulse",))

    def close(self):
        self.closed += 1
        self.events.append(("close-transfer",))


class _FakeDialogs:
    """Stand in for GeoIPOfferDialogs, keeping every handle it made."""

    def __init__(self, events):
        self.events = events
        self.questions = []
        self.transfers = []
        self.present_question_error = None

    def present_question(self, parent, on_accept, on_decline):
        self.events.append(("question", parent))
        if self.present_question_error is not None:
            raise self.present_question_error
        question = _FakeQuestion(self.events, on_accept, on_decline)
        self.questions.append(question)
        return question

    def present_transfer(self, parent, on_cancel):
        self.events.append(("transfer-dialog", parent))
        dialog = _FakeTransferDialog(self.events, on_cancel)
        self.transfers.append(dialog)
        return dialog


class _FakeTransfer:
    """Stand in for one owned GeoIP transfer, without touching the network."""

    def __init__(
        self,
        destination,
        deadline,
        keywords,
        *,
        result=None,
        blocking=False,
        progress_before=(),
        progress_after=(),
    ):
        self.destination = destination
        self.deadline = deadline
        self.keywords = keywords
        self.result = result
        self.blocking = blocking
        self.progress_before = progress_before
        self.progress_after = progress_after
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.released = threading.Event()
        self.thread = None

    def cancel(self):
        # The real transfer closes the response it is reading, which returns
        # the blocked worker; releasing here reproduces exactly that.
        self.cancelled.set()
        self.released.set()

    def run(self, progress_cb=None):
        self.thread = threading.current_thread()
        self.started.set()
        for update in self.progress_before:
            progress_cb(*update)
        if self.blocking:
            self.released.wait(1)
        for update in self.progress_after:
            progress_cb(*update)
        if self.cancelled.is_set():
            return None
        return self.result


class _TransferFactory:
    """Replace `GeoIPDatabaseDownload` and record every transfer built."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.transfers = []

    def __call__(self, destination, deadline, **keywords):
        transfer = _FakeTransfer(destination, deadline, keywords, **self.behaviour)
        self.transfers.append(transfer)
        return transfer

    def release_all(self):
        """Unblock and join every worker this factory's transfers own."""
        for transfer in self.transfers:
            transfer.released.set()
            if transfer.thread is not None:
                transfer.thread.join(1)


class _IdleQueue:
    """Collect `GLib.idle_add` payloads for the test to run in order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queued = []
        self._next_id = 0

    def __call__(self, callback, *args):
        with self._lock:
            self._queued.append((callback, args))
            self._next_id += 1
            return self._next_id

    @property
    def pending(self):
        with self._lock:
            return tuple(self._queued)

    def drain(self):
        while True:
            with self._lock:
                if not self._queued:
                    return
                callback, args = self._queued.pop(0)
            callback(*args)


class _FakeGeolocation:
    """Stand in for geoip.Geolocation: the enabled flag and the load only."""

    def __init__(self, events):
        self.enabled = False
        self.events = events

    def load(self, path):
        self.events.append(("load", path))
        self.enabled = True


class GeoIPDownloadCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.geolocation = _FakeGeolocation(self.events)
        self.dialogs = _FakeDialogs(self.events)
        self.idle = _IdleQueue()
        self.parent = object()
        self.enterContext(mock.patch.object(
            gtk_geoip.GLib, "idle_add", side_effect=self.idle))
        self.coordinator = gtk_geoip.GeoIPDownloadCoordinator(
            self.geolocation, dialogs=self.dialogs)

    def install_transfers(self, **behaviour):
        factory = _TransferFactory(**behaviour)
        self.enterContext(mock.patch.object(
            gtk_geoip.geoip, "GeoIPDatabaseDownload", factory))
        self.addCleanup(factory.release_all)
        return factory

    def finished(self):
        self.events.append(("finished",))

    def refused(self):
        raise AssertionError("a refused offer must not report completion")

    def offer(self, finished_callback=None):
        return self.coordinator.offer(
            self.parent,
            self.finished if finished_callback is None else finished_callback,
        )

    def accept(self, factory):
        """Accept the open question and wait for its worker to start."""
        self.dialogs.questions[-1].on_accept()
        self.assertEqual(len(factory.transfers), 1)
        transfer = factory.transfers[0]
        self.assertTrue(transfer.started.wait(1))
        return transfer

    def test_offer_is_single_flight_until_decline(self):
        factory = self.install_transfers()

        self.assertIs(self.offer(), True)
        self.assertEqual(self.events, [("question", self.parent)])
        self.assertEqual(len(self.dialogs.questions), 1)

        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs.questions), 1)

        self.dialogs.questions[0].on_decline()
        self.assertEqual(self.events, [("question", self.parent), ("finished",)])
        self.assertEqual(factory.transfers, [])
        self.assertEqual(self.dialogs.transfers, [])
        # A question that answered closes itself; it is never dismissed.
        self.assertEqual(self.dialogs.questions[0].dismissed, 0)

        # The declined offer released single flight, so the next activation
        # may offer again.
        self.assertIs(self.offer(), True)
        self.assertEqual(len(self.dialogs.questions), 2)

        self.dialogs.questions[1].on_decline()
        self.geolocation.enabled = True
        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs.questions), 2)

    def test_failed_presentation_releases_the_offer(self):
        self.install_transfers()
        error = RuntimeError("dialog presentation failed")
        self.dialogs.present_question_error = error

        with self.assertRaises(RuntimeError) as caught:
            self.offer(self.refused)

        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, [("question", self.parent)])

        # The released offer leaves this coordinator free to offer again.
        self.dialogs.present_question_error = None
        self.assertIs(self.offer(), True)
        self.assertEqual(len(self.dialogs.questions), 1)

    def test_close_during_presentation_dismisses_the_question_and_refuses(self):
        self.install_transfers()
        original = self.dialogs.present_question

        def close_while_presenting(parent, on_accept, on_decline):
            question = original(parent, on_accept, on_decline)
            self.coordinator.begin_close()
            return question

        self.dialogs.present_question = close_while_presenting

        self.assertIs(self.offer(self.refused), False)

        self.assertEqual(self.dialogs.questions[0].dismissed, 1)
        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs.questions), 1)

    def test_accepted_offer_remains_single_flight_until_transfer_completion(self):
        factory = self.install_transfers(result="/cache/db.mmdb", blocking=True)

        self.assertIs(self.offer(), True)
        started = time.monotonic()
        transfer = self.accept(factory)

        self.assertEqual(
            self.events,
            [("question", self.parent), ("transfer-dialog", self.parent)],
        )
        self.assertEqual(transfer.destination, gtk_geoip.geoip.CACHE_DB_PATH)
        self.assertEqual(transfer.keywords, {})
        self.assertGreaterEqual(
            transfer.deadline,
            started + gtk_geoip.geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )
        self.assertLessEqual(
            transfer.deadline,
            time.monotonic() + gtk_geoip.geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )

        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs.questions), 1)

        transfer.released.set()
        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertIs(self.offer(self.refused), False)

        self.idle.drain()
        self.assertEqual(
            self.events[2:],
            [("close-transfer",), ("load", "/cache/db.mmdb"), ("finished",)],
        )
        self.assertEqual(self.dialogs.transfers[0].closed, 1)
        self.assertEqual(len(factory.transfers), 1)

    def test_a_repeated_accept_starts_no_second_transfer(self):
        factory = self.install_transfers(blocking=True)

        self.assertIs(self.offer(), True)
        self.accept(factory)
        self.dialogs.questions[0].on_accept()

        self.assertEqual(len(factory.transfers), 1)
        self.assertEqual(len(self.dialogs.transfers), 1)

    def test_progress_reaches_the_transfer_dialog_until_close(self):
        factory = self.install_transfers(
            result="/cache/db.mmdb",
            blocking=True,
            progress_before=((1, 4), (0, 0)),
        )

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)
        self.idle.drain()

        self.assertEqual(
            self.dialogs.transfers[0].calls,
            [("set_fraction", 0.25), ("pulse",)],
        )
        transfer.released.set()
        transfer.thread.join(1)

    def test_cancel_from_the_transfer_dialog_stops_the_transfer_and_completes_without_a_database(self):
        factory = self.install_transfers(result="/cache/db.mmdb", blocking=True)

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        self.dialogs.transfers[0].on_cancel()
        self.assertTrue(transfer.cancelled.is_set())
        self.assertEqual(self.dialogs.transfers[0].closed, 1)

        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.idle.drain()

        self.assertEqual(self.events[-1], ("finished",))
        self.assertNotIn(("load", "/cache/db.mmdb"), self.events)
        self.assertFalse(self.geolocation.enabled)
        # Completion closes the dialog again; the handle makes that a no-op.
        self.assertEqual(self.dialogs.transfers[0].closed, 2)

    def test_begin_close_cancels_transfer_and_suppresses_late_dispatch(self):
        factory = self.install_transfers(
            result="/cache/db.mmdb",
            blocking=True,
            progress_after=((1, 2),),
        )

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        self.coordinator.begin_close()
        self.assertTrue(transfer.cancelled.is_set())

        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertEqual(self.idle.pending, ())
        self.assertEqual(self.dialogs.transfers[0].closed, 0)
        self.assertNotIn(("finished",), self.events)
        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs.questions), 1)

    def test_queued_completion_after_close_does_not_load_database_or_touch_dialogs(self):
        factory = self.install_transfers(
            result="/cache/db.mmdb",
            blocking=True,
            progress_after=((3, 4),),
        )

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        transfer.released.set()
        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertEqual(len(self.idle.pending), 2)

        self.coordinator.begin_close()
        self.idle.drain()

        self.assertNotIn(("finished",), self.events)
        self.assertNotIn(("load", "/cache/db.mmdb"), self.events)
        self.assertEqual(self.dialogs.transfers[0].closed, 0)
        self.assertEqual(self.dialogs.transfers[0].calls, [])
        self.assertFalse(self.geolocation.enabled)

    def test_join_until_uses_only_the_remaining_application_deadline(self):
        factory = self.install_transfers(blocking=True)

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        recorded = []
        real_join = transfer.thread.join

        def recording_join(timeout=None):
            recorded.append(timeout)
            return real_join(timeout)

        with mock.patch.object(transfer.thread, "join", recording_join):
            self.coordinator.join_until(time.monotonic() + 0.2)
            self.assertEqual(len(recorded), 1)
            self.assertGreater(recorded[0], 0)
            self.assertLessEqual(recorded[0], 0.2)
            self.assertTrue(transfer.thread.is_alive())

            self.coordinator.join_until(time.monotonic() - 0.2)
            self.assertEqual(len(recorded), 1)

            with self.assertRaises(TypeError):
                self.coordinator.join_until(1)
            self.assertEqual(len(recorded), 1)

            transfer.released.set()
            self.coordinator.join_until(time.monotonic() + 1.0)
            self.assertEqual(len(recorded), 2)

        self.assertFalse(transfer.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
