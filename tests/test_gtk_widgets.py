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

"""Real widgets under a real display: templates, factories, dialogs.

This module refuses to import without a display rather than skipping, so a
run that cannot build widgets fails visibly.
"""

import base64
import unittest

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

if Gdk.Display.get_default() is None:
    raise RuntimeError(
        "tests.test_gtk_widgets builds real widgets and needs a display: "
        "run the suite inside a desktop session or under `xvfb-run -a`."
    )
Adw.init()
Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

# PyGObject lazily imports its asyncio integration on the first
# `GLib.MainContext.iteration()` call and, on Python 3.14, that import
# touches asyncio's soon-to-be-removed event-loop-policy API, which warns.
# The same lookup then repeats on every later iteration because it wraps the
# deprecated call in an empty `warnings.catch_warnings()` block, whose exit
# resets the "already shown" registry and lets it reprint — many times over
# under unittest's own run, which turns DeprecationWarning back on. Priming
# the cache here, through the public `get_event_loop_for_context`, makes
# every later iteration hit that cache instead, so this happens once, now,
# under Python's ordinary (silencing) import-time filters.
from gi.events import GLibEventLoopPolicy
GLibEventLoopPolicy().get_event_loop_for_context(GLib.MainContext.default())

from obozrenie import gtk_dialogs, gtk_geoip, gtk_lists, gtk_preferences, presenter
from obozrenie.list_rows import GameRow, PlayerRow, ServerRow
from obozrenie.models import Endpoint, Player, Server


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def pump():
    """Run every pending main-context source, then return."""
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def texture():
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(ONE_PIXEL_PNG))


class _FakeListItem(GObject.Object):
    """What a factory sees of a Gtk.ListItem: its child and its item."""

    def __init__(self, item=None):
        super().__init__()
        self.item = item
        self.child = None

    def set_child(self, child):
        self.child = child

    def get_child(self):
        return self.child

    def get_item(self):
        return self.item


class _WindowTestCase(unittest.TestCase):
    """A presented parent window for dialogs, destroyed after each test."""

    def setUp(self):
        self.window = Adw.Window()
        self.window.present()
        pump()
        self.addCleanup(self._destroy_window)

    def _destroy_window(self):
        self.window.destroy()
        pump()

    @staticmethod
    def closed_flag(dialog):
        closed = []
        dialog.connect("closed", lambda _dialog: closed.append(True))
        return closed


class ListFactoryTests(unittest.TestCase):
    def test_label_factory_binds_text_and_alignment(self):
        item = _FakeListItem(PlayerRow(Player("Ada", None, 30)))
        factory = gtk_lists.label_factory(lambda row: "" if row.score is None else str(row.score), xalign=1)

        factory.emit("setup", item)
        factory.emit("bind", item)

        self.assertIsInstance(item.child, Gtk.Label)
        self.assertEqual(item.child.get_text(), "")
        self.assertEqual(item.child.get_xalign(), 1)

    def test_server_name_cell_shows_icon_lock_shield_and_name(self):
        icon = texture()
        server = Server(endpoint=Endpoint("h.example", 1), game_id="g", name="Locked", password=True, secure=False)
        item = _FakeListItem(ServerRow(server, icon))

        gtk_lists.setup_server_name_cell(item)
        gtk_lists.bind_server_name_cell(item)

        image, lock, shield, label = gtk_lists.box_children(item.child)
        self.assertIs(image.get_paintable(), icon)
        self.assertEqual((lock.get_icon_name(), lock.get_visible()), (gtk_lists.LOCK_ICON, True))
        self.assertEqual((shield.get_icon_name(), shield.get_visible()), (gtk_lists.SECURE_ICON, False))
        self.assertEqual(label.get_text(), "Locked")

    def test_players_text_formats_count_and_limit(self):
        row = ServerRow(Server(endpoint=Endpoint("h.example", 1), game_id="g", player_count=3, player_limit=16))
        self.assertEqual(gtk_lists.players_text(row), "3 / 16")

    def test_game_cell_binds_icon_name_and_follows_the_status_icon(self):
        icon = texture()
        row = GameRow("q3a", "Quake III", icon)
        item = _FakeListItem(row)

        gtk_lists.setup_game_cell(item, show_status=True)
        gtk_lists.bind_game_cell(item, show_status=True)
        image, label, status = gtk_lists.box_children(item.child)

        self.assertIs(image.get_paintable(), icon)
        self.assertEqual(label.get_text(), "Quake III")
        self.assertIsNone(status.get_icon_name())
        row.status_icon = "emblem-ok-symbolic"
        self.assertEqual(status.get_icon_name(), "emblem-ok-symbolic")
        row.status_icon = None
        self.assertIsNone(status.get_icon_name())
        row.status_icon = "emblem-ok-symbolic"
        self.assertEqual(status.get_icon_name(), "emblem-ok-symbolic")

        gtk_lists.unbind_game_cell(item)
        row.status_icon = "dialog-error-symbolic"
        self.assertEqual(status.get_icon_name(), "emblem-ok-symbolic")

        # A recycled cell rebinds to a different row; the new row's own
        # status (None included) replaces whatever the old row last showed,
        # and the old row's later changes no longer reach the image.
        idle_row = GameRow("q4", "Quake 4", None)
        item.item = idle_row
        gtk_lists.bind_game_cell(item, show_status=True)
        self.assertIsNone(status.get_icon_name())

        gtk_lists.unbind_game_cell(item)
        working_row = GameRow("qw", "QuakeWorld", None)
        working_row.status_icon = "emblem-synchronizing-symbolic"
        item.item = working_row
        gtk_lists.bind_game_cell(item, show_status=True)
        self.assertEqual(status.get_icon_name(), "emblem-synchronizing-symbolic")

        idle_row.status_icon = "dialog-error-symbolic"
        self.assertEqual(status.get_icon_name(), "emblem-synchronizing-symbolic")

    def test_game_cell_without_status_has_two_children(self):
        item = _FakeListItem(GameRow("q3a", "Quake III", None))
        gtk_lists.setup_game_cell(item, show_status=False)
        gtk_lists.bind_game_cell(item, show_status=False)
        self.assertEqual(len(gtk_lists.box_children(item.child)), 2)


