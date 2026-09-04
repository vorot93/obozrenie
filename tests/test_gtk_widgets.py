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
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
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

from obozrenie import (
    core,
    game_table,
    geoip,
    gtk_dialogs,
    gtk_geoip,
    gtk_lists,
    gtk_preferences,
    gtk_window,
    presenter,
)
from obozrenie import settings as settings_module
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


class RecordingPresenter:
    """Record every notification the window sends; match every server."""

    def __init__(self):
        self.events = []
        self.matching = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # Every notification the window sends must name a real presenter
        # method, or this fake would record a typo as a passing test.
        assert hasattr(presenter.MainPresenter, name), name

        def record():
            self.events.append(name)
        return record

    def server_matches(self, row):
        return self.matching is None or row.host in self.matching


class _MainWindowTestCase(unittest.TestCase):
    def setUp(self):
        self.window = gtk_window.MainWindow()
        self.addCleanup(self._destroy_window)
        self.window.present()
        pump()

    def _destroy_window(self):
        self.window.destroy()
        pump()

    @staticmethod
    def game_rows():
        return [GameRow("q3a", "Quake III", texture()), GameRow("xonotic", "Xonotic", texture())]

    @staticmethod
    def server_rows():
        return [
            ServerRow(Server(endpoint=Endpoint("b.example", 1), game_id="q3a", name="b", ping=50, password=True)),
            ServerRow(Server(endpoint=Endpoint("a.example", 1), game_id="q3a", name="a", ping=10)),
            ServerRow(Server(endpoint=Endpoint("c.example", 1), game_id="q3a", name="c", ping=30, country="US")),
        ]

    def sorted_hosts(self):
        model = self.window.server_selection
        return [model.get_item(index).host for index in range(model.get_n_items())]


class MainWindowTemplateTests(_MainWindowTestCase):
    def test_template_builds_every_named_child(self):
        window = self.window
        self.assertIsInstance(window, Adw.ApplicationWindow)
        self.assertIsInstance(window.server_view, Gtk.ColumnView)
        self.assertIsInstance(window.game_list, Gtk.ListView)
        self.assertIsInstance(window.compact_game_dropdown, Gtk.DropDown)
        self.assertIsInstance(window.connect_game_dropdown, Gtk.DropDown)
        self.assertIsInstance(window.password_entry, Gtk.PasswordEntry)
        self.assertIsInstance(window.welcome_page, Adw.StatusPage)
        self.assertEqual(window.pages.get_visible_child_name(), "welcome")
        self.assertEqual(window.menu_button.get_menu_model().get_n_items(), 1)
        self.assertEqual(window.get_title(), "Obozrenie")
        self.assertIsNotNone(window.logo)

    def test_selectors_are_focusable_and_filter_labels_target_their_controls(self):
        window = self.window
        for widget in (window.game_list, window.compact_game_dropdown,
                       window.connect_game_dropdown, window.filter_secure):
            self.assertTrue(widget.get_focusable(), widget)
        relationships = (
            (window.filter_mod_label, window.filter_mod),
            (window.filter_type_label, window.filter_type),
            (window.filter_terrain_label, window.filter_terrain),
            (window.filter_ping_label, window.filter_ping),
            (window.filter_secure_label, window.filter_secure),
        )
        for label, target in relationships:
            with self.subTest(label=label.get_label()):
                self.assertIs(label.get_mnemonic_widget(), target)
                self.assertTrue(label.get_use_underline())
                self.assertIn("_", label.get_label())

    def test_pages_actions_error_text_and_sidebar_are_view_writes(self):
        window = self.window
        for name in presenter.PAGES:
            window.show_page(name)
            self.assertEqual(window.pages.get_visible_child_name(), name)
        window.set_error_text("exact error")
        self.assertEqual(window.error_page.get_description(), "exact error")
        buttons = {
            "preferences": window.preferences_button,
            "refresh": window.refresh_button,
            "connect": window.connect_button,
            "info": window.info_button,
        }
        for action in presenter.ACTIONS:
            window.set_action_enabled(action, False)
            self.assertFalse(buttons[action].get_sensitive())
            window.set_action_enabled(action, True)
            self.assertTrue(buttons[action].get_sensitive())
        window.show_sidebar(False)
        self.assertFalse(window.sidebar_shown())
        self.assertFalse(window.sidebar_revealer.get_reveal_child())
        self.assertTrue(window.compact_game_revealer.get_reveal_child())
        window.show_sidebar(True)
        self.assertTrue(window.sidebar_revealer.get_reveal_child())
        self.assertFalse(window.compact_game_revealer.get_reveal_child())
        window.filters_toggle.set_active(True)
        self.assertTrue(window.filters_revealer.get_reveal_child())

    def test_filter_values_read_every_control(self):
        window = self.window
        window.filter_mod.set_text("insta")
        window.filter_type.set_text("ctf")
        window.filter_terrain.set_text("q3")
        window.filter_ping.set_value(120)
        window.filter_secure.set_selected(2)
        window.filter_notfull.set_active(True)
        window.filter_nopassword.set_active(True)

        self.assertEqual(window.filter_values(), {
            "mod": "insta",
            "type": "ctf",
            "terrain": "q3",
            "ping": 120,
            "secure": "False",
            "notfull": True,
            "notempty": False,
            "nopassword": True,
        })
        self.assertEqual(set(window.filter_values()), set(presenter.FILTER_KEYS))

    def test_host_and_password_reads_and_writes(self):
        window = self.window
        window.set_host_text("h.example:1")
        window.password_entry.set_text("secret")
        self.assertEqual(window.host_text(), "h.example:1")
        self.assertEqual(window.password_text(), "secret")


