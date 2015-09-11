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

from gi.repository import GLib, Gio, Gtk

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

        builder_object = self.builder.get_object

        self.main_window = builder_object("Main_Window")

        self.game_combobox = builder_object("Game_ComboBox")
        self.game_treeview = builder_object("Game_TreeView")
        self.game_model = builder_object("Game_Store")
        self.game_combobox_revealer = builder_object("Game_ComboBox_Revealer")
        self.game_view_revealer = builder_object("Game_View_Revealer")

        self.game_view_togglebutton = builder_object("Game_View_ToggleButton")
        self.serverlist_update_button = builder_object("Update_Button")
        self.serverlist_info_button = builder_object("Info_Button")
        self.serverlist_connect_button = builder_object("Connect_Button")

        self.serverlist_model = builder_object("ServerList_Store")
        self.serverlist_view = builder_object("ServerList_View")
        self.serverlist_view_selection = builder_object("ServerList_View").get_selection()

        self.serverlist_notebook = builder_object("ServerList_Notebook")

        self.serverlist_scrolledwindow = builder_object("ServerList_ScrolledWindow")
        self.welcome_label = builder_object("ServerList_Welcome_Label")
        self.refresh_spinner = builder_object("ServerList_Refresh_Spinner")
        self.error_grid = builder_object("Error_Grid")

        self.serverlist_notebook_pages = self.get_notebook_page_dict(self.serverlist_notebook, {"servers": self.serverlist_scrolledwindow,
                                                                                                "welcome": self.welcome_label,
                                                                                                "loading": self.refresh_spinner,
                                                                                                "error":   self.error_grid})

        self.serverhost_entry = self.builder.get_object("ServerHost_Entry")

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_pages["welcome"])


        self.server_list_model_format = ("host",
                                         "password",
                                         "player_count",
                                         "player_limit",
                                         "ping",
                                         "country",
                                         "name",
                                         "game_id",
                                         "game_mod",
                                         "game_type",
                                         "terrain",
                                         "game_icon",
                                         "password_icon",
                                         "country_icon")

        # Load flags
        try:
            country_db = self.core.geolocation.const.COUNTRY_CODES
            self.flag_icons = gtk_helpers.get_icon_dict(country_db, 'flag', 'svg', ICON_FLAGS_DIR, 24, 18)
        except TypeError:
            self.flag_icons = {}
        game_list = self.core.game_table.keys()
        self.game_icons = gtk_helpers.get_icon_dict(game_list, 'game', 'png', ICON_GAMES_DIR, 24, 24)

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
        game = gtk_helpers.get_widget_value(self.serverlist_view, treeview_colnum=self.server_list_model_format.index("game_id"))
        if game is None:
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
        if self.core.game_table[game_id]["query-status"] is None:  # Refresh server list on first access
            self.cb_update_button_clicked(widget, *data)
        else:
            if self.core.game_table[game_id]["query-status"] == "working":
                self.set_loading_state("working")
            GLib.idle_add(self.show_game_page, game_id, self.core.game_table.copy())

    def cb_update_button_clicked(self, widget, *data):
        """Actions on server list update button click"""
        ping_button = self.builder.get_object("PingingEnable_CheckButton")
        ping_column = self.builder.get_object("Ping_ServerList_TreeViewColumn")

        game = self.app.settings.settings_table["common"]["selected-game"]

        self.set_loading_state("working")
        self.set_game_state(game, "working")

        self.core.update_server_list(game, self.show_game_page)

    @staticmethod
    def get_notebook_page_dict(notebook, widget_mapping):
        """Get mapping for notebook pages."""
        notebook_pages = {}
        for entry in widget_mapping:
            notebook_pages[entry] = notebook.page_num(widget_mapping[entry])

        return notebook_pages

    def fill_game_store(self):
        """
        Loads game list into a list store
        """
        self.game_view_format = ("game_id",
                                 "name",
                                 "backend",
                                 "game_icon",
                                 "status_icon")

        game_table = self.core.game_table.copy()
        game_icons = self.game_icons

        self.game_store = self.builder.get_object("Game_Store")

        game_store_table = []
        for entry in game_table:
            icon = entry + '.png'

            game_store_table.append({})
            game_store_table[-1]["game_id"] = entry
            game_store_table[-1]["name"] = game_table[entry]["info"]["name"]
            game_store_table[-1]["backend"] = game_table[entry]["info"]["backend"]
            game_store_table[-1]["status_icon"] = None
            game_store_table[-1]["game_icon"] = game_icons[entry]

        game_store_table = helpers.sort_dict_table(game_store_table, "name")
        game_store_list = helpers.dict_to_list(game_store_table, self.game_view_format)

        for list_entry in game_store_list:
            self.game_store.append(list_entry)

    def show_game_page(self, game, game_table):
        """Set of actions to do after query is complete."""
        self.set_game_state(game, game_table[game]["query-status"])  # Display game status in GUI
        if self.app.settings.settings_table["common"]["selected-game"] == game:  # Is callback for the game that is currently viewed?
            if game_table[game]["query-status"] == "ready":
                self.fill_server_list_model(game_table[game]["servers"])
                self.set_loading_state("ready")
            elif game_table[game]["query-status"] == "error":
                self.set_loading_state("error")

    def set_game_state(self, game, state):
        icon = ""

        if state == "working":
            icon = "emblem-synchronizing-symbolic"
        elif state == "ready":
            icon = "emblem-ok-symbolic"
        elif state == "error":
            icon = "error"
        else:
            return

        model = self.game_model
        column = self.game_view_format.index("game_id")
        game_index = gtk_helpers.search_model(model, column, game)

        model[game_index][self.game_view_format.index("status_icon")] = icon

    def set_loading_state(self, state):
        if state == "working":
            self.serverlist_notebook.set_property("page", self.serverlist_notebook_pages["loading"])
        elif state == "ready":
            self.serverlist_notebook.set_property("page", self.serverlist_notebook_pages["servers"])
        elif state == "error":
            self.serverlist_notebook.set_property("page", self.serverlist_notebook_pages["error"])


    def fill_server_list_model(self, server_table):
        """Fill the server view"""

        view_table = server_table.copy()

        model = self.serverlist_model
        model_append = model.append
        model_format = self.server_list_model_format

        game_icons = self.game_icons
        flag_icons = self.flag_icons

        # Goodies for GUI
        for entry in view_table:
            game_id = entry.get("game_id")
            country = entry.get("country")
            # Game icon
            entry["game_icon"] = game_icons.get(game_id)

            # Lock icon
            if entry["password"] is True:
                entry["password_icon"] = "network-wireless-encrypted-symbolic"
            else:
                entry["password_icon"] = None

            # Country flags
            entry["country_icon"] = flag_icons.get(country)

        view_table = helpers.sort_dict_table(view_table, "ping")
        server_list = helpers.dict_to_list(view_table, self.server_list_model_format)
        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        a = self.serverhost_entry.get_text()
        model.clear()
        self.serverhost_entry.set_text(a)

        for entry in server_list:
            treeiter = model_append(entry)

    def cb_server_list_selection_changed(self, widget, *data):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.serverhost_entry

        text = gtk_helpers.get_widget_value(widget, treeview_colnum=self.server_list_model_format.index("host"))

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
        print(SEPARATOR_MSG + "\n" + GTK_MSG, N_("Obozrenie is starting"), "\n" + SEPARATOR_MSG)
        self.status = "starting"
        self.guiactions.fill_game_store()
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

        self.status = "up"

    def on_activate(self, app):
        window = self.builder.get_object("Main_Window")
        window.show_all()

    def on_shutdown(self, app):
        if self.status == "up":
            self.settings.save()
            self.status = "shutting down"
            print(SEPARATOR_MSG + "\n" + GTK_MSG, N_("Shutting down"), "\n" + SEPARATOR_MSG)
        else:
            self.status = "start failed"
            print(SEPARATOR_MSG + "\n" + GTK_MSG, N_("Initialization failed. Aborting."), "\n", SEPARATOR_MSG)

if __name__ == "__main__":
    app = App(Core)
    app.run(None)