class ErrorDialogTests(_WindowTestCase):
    def test_error_dialog_shows_the_message_and_reports_its_close(self):
        closes = []
        dialog = gtk_dialogs.present_error(self.window, "Launch failed", lambda: closes.append(True))
        pump()

        self.assertIsInstance(dialog, Adw.AlertDialog)
        self.assertEqual(dialog.get_heading(), "Error")
        self.assertEqual(dialog.get_body(), "Launch failed")
        self.assertEqual(dialog.get_close_response(), "close")
        self.assertEqual(dialog.get_default_response(), "close")

        dialog.emit("response", "close")
        self.assertEqual(closes, [True])
        dialog.force_close()
        pump()

    def test_error_dialog_without_a_callback_still_presents(self):
        dialog = gtk_dialogs.present_error(self.window, "plain")
        pump()
        self.assertEqual(dialog.get_body(), "plain")
        dialog.force_close()
        pump()


class AboutDialogTests(_WindowTestCase):
    def test_about_dialog_uses_the_texture_logo_and_project_metadata(self):
        logo = texture()

        about = gtk_dialogs.present_about(self.window, logo)
        pump()

        self.assertIs(about.get_logo(), logo)
        self.assertEqual(about.get_program_name(), "Obozrenie")
        self.assertEqual(about.get_license_type(), Gtk.License.GPL_3_0)
        self.assertIs(about.get_transient_for(), self.window)
        self.assertIs(about.get_modal(), True)
        self.assertIs(about.get_hide_on_close(), False)
        about.close()
        pump()


class ServerInfoDialogTests(_WindowTestCase):
    def details(self):
        return presenter.ServerDetails(
            name="Details",
            host="[2001:db8::50]:30000",
            game_name="Game",
            terrain="Map",
            players_text="1 / 16",
            ping_text="21",
            players=(PlayerRow(Player("first", 7, 20)), PlayerRow(Player("second", None, 30))),
        )

    def test_dialog_shows_every_detail_and_lists_the_players(self):
        dialog = gtk_dialogs.ServerInfoDialog(self.details(), lambda: None)
        dialog.present(self.window)
        pump()

        self.assertEqual(dialog.name_value.get_text(), "Details")
        self.assertEqual(dialog.host_value.get_text(), "[2001:db8::50]:30000")
        self.assertEqual(dialog.game_value.get_text(), "Game")
        self.assertEqual(dialog.terrain_value.get_text(), "Map")
        self.assertEqual(dialog.players_value.get_text(), "1 / 16")
        self.assertEqual(dialog.ping_value.get_text(), "21")
        self.assertEqual(dialog.players.get_n_items(), 2)
        self.assertEqual(
            [dialog.players.get_item(index).score for index in range(2)],
            [7, None],
        )
        dialog.force_close()
        pump()

    def test_connect_closes_then_calls_back_and_close_only_closes(self):
        connects = []
        dialog = gtk_dialogs.ServerInfoDialog(self.details(), lambda: connects.append(True))
        dialog.present(self.window)
        pump()
        closed = self.closed_flag(dialog)

        dialog.connect_button.emit("clicked")
        pump()
        self.assertEqual(connects, [True])
        self.assertEqual(closed, [True])

        other = gtk_dialogs.ServerInfoDialog(self.details(), lambda: connects.append(True))
        other.present(self.window)
        pump()
        other_closed = self.closed_flag(other)
        other.close_button.emit("clicked")
        pump()
        self.assertEqual(connects, [True])
        self.assertEqual(other_closed, [True])


