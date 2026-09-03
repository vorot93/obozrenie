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

"""The GeoIP database offer: one dialog, one transfer, one shutdown."""

import threading
import time
from collections.abc import Callable

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk

from obozrenie import geoip, i18n
from obozrenie.network_calls import validate_deadline


_GEOIP_WORKER_THREAD_NAME = "obozrenie-geoip-download"


class GeoIPDownloadCoordinator:

    """Owner of the GeoIP offer dialog, its transfer, and their shutdown.

    One offer is in flight at a time. `offer` reserves this coordinator under
    its state lock and reports whether it presented a dialog, so a repeated
    activation can neither stack a second dialog nor start a second transfer.
    Only a presented offer promises a completion callback, and that callback
    runs once the dialog has been destroyed and any downloaded database has
    been loaded, which is what keeps startup admission held until then.

    The state lock only publishes the dialog, transfer, worker, and completion
    flags that close and the worker both read: dialog work, the caller's
    callback, and thread joins never run under it. `begin_close` marks this
    coordinator closing and cancels the transfer, which closes the response its
    worker may be reading. The worker re-checks that flag before it queues
    anything, and every queued callback re-checks it again, so a transfer that
    outlives close can neither load a database nor touch a widget.
    """

    def __init__(self, geolocation: geoip.Geolocation) -> None:
        self._geolocation = geolocation
        self._state_lock = threading.Lock()
        self._dialog = None
        self._transfer = None
        self._worker = None
        self._closing = False
        self._active = False
        self._completed = False
        self._finished_callback = None

    def offer(
        self,
        parent: Gtk.Window,
        finished_callback: Callable[[], None],
    ) -> bool:
        """Present one GeoIP offer; report whether this call started it.

        A true result promises exactly one later `finished_callback`, after the
        offer is declined or after an accepted transfer has completed. A false
        result promises nothing and never calls back: the database is already
        loaded, this coordinator is closing, or an offer is already in flight.
        """
        with self._state_lock:
            if self._closing or self._offer_in_flight():
                return False
            if self._geolocation.enabled:
                return False
            self._active = True
            self._completed = False
            self._finished_callback = finished_callback

        presented = False
        try:
            dialog = self._build_offer_dialog(parent)
            with self._state_lock:
                closing = self._closing
                if not closing:
                    self._dialog = dialog
            if closing:
                # Close arrived while this dialog was being built. It is this
                # call's to destroy, and it promised no callback.
                dialog.destroy()
                return False
            try:
                dialog.show()
            except BaseException:
                # An offer that never reached the screen still owns its
                # dialog: releasing the reference below would otherwise leave
                # it alive with nothing able to destroy it.
                dialog.destroy()
                raise
            presented = True
        finally:
            if not presented:
                self._release_offer()
        return True

    def begin_close(self) -> None:
        """Stop any transfer and suppress every callback still to be queued."""
        with self._state_lock:
            self._closing = True
            transfer = self._transfer
        if transfer is not None:
            # Cancellation closes the response the worker may be reading, so it
            # runs outside the lock that worker's own callbacks take.
            transfer.cancel()

    def join_until(self, deadline: float) -> None:
        """Join this coordinator's transfer worker against one deadline."""
        deadline = validate_deadline(deadline)
        with self._state_lock:
            worker = self._worker
        if worker is None or worker.ident is None:
            # A worker that never launched cannot be joined.
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            worker.join(remaining)

    def _offer_in_flight(self) -> bool:
        """Report whether an offer or its worker still holds this coordinator.

        The caller holds the state lock. A cancelled transfer's worker keeps
        the coordinator until it exits, so this owner never holds more than one
        worker and `join_until` can bound all of them.
        """
        if self._active:
            return True
        worker = self._worker
        return worker is not None and worker.is_alive()

    def _release_offer(self) -> None:
        """Drop one offer that never reached a completion of its own."""
        with self._state_lock:
            self._active = False
            self._dialog = None
            self._transfer = None
            self._finished_callback = None

    def _build_offer_dialog(self, parent):
        """Build the offer dialog and wire the widgets its response drives."""
        dialog = Gtk.Dialog(
            title=i18n._("Download GeoIP database?"),
            transient_for=parent,
            modal=True)
        dialog.add_button(i18n._("Not now"), Gtk.ResponseType.CANCEL)
        download_button = dialog.add_button(
            i18n._("Download"), Gtk.ResponseType.ACCEPT)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        label = Gtk.Label(
            label=i18n._("Server geolocation needs a country database. "
                         "Download it now (a few megabytes)?"))
        label.set_line_wrap(True)
        content.add(label)
        progress = Gtk.ProgressBar(show_text=True)
        content.add(progress)
        # This content is built here rather than by GtkBuilder, so nothing in
        # it is visible until it is shown; the progress bar reappears only once
        # a transfer starts.
        content.show_all()
        progress.hide()

        dialog.connect(
            "response", self._on_response, label, download_button, progress)
        return dialog

    def _on_response(self, dialog, response, label, download_button, progress):
        """Route one dialog response on the GTK main thread."""
        if response == Gtk.ResponseType.ACCEPT:
            self._accept(dialog, label, download_button, progress)
        else:
            self._decline(dialog)

    def _accept(self, dialog, label, download_button, progress):
        """Start this offer's one transfer on its own daemon worker."""
        with self._state_lock:
            if self._closing or self._transfer is not None:
                # Neither close nor a repeated response starts a transfer.
                return

        download_button.set_sensitive(False)
        dialog.set_response_sensitive(Gtk.ResponseType.CANCEL, True)
        label.set_text(i18n._("Downloading GeoIP database…"))
        progress.show()

        # The transfer's own window opens when it is constructed, so it is
        # built immediately before the worker that runs it.
        transfer = geoip.GeoIPDatabaseDownload(
            geoip.CACHE_DB_PATH,
            time.monotonic() + geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )
        worker = threading.Thread(
            target=self._run_transfer,
            args=(transfer, progress),
            name=_GEOIP_WORKER_THREAD_NAME,
            daemon=True,
        )
        with self._state_lock:
            closing = self._closing
            if not closing:
                self._transfer = transfer
                self._worker = worker
        if closing:
            # Close won the race with this response: the transfer is stopped
            # before it runs, and close owns the offer from here.
            transfer.cancel()
            return

        try:
            worker.start()
        except RuntimeError:
            # No worker will report this transfer, so this response completes
            # the offer itself rather than stranding startup admission.
            transfer.cancel()
            self._complete(None)

    def _decline(self, dialog):
        """Finish a declined offer, stopping a transfer it already started."""
        with self._state_lock:
            transfer = self._transfer
        if transfer is None:
            self._complete(None)
            return
        # A running transfer keeps exactly one completion owner: its worker.
        # Cancelling returns that worker at once, and hiding the dialog gives
        # the same immediate feedback destroying it would.
        transfer.cancel()
        dialog.hide()

    def _run_transfer(self, transfer, progress):
        """Run one transfer to completion on this coordinator's worker."""
        path = None
        try:
            path = transfer.run(
                lambda downloaded, total: self._dispatch(
                    self._apply_progress, progress, downloaded, total))
        finally:
            self._dispatch(self._finish_transfer, path)

    def _dispatch(self, callback, *args):
        """Queue one main-thread callback unless close has already begun."""
        with self._state_lock:
            if self._closing:
                return
        # Queueing is not the coordinator's own state, and the callback it
        # queues re-checks close, so it never runs under the state lock.
        GLib.idle_add(callback, *args)

    def _apply_progress(self, progress, downloaded, total):
        """Render one progress update, unless close has begun meanwhile."""
        with self._state_lock:
            if self._closing:
                return False
        if total > 0:
            progress.set_fraction(downloaded / total)
        else:
            progress.pulse()
        return False

    def _finish_transfer(self, path):
        """Complete the accepted offer, unless close has begun meanwhile."""
        with self._state_lock:
            if self._closing:
                return False
        self._complete(path)
        return False

    def _complete(self, path):
        """Finish one offer exactly once, on the GTK main thread.

        The dialog is destroyed and any transferred database is loaded before
        the caller's callback releases startup admission.
        """
        with self._state_lock:
            if self._completed:
                return
            self._completed = True
            self._active = False
            dialog = self._dialog
            callback = self._finished_callback
            self._dialog = None
            self._transfer = None
            self._finished_callback = None

        try:
            if dialog is not None:
                dialog.destroy()
            if path is not None:
                self._geolocation.load(path)
        finally:
            if callback is not None:
                callback()
