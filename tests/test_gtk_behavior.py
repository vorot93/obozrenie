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

import errno
import io
import os
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

from obozrenie import game_table
from obozrenie import gtk as gtk_module
from obozrenie import gtk_geoip
from obozrenie import settings as settings_module
from obozrenie.diagnostics import SafeDiagnostic


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


class StartupTests(unittest.TestCase):
    @staticmethod
    def make_settings(root):
        root = Path(root)
        bundled = {
            ("settings", "defaults", "settings.toml"): b"""[common]
selected-game-connect = "game"
selected-game-browser = "game"
server-host = ""
game-listview = true
""",
            ("settings", "defaults", "games.toml"): b"""[game]
path = "client"
master_uri = ["master://example.test:27950"]
""",
        }

        def load_resource(*parts):
            with io.BytesIO(bundled[tuple(parts)]) as table_file:
                return tomllib.load(table_file)

        table = game_table.GameTable({
            "game": {
                "name": "Game",
                "adapter": "qstat",
                "launch_pattern": "quake",
                "settings": ["path", "master_uri"],
            },
        })
        profile = root / "profile"
        profile.mkdir()
        settings = settings_module.Settings(
            table,
            str(profile),
            resource_loader=load_resource,
        )
        Path(settings.user_common_settings_path).write_text(
            '[common]\nsecret = "unterminated\n',
            encoding="utf-8",
        )
        Path(settings.user_game_settings_path).write_text(
            '[game]\nsecret = "unterminated\n',
            encoding="utf-8",
        )
        return settings

    @staticmethod
    def make_startup_app(settings):
        """One `on_startup` stand-in carrying the App's own methods under test."""
        quits = []
        app = SimpleNamespace(
            window=None,
            presenter=None,
            core=SimpleNamespace(game_table=SimpleNamespace(get_game_set=lambda: {"game"})),
            settings=settings,
            handler_failure=None,
            status="starting",
            _pending_settings_warnings=[],
            quit=lambda: quits.append("quit"),
            on_about=mock.Mock(),
            on_quit=mock.Mock(),
            add_action=mock.Mock(),
            set_accels_for_action=mock.Mock(),
        )
        app._record_handler_failure = MethodType(gtk_module.App._record_handler_failure, app)
        app._add_action = MethodType(gtk_module.App._add_action, app)
        return app, quits

    def patch_construction(self):
        """Replace the window, presenter, and icons `on_startup` builds."""
        window = mock.Mock(name="window")
        presenter = mock.Mock(name="presenter")
        self.enterContext(mock.patch.object(gtk_module, "MainWindow", return_value=window))
        self.enterContext(mock.patch.object(gtk_module, "MainPresenter", return_value=presenter))
        self.enterContext(mock.patch.object(
            gtk_module.gtk_icons, "game_icon_textures", return_value={"game": object()}))
        self.enterContext(mock.patch.object(gtk_module, "Gio"))
        return window, presenter

    def test_startup_quits_when_its_own_first_log_line_fails(self):
        broken = OSError(errno.EBADF, "Bad file descriptor")
        settings = SimpleNamespace(load=mock.Mock())
        app, quits = self.make_startup_app(settings)

        with mock.patch.object(gtk_module.helpers, "debug_msg", side_effect=broken):
            gtk_module.App.on_startup(app, app)

        self.assertEqual(quits, ["quit"])

    def test_startup_records_the_settings_failure_its_report_could_not_write(self):
        load_error = PermissionError(13, "Permission denied")
        settings = SimpleNamespace(load=mock.Mock(side_effect=load_error))
        app, quits = self.make_startup_app(settings)
        self.patch_construction()
        written = []

        def fail_after_the_first_line(message):
            written.append(message)
            if len(written) > 1:
                raise OSError(errno.EBADF, "Bad file descriptor")

        with mock.patch.object(gtk_module.helpers, "debug_msg", side_effect=fail_after_the_first_line):
            gtk_module.App.on_startup(app, app)

        self.assertEqual(quits, ["quit"])
        self.assertEqual(
            app.handler_failure,
            SafeDiagnostic("Unexpected PermissionError; exception details omitted."),
        )
        self.assertEqual(len(written), 2)

    def test_startup_records_the_declared_diagnostic_for_an_unreadable_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(directory)
            unreadable = Path(settings.user_common_settings_path)
            os.chmod(unreadable, 0o000)
            try:
                with open(unreadable, "rb"):
                    pass
            except PermissionError:
                pass
            else:
                self.skipTest("this user can read a 0000 settings file")

            app, quits = self.make_startup_app(settings)
            self.patch_construction()
            with mock.patch.object(gtk_module.helpers, "debug_msg") as logged:
                gtk_module.App.on_startup(app, app)

        recorded = SafeDiagnostic("Settings file could not be read.")
        self.assertEqual(quits, ["quit"])
        self.assertEqual(app.handler_failure, recorded)
        self.assertIn(mock.call([gtk_module.GTK_MSG, recorded]), logged.call_args_list)

    def test_on_startup_loads_games_before_settings_and_binds_before_attaching_and_rendering(self):
        self.enterContext(mock.patch.object(gtk_module.helpers, "debug_msg"))
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(directory)
            app, quits = self.make_startup_app(settings)
            window, presenter = self.patch_construction()
            manager = mock.Mock()
            manager.attach_mock(presenter, "presenter")
            manager.attach_mock(window, "window")
            real_load = settings.load
            settings.load = mock.Mock(side_effect=real_load)
            manager.attach_mock(settings.load, "load")

            gtk_module.App.on_startup(app, app)

        self.assertEqual(quits, [])
        self.assertEqual(app.status, "up")
        self.assertIs(app.window, window)
        self.assertIs(app.presenter, presenter)
        self.assertEqual(len(app._pending_settings_warnings), 2)
        self.assertEqual(
            [name for name, _args, _kwargs in manager.mock_calls],
            [
                "presenter.load_games",
                "load",
                "presenter.bind_settings",
                "window.attach_presenter",
                "presenter.render_loaded_state",
            ],
        )
        window.attach_presenter.assert_called_once_with(presenter)
        gtk_module.MainWindow.assert_called_once_with(application=app)
        gtk_module.MainPresenter.assert_called_once()
        self.assertIs(gtk_module.MainPresenter.call_args.args[0], window)
        self.assertEqual(app.add_action.call_count, 2)
        app.set_accels_for_action.assert_called_once_with("app.quit", ["<Control>q"])

    @staticmethod
    def make_active_app(window, coordinator, warnings=()):
        app = SimpleNamespace(
            window=window,
            services=SimpleNamespace(geoip_downloads=coordinator),
            _pending_settings_warnings=list(warnings),
            _startup_dialogs_pending=False,
        )
        app._continue_startup_dialogs = MethodType(gtk_module.App._continue_startup_dialogs, app)
        app._show_startup_dialogs = MethodType(gtk_module.App._show_startup_dialogs, app)
        app._release_startup_admission = MethodType(gtk_module.App._release_startup_admission, app)
        return app

    def test_activation_chains_warnings_before_one_offer_and_admission_waits_for_completion(self):
        window = mock.Mock()
        offers = []

        class RecordingCoordinator:
            def offer(self, parent, finished_callback):
                offers.append((parent, finished_callback))
                return True

        app = self.make_active_app(window, RecordingCoordinator(), ["first", "second"])
        queued = []
        with mock.patch.object(gtk_module.GLib, "idle_add", side_effect=lambda callback, *args: queued.append((callback, args))):
            gtk_module.App.on_activate(app, app)
            self.assertEqual(window.present.call_count, 1)
            self.assertTrue(app._startup_dialogs_pending)
            self.assertEqual(app._pending_settings_warnings, [])
            self.assertEqual(len(queued), 1)

            # An activation while the dispatcher is queued only presents.
            gtk_module.App.on_activate(app, app)
            self.assertEqual(window.present.call_count, 2)
            self.assertEqual(len(queued), 1)

            callback, args = queued[0]
            self.assertIs(callback(*args), False)
            self.assertEqual(window.present_error.call_count, 1)
            first_message, first_continue = window.present_error.call_args.args
            self.assertEqual(first_message, "first")
            self.assertEqual(offers, [])

            # The second warning waits for the first to close; the offer
            # waits for the second.
            first_continue()
            self.assertEqual(window.present_error.call_count, 2)
            second_message, second_continue = window.present_error.call_args.args
            self.assertEqual(second_message, "second")
            self.assertEqual(offers, [])
            second_continue()
            self.assertEqual(len(offers), 1)
            self.assertIs(offers[0][0], window)

            # Completion, not the window, releases startup admission.
            gtk_module.App.on_activate(app, app)
            self.assertEqual(window.present.call_count, 3)
            self.assertEqual(len(queued), 1)
            self.assertTrue(app._startup_dialogs_pending)
            offers[0][1]()
            self.assertFalse(app._startup_dialogs_pending)

            gtk_module.App.on_activate(app, app)
            self.assertEqual(len(queued), 2)
            queued[1][0](*queued[1][1])
            self.assertEqual(len(offers), 2)
            self.assertEqual(window.present_error.call_count, 2)

    def test_refused_offer_releases_startup_admission_at_once(self):
        window = mock.Mock()
        offers = []

        class RefusingCoordinator:
            def offer(self, parent, finished_callback):
                offers.append((parent, finished_callback))
                return False

        app = self.make_active_app(window, RefusingCoordinator())
        queued = []
        with mock.patch.object(gtk_module.GLib, "idle_add", side_effect=lambda callback, *args: queued.append((callback, args))):
            gtk_module.App.on_activate(app, app)
            self.assertTrue(app._startup_dialogs_pending)
            for callback, args in queued:
                self.assertIs(callback(*args), False)
            self.assertFalse(app._startup_dialogs_pending)

            gtk_module.App.on_activate(app, app)
            for callback, args in queued[1:]:
                self.assertIs(callback(*args), False)

        self.assertEqual(len(offers), 2)
        window.present_error.assert_not_called()
        self.assertEqual(window.present.call_count, 2)

    def test_a_failing_warning_dialog_releases_admission_and_propagates(self):
        window = mock.Mock()
        window.present_error.side_effect = RuntimeError("no dialog")
        app = self.make_active_app(window, mock.Mock(), ["warning"])
        queued = []
        with mock.patch.object(gtk_module.GLib, "idle_add", side_effect=lambda callback, *args: queued.append((callback, args))):
            gtk_module.App.on_activate(app, app)
            with self.assertRaises(RuntimeError):
                queued[0][0](*queued[0][1])

        self.assertFalse(app._startup_dialogs_pending)
        app.services.geoip_downloads.offer.assert_not_called()

    def test_every_activation_presents_without_reaching_for_another_offer(self):
        window = mock.Mock()
        coordinator = mock.Mock()
        app = self.make_active_app(window, coordinator, ["unseen warning"])
        app._startup_dialogs_pending = True

        with mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            for _ in range(3):
                gtk_module.App.on_activate(app, app)

        self.assertEqual(window.present.call_count, 3)
        idle_add.assert_not_called()
        window.present_error.assert_not_called()
        coordinator.offer.assert_not_called()
        self.assertTrue(app._startup_dialogs_pending)
        self.assertEqual(app._pending_settings_warnings, ["unseen warning"])

    def test_activation_after_a_startup_that_built_no_window_does_nothing(self):
        # GIO emits `activate` even once `on_startup` has quit, and a startup
        # that failed before `MainWindow` left `window` as `None`.
        coordinator = mock.Mock()
        app = self.make_active_app(None, coordinator, ["unseen warning"])

        with mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            gtk_module.App.on_activate(app, app)

        idle_add.assert_not_called()
        coordinator.offer.assert_not_called()
        self.assertFalse(app._startup_dialogs_pending)
        self.assertEqual(app._pending_settings_warnings, ["unseen warning"])


