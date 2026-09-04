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

"""The application: startup, activation, the startup dialogs, shutdown."""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio

from obozrenie import gtk_icons, helpers, i18n
from obozrenie.diagnostics import diagnostic_for_exception
from obozrenie.global_settings import APPLICATION_ID
from obozrenie.global_strings import GTK_MSG
from obozrenie.gtk_window import MainWindow
from obozrenie.presenter import MainPresenter


class App(Adw.Application):

    """App class."""

    def __init__(self, core_instance, settings_instance, services):
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)
        self.connect("shutdown", self.on_shutdown)

        self.core = core_instance
        self.settings = settings_instance
        self.services = services
        # The shutdown handler reads this whatever startup managed to do, so
        # it exists before the first thing that could fail.
        self.status = "starting"
        self.handler_failure = None
        self._pending_settings_warnings = []
        self._startup_dialogs_pending = False
        self.window = None
        self.presenter = None

    def on_startup(self, app):
        """Build the window and presenter, load settings, register actions.

        The presenter is attached to the window only once the loaded settings
        are shown, so no selection made while binding reaches it; the first
        rendering is then requested explicitly.
        """
        try:
            helpers.debug_msg([GTK_MSG, i18n._("Obozrenie is starting")])
            window = MainWindow(application=self)
            self.window = window
            icons = gtk_icons.game_icon_textures(self.core.game_table.get_game_set())
            presenter = MainPresenter(window, self.core, self.settings, icons)
            self.presenter = presenter

            presenter.load_games()
            self._pending_settings_warnings = self.settings.load()
            presenter.bind_settings()
            window.attach_presenter(presenter)
            presenter.render_loaded_state()

            self._add_action("about", self.on_about)
            self._add_action("quit", self.on_quit, "<Control>q")

            self.status = "up"
        except Exception as error:
            self._record_handler_failure(error)
            app.quit()

    def _add_action(self, name, handler, accelerator=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", handler)
        self.add_action(action)
        if accelerator is not None:
            self.set_accels_for_action(f"app.{name}", [accelerator])

    def on_about(self, *_args):
        self.window.present_about()

    def on_quit(self, *_args):
        self.quit()

    def _record_handler_failure(self, error: BaseException) -> None:
        """Record and report one failure a signal handler cannot raise.

        PyGObject prints and swallows whatever a `startup` or `shutdown`
        handler raises, so `run()` would report success for a settings file
        that never loaded or never saved and the launcher would exit 0.
        Recording the first failure here is what lets `main()` end the run
        with a failure status, and reporting it as its declared diagnostic
        keeps the exception's own text out of the log.

        Recording comes first and the report is best effort: the failure
        being recorded can be the output stream itself, and every caller does
        something after this call — quitting a failed startup, above all —
        that a second failure out of the report would skip, which is exactly
        what `helpers.log_safe_diagnostic` guarantees.
        """
        if self.handler_failure is None:
            self.handler_failure = diagnostic_for_exception(error)
        helpers.log_safe_diagnostic(GTK_MSG, error)

    def on_activate(self, app):
        """Present the window and, once, run the startup dialogs behind it.

        Startup admission is held from the first activation until the last
        warning has closed and the GeoIP coordinator reports the offer it
        accepted is finished, so an activation arriving meanwhile only
        presents the window again.
        """
        self.window.present()
        if self._startup_dialogs_pending:
            return

        pending_warnings = self._pending_settings_warnings
        self._pending_settings_warnings = []
        self._startup_dialogs_pending = True
        GLib.idle_add(self._show_startup_dialogs, list(pending_warnings))

    def _show_startup_dialogs(self, warnings):
        self._continue_startup_dialogs(warnings)
        return False

    def _continue_startup_dialogs(self, warnings):
        """Show the next warning, or offer GeoIP once every warning is closed.

        Each error dialog's close continues the chain, so the dialogs never
        stack; a dialog that cannot be presented releases admission and
        propagates, and an offer that never started promises no completion.
        """
        if warnings:
            warning, *rest = warnings
            try:
                self.window.present_error(
                    warning, lambda: self._continue_startup_dialogs(rest))
            except BaseException:
                self._release_startup_admission()
                raise
            return

        offered = False
        try:
            offered = self.services.geoip_downloads.offer(
                self.window, self._release_startup_admission)
        finally:
            if not offered:
                self._release_startup_admission()

    def _release_startup_admission(self):
        self._startup_dialogs_pending = False

    def on_shutdown(self, app):
        try:
            if self.status == "up":
                # Only the save decides how this run ends, so it alone is
                # recorded: a failing log line below is what PyGObject prints
                # and swallows, not a reason to exit non-zero.
                try:
                    self.settings.save()
                except Exception as error:
                    # A save that failed — a lock this copy could not take, a
                    # profile it could not write — is the run's outcome, so it
                    # is recorded rather than swallowed by the GTK dispatch.
                    self._record_handler_failure(error)
                else:
                    self.status = "shutting down"
                    helpers.debug_msg([GTK_MSG, i18n._("Shutting down")])
            else:
                self.status = "start failed"
                helpers.debug_msg(
                    [GTK_MSG, i18n._("Initialization failed. Aborting.")])
        finally:
            # However the settings save ends, every owned service is told to
            # close here; the launcher joins them against its own deadline.
            self.services.begin_close()
