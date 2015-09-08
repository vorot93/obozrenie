#!/usr/bin/python
# This source file is part of Obozrenie
# Copyright 2015 Artem Vorotnikov

# For more information, see https://github.com/skybon/obozrenie

# Obozrenie is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.

# Obozrenie is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Obozrenie.  If not, see <http://www.gnu.org/licenses/>.

"""Simple and easy to use game server browser."""


import argparse
import ast
import os
import shutil
import threading

from obozrenie import N_

from gi.repository import GdkPixbuf, Gtk, Gio, GLib

from obozrenie import helpers
from obozrenie import gtk_helpers
from obozrenie.core import Core, Settings
import obozrenie.gtk_templates as templates
from obozrenie.global_settings import *


class GUIActions:

    """Responses to events from GUI"""

    def __init__(self, app, builder, core_library):
        self.app = app
        self.builder = builder
        self.core = core_library

        self.main_window = self.builder.get_object("Main_Window")

        self.game_combobox = self.builder.get_object("Game_ComboBox")
        self.game_treeview = self.builder.get_object("Game_TreeView")
        self.game_model = self.builder.get_object("Game_Store")
        self.game_combobox_revealer = self.builder.get_object("Game_ComboBox_Revealer")
        self.game_view_revealer = self.builder.get_object("Game_View_Revealer")

        self.game_view_togglebutton = self.builder.get_object("Game_View_ToggleButton")
        self.serverlist_update_button = self.builder.get_object("Update_Button")
        self.serverlist_info_button = self.builder.get_object("Info_Button")
        self.serverlist_connect_button = self.builder.get_object("Connect_Button")

        self.serverlist_model = self.builder.get_object("ServerList_Store")
        self.serverlist_view = self.builder.get_object("ServerList_View")
        self.serverlist_view_selection = self.builder.get_object("ServerList_View").get_selection()

        self.serverlist_notebook = self.builder.get_object("ServerList_Notebook")

        self.serverlist_scrolledwindow = self.builder.get_object("ServerList_ScrolledWindow")
        self.welcome_label = self.builder.get_object("ServerList_Welcome_Label")
        self.refresh_spinner = self.builder.get_object("ServerList_Refresh_Spinner")

        self.serverlist_notebook_servers_page = self.serverlist_notebook.page_num(self.serverlist_scrolledwindow)
        self.serverlist_notebook_welcome_page = self.serverlist_notebook.page_num(self.welcome_label)
        self.serverlist_notebook_loading_page = self.serverlist_notebook.page_num(self.refresh_spinner)

        self.serverhost_entry = self.builder.get_object("ServerHost_Entry")

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_welcome_page)

    def cb_set_widgets_active(self, status):
        """Sets sensitivity and visibility for conditional widgets."""
        pass

    def cb_game_preferences_button_clicked(self, combobox, *data):
        game = self.app.settings.settings_table["common"]["selected-game"]
        prefs_dialog = templates.PreferencesDialog(self.main_window,
                                                   game,
                                                   self.core.game_table,
                                                   self.app.settings.dynamic_widget_table,
                                                   callback_start=self.apply_settings_to_preferences_dialog,
                                                   callback_close=self.update_game_settings_table)
        prefs_dialog.run()
        prefs_dialog.destroy()

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        pass

    def cb_connect_button_clicked(self, *args):
        """Starts the game."""
        game = self.app.settings.settings_table["common"]["selected-game"]
        server = self.app.settings.settings_table["common"]["server-host"]
        password = self.app.settings.settings_table["common"]["server-pass"]

        self.core.start_game(game, server, password)

    @staticmethod
    def cb_about(action, dialog, parent, *args):
        """Opens the About dialog."""
        about_dialog = templates.AboutDialog(parent, PROJECT, DESCRIPTION, WEBSITE, VERSION, AUTHORS, ARTISTS, COPYRIGHT, Gtk.License.GPL_3_0, None)
        about_dialog.run()
        about_dialog.destroy()

    def cb_quit(self, *args):
        """Exits the program."""
        self.app.quit()

    def cb_game_combobox_changed(self, widget, *data):
        """Actions on game combobox selection change."""
        game_id = gtk_helpers.get_widget_value(widget)
        gtk_helpers.set_widget_value(self.game_treeview, game_id, treeview_colnum=self.game_view_format.index("game_id"))

    def cb_game_treeview_togglebutton_clicked(self, widget, *data):
        """Switches between TreeView and ComboBox game selection."""
        if self.game_view_togglebutton.get_active() is True:
            self.game_combobox_revealer.set_reveal_child(False)
            self.game_view_revealer.set_reveal_child(True)
        else:
            self.game_combobox_revealer.set_reveal_child(True)
            self.game_view_revealer.set_reveal_child(False)

    def cb_game_treeview_selection_changed(self, widget, *data):
        game_id = self.app.settings.settings_table["common"]["selected-game"]

        gtk_helpers.set_widget_value(self.game_combobox, game_id)
        if self.core.game_table[game_id]["servers"] == []:
            self.cb_update_button_clicked(widget, *data)
        else:
            self.fill_server_view(self.core.game_table[game_id]["servers"])

    def cb_update_button_clicked(self, widget, *data):
        """Actions on server list update button click"""
        ping_button = self.builder.get_object("PingingEnable_CheckButton")
        ping_column = self.builder.get_object("Ping_ServerList_TreeViewColumn")

        game = self.app.settings.settings_table["common"]["selected-game"]

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_loading_page)

        self.serverlist_update_button.set_sensitive(False)
        self.game_treeview.set_sensitive(False)
        self.game_combobox.set_sensitive(False)

        self.core.update_server_list(game, self.fill_server_view)

    def get_games_list_store(self):
        """
        Loads game list into a list store
        """
        self.game_view_format = ("game_id",
                                 "name",
                                 "backend")

        table = self.core.game_table.copy()

        self.game_store = self.builder.get_object("Game_Store")

        game_store_table = []
        for entry in self.core.game_table:
            game_store_table.append({})
            game_store_table[-1]["game_id"] = entry
            game_store_table[-1]["name"] = self.core.game_table[entry]["info"]["name"]
            game_store_table[-1]["backend"] = self.core.game_table[entry]["info"]["backend"]

        game_store_table = helpers.sort_dict_table(game_store_table, "name")
        game_store_list = helpers.dict_to_list(game_store_table, self.game_view_format)

        for entry in game_store_list:
            game_id = entry[self.game_view_format.index("game_id")]
            icon = game_id + '.png'
            icon_missing = "image-missing.png"

            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon), 24, 24))
            except GLib.Error:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon_missing), 24, 24))

            treeiter = self.game_store.append(entry)

    def fill_server_view(self, server_table):
        """Fill the server view"""
        self.server_view_format = ("host",
                                   "password",
                                   "player_count",
                                   "player_limit",
                                   "ping",
                                   "country",
                                   "name",
                                   "game_id",
                                   "game_type",
                                   "terrain")

        server_table = helpers.sort_dict_table(server_table, "ping")
        server_list = helpers.dict_to_list(server_table, self.server_view_format)

        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        a = self.serverhost_entry.get_text()
        self.serverlist_model.clear()
        self.serverhost_entry.set_text(a)

        # Goodies for GUI
        for i in range(len(server_list)):
            entry = server_list[i].copy()

            # Game icon
            try:
                if entry[self.server_view_format.index("game_id")] is not None:
                    entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, entry[self.server_view_format.index("game_id")] + '.png'), 24, 24))
                else:
                    entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, game + '.png'), 24, 24))
            except GLib.Error:
                icon_missing = "image-missing.png"
                print(GTK_MSG, N_("Error appending icon for game id:"), self.server_view_format.index("game_id"), "; host:", entry[self.server_view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon_missing), 24, 24))

            # Lock icon
            if entry[self.server_view_format.index("password")] == True:
                entry.append("network-wireless-encrypted-symbolic")
            else:
                entry.append(None)

            # Country flags
            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_FLAGS_DIR, entry[self.server_view_format.index("country")].lower() + '.svg'), 24, 18))
            except GLib.Error:
                print(GTK_MSG, N_("Error appending flag icon of ") + entry[self.server_view_format.index("country")] + " for host: " + entry[self.server_view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_FLAGS_DIR, 'unknown' + '.svg'), 24, 18))

            treeiter = self.serverlist_model.append(entry)

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_servers_page)

        self.serverlist_update_button.set_sensitive(True)
        self.game_combobox.set_sensitive(True)
        self.game_treeview.set_sensitive(True)

    def cb_server_list_selection_changed(self, widget, *data):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.serverhost_entry

        text = gtk_helpers.get_widget_value(widget, treeview_colnum=self.server_view_format.index("host"))

        gtk_helpers.set_widget_value(entry_field, text)

    def cb_server_list_view_row_activated(self, widget, path, column, *data):
        """Launches the game"""
        self.cb_server_list_selection_changed(widget)
        self.cb_info_button_clicked(widget)

    def cb_server_host_entry_changed(self, widget, *data):
        """Resets button sensitivity on Gtk.Entry change"""
        if widget.get_text() == '':
            self.serverlist_info_button.set_sensitive(False)
            self.serverlist_connect_button.set_sensitive(False)
        else:
            self.serverlist_info_button.set_sensitive(True)
            self.serverlist_connect_button.set_sensitive(True)

    def cb_listed_widget_changed(self, *data):
        self.update_settings_table()

    def apply_settings_to_preferences_dialog(self, game, widget_option_mapping, dynamic_settings_table):
        for option in widget_option_mapping:
            value = self.core.game_table[game]["settings"][option]
            if dynamic_settings_table[option]["gtk_type"] == "Multiline Entry with Label":
                value.join("\n")
            gtk_helpers.set_widget_value(widget_option_mapping[option], value)

    def update_settings_table(self, *data):
        for group in self.widget_table:
            for option in self.widget_table[group]:
                # Define variables
                widget_name = self.widget_table[group][option]["gtk_widget_name"]
                widget = self.builder.get_object(widget_name)

                self.app.settings.settings_table[group][option] = str(gtk_helpers.get_widget_value(widget))

    def update_game_settings_table(self, game, widget_option_mapping, dynamic_settings_table, *args):
        for option in widget_option_mapping:
            value = gtk_helpers.get_widget_value(widget_option_mapping[option])
            if dynamic_settings_table[option]["gtk_type"] == "Multiline Entry with Label":
                value.split("\n")
            self.core.game_table[game]["settings"][option] = value

    def cb_post_settings_genload(self, widget_table, group, option, value):
        self.widget_table = widget_table
        widget_name = widget_table[group][option]["gtk_widget_name"]
        widget = self.builder.get_object(widget_name)

        gtk_helpers.set_widget_value(widget, value)
        gtk_helpers.bind_widget_to_callback(widget, self.update_settings_table)