class ApplicationShutdownTests(unittest.TestCase):
    def test_app_carries_a_status_before_startup_can_fail(self):
        app = gtk_module.App(mock.Mock(), mock.Mock(), mock.Mock())

        self.assertEqual(app.status, "starting")
        self.assertIsNone(app.handler_failure)
        self.assertIsNone(app.window)
        self.assertEqual(app.get_flags(), gtk_module.Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.assertEqual(app.get_application_id(), gtk_module.APPLICATION_ID)

        # Emitted, not called directly, so the "shutdown" connection made in
        # `__init__` is what runs `on_shutdown` here.
        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            app.emit("shutdown")

        self.assertEqual(app.status, "start failed")
        app.services.begin_close.assert_called_once_with()

    @staticmethod
    def make_app(status, save):
        events = []

        def begin_close():
            events.append("begin-close")

        app = SimpleNamespace(
            status=status,
            handler_failure=None,
            settings=SimpleNamespace(save=save),
            services=SimpleNamespace(begin_close=begin_close),
        )
        app._record_handler_failure = MethodType(gtk_module.App._record_handler_failure, app)
        return app, events

    def test_shutdown_begins_service_close_even_when_settings_save_raises(self):
        error = RuntimeError("settings save failed")
        saves = []

        def save():
            saves.append("save")
            raise error

        app, close_events = self.make_app("up", save)

        with mock.patch.object(gtk_module.helpers, "debug_msg") as debug_msg:
            gtk_module.App.on_shutdown(app, app)

        recorded = SafeDiagnostic("Unexpected RuntimeError; exception details omitted.")
        self.assertEqual(app.handler_failure, recorded)
        self.assertIn(mock.call([gtk_module.GTK_MSG, recorded]), debug_msg.call_args_list)
        self.assertEqual(saves, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "up")

    def test_shutdown_records_only_the_first_failure_it_reports(self):
        app, close_events = self.make_app("up", mock.Mock(side_effect=RuntimeError("settings save failed")))
        app.handler_failure = SafeDiagnostic("startup already failed")

        with mock.patch.object(gtk_module.helpers, "debug_msg") as debug_msg:
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(app.handler_failure, SafeDiagnostic("startup already failed"))
        self.assertIn(
            mock.call([gtk_module.GTK_MSG, SafeDiagnostic("Unexpected RuntimeError; exception details omitted.")]),
            debug_msg.call_args_list,
        )
        self.assertEqual(close_events, ["begin-close"])

    def test_successful_shutdown_saves_settings_then_begins_service_close(self):
        events = []
        app, close_events = self.make_app("up", lambda: events.append("save"))

        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(events, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "shutting down")

    def test_a_failing_log_line_after_a_successful_save_is_not_a_failure(self):
        broken = OSError(errno.EBADF, "Bad file descriptor")
        saves = []
        app, close_events = self.make_app("up", lambda: saves.append("save"))

        with mock.patch.object(gtk_module.helpers, "debug_msg", side_effect=broken):
            with self.assertRaises(OSError) as caught:
                gtk_module.App.on_shutdown(app, app)

        self.assertIs(caught.exception, broken)
        self.assertIsNone(app.handler_failure)
        self.assertEqual(saves, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "shutting down")

    def test_failed_startup_shutdown_begins_service_close_without_saving(self):
        events = []
        app, close_events = self.make_app("starting", lambda: events.append("save"))

        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(events, [])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "start failed")


if __name__ == "__main__":
    unittest.main()