class MainWindowGameTests(_MainWindowTestCase):
    def test_games_feed_the_sidebar_and_both_dropdowns_and_selection_syncs(self):
        window = self.window
        window.set_games(self.game_rows())
        pump()

        self.assertEqual(window.game_store.get_n_items(), 2)
        self.assertEqual(window.sidebar_selection.get_n_items(), 2)
        self.assertEqual(window.compact_game_dropdown.get_model().get_n_items(), 2)
        self.assertIsNone(window.browser_game())

        window.select_browser_game("xonotic")
        self.assertEqual(window.browser_game(), "xonotic")
        self.assertEqual(window.sidebar_selection.get_selected_item().game_id, "xonotic")
        self.assertEqual(window.compact_game_dropdown.get_selected_item().game_id, "xonotic")

        window.select_connect_game("q3a")
        self.assertEqual(window.connect_game(), "q3a")
        window.select_connect_game(None)
        self.assertEqual(window.connect_game(), "q3a")

    def test_set_games_with_a_presenter_attached_does_not_notify_or_reassign_the_browser_game(self):
        window = self.window
        recorder = RecordingPresenter()
        window.attach_presenter(recorder)
        window.select_browser_game("q3a")

        window.set_games(self.game_rows())
        pump()

        self.assertEqual(recorder.events, [])
        self.assertEqual(window.browser_game(), "q3a")

    def test_search_filters_the_sidebar_without_losing_the_browser_game(self):
        window = self.window
        window.set_games(self.game_rows())
        window.select_browser_game("q3a")

        window.game_search.set_text("xon")
        window.game_search.emit("search-changed")
        pump()
        self.assertEqual(window.sidebar_selection.get_n_items(), 1)
        self.assertEqual(window.browser_game(), "q3a")

        window.game_search.set_text("")
        window.game_search.emit("search-changed")
        pump()
        self.assertEqual(window.sidebar_selection.get_n_items(), 2)
        self.assertEqual(window.sidebar_selection.get_selected_item().game_id, "q3a")

    def test_user_selection_reports_one_browser_change_and_syncs_the_other_selector(self):
        window = self.window
        recorder = RecordingPresenter()
        window.set_games(self.game_rows())
        window.select_browser_game("q3a")
        window.attach_presenter(recorder)

        window.sidebar_selection.set_selected(1)
        pump()
        self.assertEqual(recorder.events, ["browser_game_changed"])
        self.assertEqual(window.browser_game(), "xonotic")
        self.assertEqual(window.compact_game_dropdown.get_selected_item().game_id, "xonotic")

        window.compact_game_dropdown.set_selected(0)
        pump()
        self.assertEqual(recorder.events, ["browser_game_changed", "browser_game_changed"])
        self.assertEqual(window.browser_game(), "q3a")
        self.assertEqual(window.sidebar_selection.get_selected_item().game_id, "q3a")

    def test_programmatic_writes_do_not_notify_but_user_edits_do(self):
        window = self.window
        recorder = RecordingPresenter()
        window.set_games(self.game_rows())
        window.attach_presenter(recorder)

        window.select_browser_game("xonotic")
        window.select_connect_game("q3a")
        window.set_host_text("h.example:1")
        window.show_sidebar(False)
        pump()
        self.assertEqual(recorder.events, [])

        window.host_entry.set_text("typed.example:2")
        window.connect_game_dropdown.set_selected(1)
        window.sidebar_toggle.set_active(True)
        window.filter_mod.set_text("x")
        window.refresh_button.emit("clicked")
        window.preferences_button.emit("clicked")
        window.info_button.emit("clicked")
        window.connect_button.emit("clicked")
        pump()
        self.assertEqual(recorder.events, [
            "connect_data_changed",
            "connect_data_changed",
            "sidebar_toggled",
            "filters_changed",
            "refresh_requested",
            "preferences_requested",
            "info_requested",
            "connect_requested",
        ])


