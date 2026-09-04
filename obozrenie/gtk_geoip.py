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

"""The GeoIP database offer: one question, one transfer, one shutdown."""

import threading
import time
from collections.abc import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from obozrenie import geoip, i18n
from obozrenie.network_calls import validate_deadline


_GEOIP_WORKER_THREAD_NAME = "obozrenie-geoip-download"


class QuestionDialog:

    """Handle to the presented question: dismiss it without a response."""

    def __init__(self, dialog: Adw.AlertDialog, response_handler: int) -> None:
        self.dialog = dialog
        self._response_handler = response_handler

    def dismiss(self) -> None:
        # `close()` would emit the close response, and unlike a plain
        # `Adw.Dialog`, force-closing an `Adw.AlertDialog` still emits
        # "response" with it — so the handler is disconnected first;
        # dismissing answers nothing, which is the point when the
        # application is shutting down.
        self.dialog.disconnect(self._response_handler)
        self.dialog.force_close()


class TransferDialog(Adw.Dialog):

    """The running transfer: a progress bar and one way to cancel.

    `can-close` is off, so Escape, the header close button, and a close
    request all arrive as `close-attempt` and cancel the transfer; only this
    handle's `close()` — the coordinator's own — closes the dialog, and it
    does so without reporting a cancellation.
    """

    def __init__(self, on_cancel: Callable[[], None]) -> None:
        super().__init__(
            title=i18n._("Downloading GeoIP database…"),
            can_close=False,
            content_width=360,
        )
        self._on_cancel = on_cancel
        self._closed = False

        self.progress = Gtk.ProgressBar(show_text=True)
        self.cancel_button = Gtk.Button(label=i18n._("Cancel"), halign=Gtk.Align.END)
        self.cancel_button.connect("clicked", self._cancel)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("start", "end", "top", "bottom"):
            content.set_property(f"margin-{edge}", 24)
        content.append(self.progress)
        content.append(self.cancel_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)
        self.set_child(toolbar)
        self.connect("close-attempt", self._cancel)

    def _cancel(self, *_args) -> None:
        self._on_cancel()

    def set_fraction(self, fraction: float) -> None:
        self.progress.set_fraction(fraction)

    def pulse(self) -> None:
        self.progress.pulse()

    def close(self) -> None:
        """Close for good; repeated closes are no-ops."""
        if self._closed:
            return
        self._closed = True
        self.force_close()


class GeoIPOfferDialogs:

    """The two dialogs the coordinator drives, built and presented on demand."""

    def present_question(
        self,
        parent: Gtk.Widget,
        on_accept: Callable[[], None],
        on_decline: Callable[[], None],
    ) -> QuestionDialog:
        dialog = Adw.AlertDialog(
            heading=i18n._("Download GeoIP database?"),
            body=i18n._("Server geolocation needs a country database. "
                        "Download it now (a few megabytes)?"),
        )
        dialog.add_response("cancel", i18n._("Not now"))
        dialog.add_response("download", i18n._("Download"))
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("download")
        dialog.set_close_response("cancel")

        def respond(_dialog, response):
            if response == "download":
                on_accept()
            else:
                on_decline()

        response_handler = dialog.connect("response", respond)
        dialog.present(parent)
        return QuestionDialog(dialog, response_handler)

    def present_transfer(
        self,
        parent: Gtk.Widget,
        on_cancel: Callable[[], None],
    ) -> TransferDialog:
        dialog = TransferDialog(on_cancel)
        dialog.present(parent)
        return dialog