class PreferencesDialogTests(_WindowTestCase):
    def fields(self):
        return (
            presenter.PreferenceField("path", presenter.OptionKind.TEXT, "Game path", "Path to the game.", "client"),
            presenter.PreferenceField("master_uri", presenter.OptionKind.LINES, "Master URI list", "Masters.", "a://1\nb://2"),
            presenter.PreferenceField("steam_launch", presenter.OptionKind.FLAG, "Launch via Steam", "Steam.", True),
        )

    def test_rows_show_the_fields_and_save_reports_every_edited_value(self):
        saved = []
        dialog = gtk_preferences.PreferencesDialog("Game preferences", self.fields(), saved.append)
        dialog.present(self.window)
        pump()

        self.assertEqual(dialog.get_title(), "Game preferences")
        self.assertIsInstance(dialog.rows["path"], Adw.EntryRow)
        self.assertIsInstance(dialog.rows["steam_launch"], Adw.SwitchRow)
        self.assertIsInstance(dialog.rows["master_uri"], Gtk.TextView)
        self.assertEqual(dialog.rows["path"].get_text(), "client")
        self.assertIs(dialog.rows["steam_launch"].get_active(), True)
        buffer = dialog.rows["master_uri"].get_buffer()
        self.assertEqual(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True), "a://1\nb://2")

        dialog.rows["path"].set_text("/opt/client")
        dialog.rows["steam_launch"].set_active(False)
        buffer.set_text("c://3\nd://4")
        closed = self.closed_flag(dialog)
        dialog.save_button.emit("clicked")
        pump()

        self.assertEqual(saved, [{"path": "/opt/client", "master_uri": "c://3\nd://4", "steam_launch": False}])
        self.assertEqual(closed, [True])

    def test_closing_without_save_discards(self):
        saved = []
        dialog = gtk_preferences.PreferencesDialog("Game preferences", self.fields(), saved.append)
        dialog.present(self.window)
        pump()
        dialog.rows["path"].set_text("changed")

        dialog.close()
        pump()

        self.assertEqual(saved, [])


class GeoIPDialogTests(_WindowTestCase):
    def test_question_routes_download_and_anything_else(self):
        events = []
        dialogs = gtk_geoip.GeoIPOfferDialogs()

        question = dialogs.present_question(self.window, lambda: events.append("accept"), lambda: events.append("decline"))
        pump()

        dialog = question.dialog
        self.assertEqual(dialog.get_heading(), "Download GeoIP database?")
        self.assertEqual(dialog.get_close_response(), "cancel")
        self.assertEqual(dialog.get_default_response(), "download")
        self.assertEqual(dialog.get_response_appearance("download"), Adw.ResponseAppearance.SUGGESTED)
        dialog.emit("response", "download")
        dialog.emit("response", "cancel")
        self.assertEqual(events, ["accept", "decline"])

        closed = self.closed_flag(dialog)
        question.dismiss()
        pump()
        self.assertEqual(closed, [True])
        self.assertEqual(events, ["accept", "decline"])

    def test_transfer_dialog_reports_user_closes_but_not_its_own(self):
        cancels = []
        dialogs = gtk_geoip.GeoIPOfferDialogs()

        dialog = dialogs.present_transfer(self.window, lambda: cancels.append(True))
        pump()

        self.assertEqual(dialog.get_title(), "Downloading GeoIP database…")
        self.assertIs(dialog.get_can_close(), False)
        dialog.set_fraction(0.5)
        self.assertEqual(dialog.progress.get_fraction(), 0.5)
        dialog.pulse()

        dialog.cancel_button.emit("clicked")
        dialog.emit("close-attempt")
        self.assertEqual(cancels, [True, True])

        closed = self.closed_flag(dialog)
        dialog.close()
        dialog.close()
        pump()
        self.assertEqual(closed, [True])
        self.assertEqual(cancels, [True, True])


if __name__ == "__main__":
    unittest.main()