class MainWindowServerTests(_MainWindowTestCase):
    def test_servers_sort_by_ping_then_by_the_chosen_column(self):
        window = self.window
        window.set_servers(self.server_rows())
        pump()

        self.assertEqual(self.sorted_hosts(), ["a.example:1", "c.example:1", "b.example:1"])

        window.server_view.sort_by_column(window.name_column, Gtk.SortType.DESCENDING)
        pump()
        self.assertEqual(self.sorted_hosts(), ["c.example:1", "b.example:1", "a.example:1"])

    def test_filter_asks_the_presenter_and_refilter_applies_a_change(self):
        window = self.window
        recorder = RecordingPresenter()
        window.attach_presenter(recorder)
        window.set_servers(self.server_rows())
        pump()
        self.assertEqual(len(self.sorted_hosts()), 3)

        recorder.matching = {"a.example:1"}
        window.refilter()
        pump()
        self.assertEqual(self.sorted_hosts(), ["a.example:1"])

    def test_selection_reports_and_refill_clears_it(self):
        window = self.window
        recorder = RecordingPresenter()
        window.attach_presenter(recorder)
        window.set_servers(self.server_rows())
        pump()
        self.assertIsNone(window.selected_server())

        window.server_selection.set_selected(0)
        pump()
        self.assertEqual(recorder.events, ["server_selected"])
        self.assertEqual(window.selected_server().host, "a.example:1")

        window.set_servers(self.server_rows()[:1])
        pump()
        self.assertIsNone(window.selected_server())
        # A presenter-requested refill drops the selection silently: still
        # just the one server_selected from the user's earlier selection.
        self.assertEqual(recorder.events, ["server_selected"])

        window.server_view.emit("activate", 0)
        self.assertEqual(recorder.events[-1], "server_activated")


class MainWindowDialogTests(_MainWindowTestCase):
    def test_view_dialogs_present_on_the_window(self):
        window = self.window
        closes = []
        error = gtk_dialogs.present_error(window, "x", lambda: closes.append(True))
        pump()
        error.force_close()

        window.present_error("boom", lambda: closes.append("boom"))
        pump()
        details = presenter.ServerDetails("n", "h.example:1", "g", "t", "1 / 2", "3", ())
        window.present_server_info(details, lambda: None)
        pump()
        window.present_preferences("t", (), lambda values: None)
        pump()
        about = window.present_about()
        pump()
        self.assertIs(about.get_logo(), window.logo)
        about.close()
        pump()