class GeoIPDownloadCoordinator:

    """Owner of the GeoIP offer, its transfer, and their shutdown.

    One offer is in flight at a time. `offer` reserves this coordinator under
    its state lock and reports whether it presented the question, so a
    repeated activation can neither stack a second question nor start a
    second transfer. Only a presented offer promises a completion callback,
    and that callback runs once the transfer dialog has been closed and any
    downloaded database has been loaded, which is what keeps startup
    admission held until then.

    The presented question is never coordinator state: it answers itself
    through `_accept` or `_decline`, so only the call that presented it —
    which alone may have to dismiss it, when close wins that race — ever
    holds it. The state lock publishes the transfer dialog, transfer,
    worker, and completion flags that close and the worker both read:
    dialog work, the caller's callback, and thread joins never run under
    it. `begin_close` marks this coordinator closing and cancels the
    transfer, which closes the response its worker may be reading. The
    worker re-checks that flag before it queues anything, and every queued
    callback re-checks it again, so a transfer that outlives close can
    neither load a database nor touch a dialog.
    """

    def __init__(self, geolocation: geoip.Geolocation, dialogs=None) -> None:
        self._geolocation = geolocation
        self._dialogs = GeoIPOfferDialogs() if dialogs is None else dialogs
        self._state_lock = threading.Lock()
        self._parent = None
        self._transfer_dialog = None
        self._transfer = None
        self._worker = None
        self._closing = False
        self._active = False
        self._completed = False
        self._finished_callback = None

    def offer(
        self,
        parent: Gtk.Widget,
        finished_callback: Callable[[], None],
    ) -> bool:
        """Present one GeoIP question; report whether this call started it.

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
            self._parent = parent
            self._finished_callback = finished_callback

        presented = False
        try:
            question = self._dialogs.present_question(
                parent, self._accept, self._decline)
            with self._state_lock:
                closing = self._closing
            if closing:
                # Close arrived while the question was being presented. It is
                # this call's to dismiss, and it promised no callback.
                question.dismiss()
                return False
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
            self._parent = None
            self._transfer_dialog = None
            self._transfer = None
            self._finished_callback = None

    def _accept(self) -> None:
        """Start this offer's one transfer on its own daemon worker.

        The question closed itself on its response. The transfer dialog is
        presented first and the transfer is built immediately before the
        worker that runs it, so its window opens with the work.
        """
        with self._state_lock:
            if self._closing or not self._active or self._transfer is not None:
                # Neither close nor a repeated response starts a transfer.
                return
            parent = self._parent

        dialog = self._dialogs.present_transfer(parent, self._decline)
        with self._state_lock:
            closing = self._closing
            if not closing:
                self._transfer_dialog = dialog
        if closing:
            dialog.close()
            return

        transfer = geoip.GeoIPDatabaseDownload(
            geoip.CACHE_DB_PATH,
            time.monotonic() + geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )
        worker = threading.Thread(
            target=self._run_transfer,
            args=(transfer,),
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
            dialog.close()
            return

        try:
            worker.start()
        except RuntimeError:
            # No worker will report this transfer, so this response completes
            # the offer itself rather than stranding startup admission.
            transfer.cancel()
            self._complete(None)

    def _decline(self) -> None:
        """Finish a declined offer, stopping a transfer it already started."""
        with self._state_lock:
            transfer = self._transfer
            dialog = self._transfer_dialog
        if transfer is None:
            self._complete(None)
            return
        # A running transfer keeps exactly one completion owner: its worker.
        # Cancelling returns that worker at once, and closing the dialog gives
        # the same immediate feedback completion would.
        transfer.cancel()
        if dialog is not None:
            dialog.close()

    def _run_transfer(self, transfer) -> None:
        """Run one transfer to completion on this coordinator's worker."""
        path = None
        try:
            path = transfer.run(
                lambda downloaded, total: self._dispatch(
                    self._apply_progress, downloaded, total))
        finally:
            self._dispatch(self._finish_transfer, path)

    def _dispatch(self, callback, *args) -> None:
        """Queue one main-thread callback unless close has already begun."""
        with self._state_lock:
            if self._closing:
                return
        # Queueing is not the coordinator's own state, and the callback it
        # queues re-checks close, so it never runs under the state lock.
        GLib.idle_add(callback, *args)

    def _apply_progress(self, downloaded, total) -> bool:
        """Render one progress update, unless close has begun meanwhile."""
        with self._state_lock:
            if self._closing:
                return False
            dialog = self._transfer_dialog
        if dialog is None:
            return False
        if total > 0:
            dialog.set_fraction(downloaded / total)
        else:
            dialog.pulse()
        return False

    def _finish_transfer(self, path) -> bool:
        """Complete the accepted offer, unless close has begun meanwhile."""
        with self._state_lock:
            if self._closing:
                return False
        self._complete(path)
        return False

    def _complete(self, path) -> None:
        """Finish one offer exactly once, on the GTK main thread.

        The dialogs are closed and any transferred database is loaded before
        the caller's callback releases startup admission.
        """
        with self._state_lock:
            if self._completed:
                return
            self._completed = True
            self._active = False
            dialog = self._transfer_dialog
            callback = self._finished_callback
            self._parent = None
            self._transfer_dialog = None
            self._transfer = None
            self._finished_callback = None

        try:
            if dialog is not None:
                dialog.close()
            if path is not None:
                self._geolocation.load(path)
        finally:
            if callback is not None:
                callback()