class App(Gtk.Application):

    """App class."""

    def __init__(self, Core):
        Gtk.Application.__init__(self,
                                 application_id=APPLICATION_ID,
                                 flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)
        self.connect("shutdown", self.on_shutdown)

        # Create builder
        self.builder = Gtk.Builder()
        self.builder.add_from_file(GTK_UI_FILE)
        self.builder.add_from_file(GTK_APPMENU_FILE)

        self.core = Core()
        self.settings = Settings(self.core, os.path.expanduser(PROFILE_PATH))

        self.guiactions = GUIActions(self, self.builder, self.core)

    def on_startup(self, app):
        """
        Startup function.
        Loads the GtkBuilder resources, settings and start the main loop.
        """

        # Load settings
        print(SEPARATOR_MSG + GTK_MSG, N_("Obozrenie is starting"))
        self.guiactions.get_games_list_store()
        self.settings.load(callback_postgenload=self.guiactions.cb_post_settings_genload)
        gtk_helpers.set_widget_value(self.guiactions.game_combobox, gtk_helpers.get_widget_value(self.guiactions.game_treeview))

        # Connect signals
        self.builder.connect_signals(self.guiactions)

        # Add main window
        main_window = self.builder.get_object("Main_Window")
        main_window.set_title("Obozrenie")
        self.add_window(main_window)

        # Create menu actions
        about_dialog = self.builder.get_object("About_Dialog")
        about_action = Gio.SimpleAction.new("about", None)
        quit_action = Gio.SimpleAction.new("quit", None)

        about_action.connect("activate", self.guiactions.cb_about, main_window)
        quit_action.connect("activate", self.guiactions.cb_quit, self)

        self.add_action(about_action)
        self.add_action(quit_action)

        self.set_app_menu(self.builder.get_object("app-menu"))

    def on_activate(self, app):
        window = self.builder.get_object("Main_Window")
        window.show_all()

    def on_shutdown(self, app):
        self.settings.save()
        print(GTK_MSG, N_("Shutting down"), "\n" + SEPARATOR_MSG)

if __name__ == "__main__":
    app = App(Core)
    app.run(None)