class ApplicationStartupTests(unittest.TestCase):
    """The App's startup order over a real window, presenter, core, settings.

    `tests.test_gtk_behavior` proves that order against mocks; this proves
    what the user actually sees once it has run for real.
    """

    GAMES = {
        "browser": {
            "name": "Browser Game",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": ["path"],
        },
        "connect": {
            "name": "Connect Game",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": ["path"],
        },
    }
    BUNDLED_DEFAULTS = {
        ("settings", "defaults", "settings.toml"): b"""[common]
selected-game-connect = "browser"
selected-game-browser = "browser"
server-host = ""
game-listview = true
""",
        ("settings", "defaults", "games.toml"): b"""[browser]
path = "browser-client"

[connect]
path = "connect-client"
""",
    }
    # Deliberately crossed, so a selection that reached the wrong selector
    # cannot pass: the browser shows the Connect game and the other way round.
    SAVED_COMMON = (
        '[common]\n'
        'selected-game-browser = "connect"\n'
        'selected-game-connect = "browser"\n'
        'server-host = "saved.example:30000"\n'
        'game-listview = false\n'
    )

    def make_settings(self, table, root):
        """A real `Settings` over a temporary profile with valid user files."""
        def load_resource(*parts):
            with io.BytesIO(self.BUNDLED_DEFAULTS[tuple(parts)]) as defaults:
                return tomllib.load(defaults)

        profile = Path(root) / "profile"
        profile.mkdir()
        settings = settings_module.Settings(
            table, str(profile), resource_loader=load_resource)
        Path(settings.user_common_settings_path).write_text(
            self.SAVED_COMMON, encoding="utf-8")
        Path(settings.user_game_settings_path).write_text(
            '[browser]\npath = "saved-browser-client"\n', encoding="utf-8")
        return settings

    def test_startup_shows_the_welcome_page_and_the_saved_selections(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        table = game_table.GameTable(self.GAMES)
        settings = self.make_settings(table, directory)
        instance = core.Core(table, geoip.Geolocation())
        self.addCleanup(instance.begin_close)
        window = gtk_window.MainWindow()
        self.addCleanup(window.destroy)
        main_presenter = presenter.MainPresenter(window, instance, settings, {})

        # Exactly what `App.on_startup` runs, in its order.
        main_presenter.load_games()
        warnings = settings.load()
        main_presenter.bind_settings()
        window.attach_presenter(main_presenter)
        main_presenter.render_loaded_state()
        window.present()
        pump()

        self.assertEqual(warnings, [])
        self.assertEqual(window.pages.get_visible_child_name(), "welcome")
        self.assertEqual(window.browser_game(), "connect")
        self.assertEqual(
            window.sidebar_selection.get_selected_item().game_id, "connect")
        self.assertEqual(
            window.compact_game_dropdown.get_selected_item().game_id, "connect")
        self.assertEqual(window.connect_game(), "browser")
        self.assertEqual(window.host_text(), "saved.example:30000")
        self.assertFalse(window.sidebar_shown())
        # Startup queries nothing: no game has left EMPTY.
        for game in self.GAMES:
            with self.subTest(game=game):
                self.assertIs(
                    table.get_query_status(game), game_table.QueryStatus.EMPTY)


class LocalizedTemplateTests(unittest.TestCase):
    def test_russian_environment_translates_the_templates_before_gtk_parses_them(self):
        script = """
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
Adw.init()
from obozrenie import gtk_window
window = gtk_window.MainWindow()
menu = window.menu_button.get_menu_model()
section = menu.get_item_link(0, "section")
print(window.name_column.get_title())
print(section.get_item_attribute_value(0, "label", None).get_string())
print(window.welcome_page.get_title())
"""
        environment = os.environ.copy()
        environment.update({"LANGUAGE": "ru_RU", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""})
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["Имя", "О программе", "Добро пожаловать в Обозрение!"],
        )


if __name__ == "__main__":
    unittest.main()
