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
import copy
import os
import shutil
import signal
import threading

from gi.repository import GLib, Gio, Gtk

from obozrenie.global_settings import *
from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers
import obozrenie.gtk_helpers as gtk_helpers
import obozrenie.core as core
import obozrenie.gtk_templates as templates


class GUIActions:

    """Responses to events from GUI"""

    def __init__(self, app, builder, core_library):
        self.app = app
        self.builder = builder
        self.core = core_library

        self.gtk_widgets = {}

        self.gtk_widgets = gtk_helpers.get_object_dict(self.builder, {"Game_Store":                             "game-model",
                                                                      "ServerList_Store":                       "serverlist-model",
                                                                      "playerlist-store":                       "playerlist-model",
                                                                      "Main_Window":                            "main-window",
                                                                      "Game_ComboBox":                          "game-combobox",
                                                                      "Game_TreeView":                          "game-treeview",
                                                                      "Game_TreeView_Column":                   "game-treeview-column",
                                                                      "Game_Store":                             "game-model",
                                                                      "Game_ComboBox_Revealer":                 "game-combobox-revealer",
                                                                      "Game_View_Revealer":                     "game-view-revealer",
                                                                      "Game_View_ToggleButton":                 "game-view-togglebutton",
                                                                      "Game_Preferences_Button":                "game-preferences-button",
                                                                      "Update_Button":                          "action-update-button",
                                                                      "Info_Button":                            "action-info-button",
                                                                      "Connect_Button":                         "action-connect-button",
                                                                      "Filters_Button":                         "filters-button",
                                                                      "ServerList_Store":                       "serverlist-model",
                                                                      "ServerList_View":                        "serverlist-view",
                                                                      "Name_ServerList_TreeViewColumn":         "serverlist-view-name-column",
                                                                      "Host_ServerList_TreeViewColumn":         "serverlist-view-host-column",
                                                                      "Ping_ServerList_TreeViewColumn":         "serverlist-view-ping-column",
                                                                      "Players_ServerList_TreeViewColumn":      "serverlist-view-players-column",
                                                                      "GameMod_ServerList_TreeViewColumn":      "serverlist-view-game_mod-column",
                                                                      "GameType_ServerList_TreeViewColumn":     "serverlist-view-game_type-column",
                                                                      "Terrain_ServerList_TreeViewColumn":      "serverlist-view-terrain-column",
                                                                      "ServerList_Notebook":                    "serverlist-notebook",
                                                                      "ServerList_ScrolledWindow":              "serverlist-scrolledwindow",
                                                                      "ServerList_Welcome_Label":               "serverlist-welcome-label",
                                                                      "ServerList_Refresh_Spinner":             "serverlist-refresh-spinner",
                                                                      "Error_Grid":                             "error-grid",
                                                                      "Error_Message_Label":                    "error-message-label",
                                                                      "server-connect-game":                    "server-connect-game",
                                                                      "server-connect-host":                    "server-connect-host",
                                                                      "server-connect-pass":                    "server-connect-pass",
                                                                      "serverinfo-dialog":                      "serverinfo-dialog",
                                                                      "serverinfo-name-label":                  "serverinfo-name-label",
                                                                      "serverinfo-name-data":                   "serverinfo-name",
                                                                      "serverinfo-host-label":                  "serverinfo-host-label",
                                                                      "serverinfo-host-data":                   "serverinfo-host",
                                                                      "serverinfo-game-label":                  "serverinfo-game-label",
                                                                      "serverinfo-game-data":                   "serverinfo-game",
                                                                      "serverinfo-terrain-label":               "serverinfo-terrain-label",
                                                                      "serverinfo-terrain-data":                "serverinfo-terrain",
                                                                      "serverinfo-players-label":               "serverinfo-players-label",
                                                                      "serverinfo-players-data":                "serverinfo-players",
                                                                      "serverinfo-ping-label":                  "serverinfo-ping-label",
                                                                      "serverinfo-ping-data":                   "serverinfo-ping",
                                                                      "serverinfo-connect-button":              "serverinfo-connect-button",
                                                                      "serverinfo-close-button":                "serverinfo-close-button",
                                                                      "serverinfo-players-scrolledview":        "serverinfo-players-scrolledview",
                                                                      "serverinfo-players-treeview":            "serverinfo-players-view",
                                                                      "serverinfo-players-name-treeviewcolumn": "serverinfo-players-name-column",
                                                                      "serverinfo-players-score-treeviewcolumn":"serverinfo-players-score-column",
                                                                      "serverinfo-players-ping-treeviewcolumn": "serverinfo-players-ping-column"
                                                                      })
        self.game_view_format = ("game_id",
                                 "name",
                                 "backend",
                                 "game_icon",
                                 "status_icon")

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

        self.player_list_model_format = ("name",
                                         "score",
                                         "ping")

        self.serverlist_notebook_pages = gtk_helpers.get_notebook_page_dict(self.gtk_widgets["serverlist-notebook"], {"servers": self.gtk_widgets["serverlist-scrolledwindow"],
                                                                                                                      "welcome": self.gtk_widgets["serverlist-welcome-label"],
                                                                                                                      "loading": self.gtk_widgets["serverlist-refresh-spinner"],
                                                                                                                      "error":   self.gtk_widgets["error-grid"]
                                                                                                                      })

        self.gtk_widgets["serverlist-notebook"].set_property("page", self.serverlist_notebook_pages["welcome"])


        # Load flags
        try:
            country_db = self.core.geolocation.const.COUNTRY_CODES
            self.flag_icons = gtk_helpers.get_icon_dict(country_db, 'flag', 'svg', ICON_FLAGS_DIR, 24, 18)
        except TypeError and AttributeError:
            self.flag_icons = {}
        game_list = self.core.get_game_set()
        self.game_icons = gtk_helpers.get_icon_dict(game_list, 'game', 'png', ICON_GAMES_DIR, 24, 24)

    def cb_game_preferences_button_clicked(self, *args):
        game = self.app.settings.settings_table["common"]["selected-game-browser"]
        prefs_dialog = templates.PreferencesDialog(self.gtk_widgets["main-window"],
                                                   game,
                                                   self.core.get_game_info(game),
                                                   self.core.get_game_settings(game),
                                                   self.app.settings.dynamic_widget_table,
                                                   callback_start=self.apply_settings_to_preferences_dialog,
                                                   callback_close=self.update_game_settings_table)
        prefs_dialog.run()
        prefs_dialog.destroy()

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        dialog = self.gtk_widgets["serverinfo-dialog"]

        game_table = self.core.get_game_table_copy()
        game = self.app.settings.settings_table["common"]["selected-game-connect"]
        server_list_table = game_table[game]["servers"]
        host = self.app.settings.settings_table["common"]["server-host"]
        server_entry_index = helpers.search_dict_table(server_list_table, "host", host)
        if server_entry_index is not None:
            server_entry = server_list_table[server_entry_index]
            player_model = self.gtk_widgets["playerlist-model"]
            player_scrolledview = self.gtk_widgets["serverinfo-players-scrolledview"]

            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-name"], server_entry["name"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-host"], server_entry["host"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-game"], game_table[server_entry["game_id"]]["info"]["name"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-terrain"], server_entry["terrain"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-players"], str(server_entry["player_count"]) + " / " + str(server_entry["player_limit"]))
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-ping"], server_entry["ping"])

            player_model.clear()
            try:
                player_table = helpers.dict_to_list(server_entry["players"], self.player_list_model_format)
                for entry in player_table:
                    player_model.append(entry)
                player_scrolledview.set_property("visible", True)
            except:
                player_scrolledview.set_property("visible", False)

            dialog.run()
            dialog.hide()

    def cb_connect_button_clicked(self, *args):
        game = gtk_helpers.get_widget_value(self.gtk_widgets["serverlist-view"])[self.server_list_model_format.index("game_id")]
        server = self.app.settings.settings_table["common"]["server-host"]
        password = self.app.settings.settings_table["common"]["server-pass"]
        self.cb_server_connect(game, server, password)

    def cb_serverinfo_connect_button_clicked(self, *args):
        game = gtk_helpers.get_widget_value(self.gtk_widgets["serverinfo-game"])
        server = gtk_helpers.get_widget_value(self.gtk_widgets["serverinfo-host"])
        password = self.app.settings.settings_table["common"]["server-pass"]
        self.cb_server_connect(game, server, password)

    def cb_server_connect(self, game, server, password):
        """Starts the game."""
        self.core.start_game(game, server, password)

    @staticmethod
    def cb_about(action, dialog, parent, *args):
        """Opens the About dialog."""
        about_dialog = templates.AboutDialog(parent, PROJECT, DESCRIPTION, WEBSITE, VERSION, AUTHORS, ARTISTS, COPYRIGHT, Gtk.License.GPL_3_0, None)
        about_dialog.run()
        about_dialog.destroy()

    @staticmethod
    def cb_hide(widget, *args):
        widget.hide()

    def cb_quit(self, *args):
        """Exits the program."""
        self.app.quit()

    def cb_game_combobox_changed(self, *args):
        """Actions on game combobox selection change."""
        combobox = self.gtk_widgets["game-combobox"]
        treeview = self.gtk_widgets["game-treeview"]
        game_id = gtk_helpers.get_widget_value(combobox)
        game_id_colnum = self.game_view_format.index("game_id")

        gtk_helpers.set_widget_value(treeview, game_id, treeview_colnum=game_id_colnum)

    def cb_game_treeview_togglebutton_clicked(self, *args):
        """Switches between TreeView and ComboBox game selection."""
        if gtk_helpers.get_widget_value(self.gtk_widgets["game-view-togglebutton"]) is True:
            self.gtk_widgets["game-combobox-revealer"].set_reveal_child(False)
            self.gtk_widgets["game-view-revealer"].set_reveal_child(True)
        else:
            self.gtk_widgets["game-combobox-revealer"].set_reveal_child(True)
            self.gtk_widgets["game-view-revealer"].set_reveal_child(False)

    def cb_game_treeview_selection_changed(self, *args):
        game_id = self.app.settings.settings_table["common"]["selected-game-browser"]
        query_status = self.core.get_query_status(game_id)

        gtk_helpers.set_widget_value(self.gtk_widgets["game-combobox"], game_id)
        if query_status is None:  # Refresh server list on first access
            self.cb_update_button_clicked()
        else:
            if query_status == "working":
                self.set_loading_state("working")
            GLib.idle_add(self.show_game_page, game_id)

    def cb_update_button_clicked(self, *args):
        """Actions on server list update button click"""
        game = self.app.settings.settings_table["common"]["selected-game-browser"]

        self.set_loading_state("working")
        self.set_game_state(game, "working")

        self.core.update_server_list(game, self.show_game_page)

    def fill_game_store(self):
        """
        Loads game list into a list store
        """

        game_table = self.core.get_game_table_copy()
        game_icons = self.game_icons
        game_model = self.gtk_widgets["game-model"]

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
            game_model.append(list_entry)

    def show_game_page(self, game):
        """Set of actions to do after query is complete."""
        query_status = self.app.core.get_query_status(game)
        server_table = self.app.core.get_servers_data(game)
        selected_game = self.app.settings.settings_table["common"]["selected-game-browser"]

        model = self.gtk_widgets["serverlist-model"]
        view = self.gtk_widgets["serverlist-view"]

        self.set_game_state(game, query_status)  # Display game status in GUI
        if selected_game == game:  # Is callback for the game that is currently viewed?
            if query_status == "ready":
                self.set_loading_state("filling list")
                view.set_model(None)  # Speed hack
                self.fill_server_list_model(server_table)
                view.set_model(model)
                self.set_loading_state("ready")
            elif query_status == "error":
                self.set_loading_state("error")

        self.cb_server_connect_data_changed()  # In case selected server's existence is altered

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

        model = self.gtk_widgets["game-model"]
        column = self.game_view_format.index("game_id")
        game_index = gtk_helpers.search_model(model, column, game)

        model[game_index][self.game_view_format.index("status_icon")] = icon

    def set_loading_state(self, state):
        notebook = self.gtk_widgets["serverlist-notebook"]

        if state == "working":
            notebook.set_property("page", self.serverlist_notebook_pages["loading"])
        elif state == "filling list" or state == "ready":
            notebook.set_property("page", self.serverlist_notebook_pages["servers"])
        elif state == "error":
            notebook.set_property("page", self.serverlist_notebook_pages["error"])


    def fill_server_list_model(self, server_table):
        """Fill the server view"""

        view_table = copy.deepcopy(server_table)

        model = self.gtk_widgets["serverlist-model"]
        model_append = model.append
        model_format = self.server_list_model_format

        game_icons = self.game_icons
        flag_icons = self.flag_icons

        # Clears the model

        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        host_selection = gtk_helpers.get_widget_value(self.gtk_widgets["server-connect-host"])
        game_selection = gtk_helpers.get_widget_value(self.gtk_widgets["server-connect-game"])
        model.clear()
        gtk_helpers.set_widget_value(self.gtk_widgets["server-connect-host"], host_selection)
        gtk_helpers.set_widget_value(self.gtk_widgets["server-connect-game"], game_selection, treeview_colnum=self.game_view_format.index("game_id"))

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

        for entry in server_list:
            model_append(entry)

    def cb_server_list_selection_changed(self, *args):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.gtk_widgets["server-connect-host"]
        game_selection = self.gtk_widgets["server-connect-game"]
        treeview = self.gtk_widgets["serverlist-view"]

        try:
            text = gtk_helpers.get_widget_value(treeview)[self.server_list_model_format.index("host")]
            game = gtk_helpers.get_widget_value(treeview)[self.server_list_model_format.index("game_id")]

            gtk_helpers.set_widget_value(entry_field, text)
            gtk_helpers.set_widget_value(game_selection, game, treeview_colnum=self.game_view_format.index("game_id"))
        except:
            pass

    def cb_server_list_view_row_activated(self, widget, path, column, *data):
        """Launches the game"""
        self.cb_server_list_selection_changed()
        self.cb_connect_button_clicked()

    def cb_server_connect_data_changed(self, *args):
        """Resets button sensitivity on server connect data change"""
        game = self.app.settings.settings_table["common"]["selected-game-connect"]
        server_list_table = self.core.get_servers_data(game)
        host = self.app.settings.settings_table["common"]["server-host"]
        server_entry_index = helpers.search_dict_table(server_list_table, "host", host)

        entry_field = self.gtk_widgets["server-connect-host"]
        info_button = self.gtk_widgets["action-info-button"]
        connect_button = self.gtk_widgets["action-connect-button"]

        if server_entry_index is None:
            info_button.set_property("sensitive", False)
            if gtk_helpers.get_widget_value(entry_field) == '':
                connect_button.set_property("sensitive", False)
            else:
                connect_button.set_property("sensitive", True)
        else:
            info_button.set_property("sensitive", True)
            connect_button.set_property("sensitive", True)

    def cb_listed_widget_changed(self, *args):
        self.update_settings_table()

    def apply_settings_to_preferences_dialog(self, game, widget_option_mapping, dynamic_settings_table):
        for option in widget_option_mapping:
            value = self.core.get_game_settings(game)[option]
            if dynamic_settings_table[option]["gtk_type"] == "Multiline Entry with Label":
                value = "\n".join(value)
            gtk_helpers.set_widget_value(widget_option_mapping[option], value)

    def update_settings_table(self, *args):
        for group in self.widget_table:
            for option in self.widget_table[group]:
                # Define variables
                widget_name = self.widget_table[group][option]["gtk_widget_name"]
                widget = self.builder.get_object(widget_name)
                value = gtk_helpers.get_widget_value(widget)
                if isinstance(widget, Gtk.TreeView) and "gtk_widget_colnum" in self.widget_table[group][option].keys():
                    value = value[self.widget_table[group][option]["gtk_widget_colnum"]]
                self.app.settings.settings_table[group][option] = str(value)

    def update_game_settings_table(self, game, widget_option_mapping, dynamic_settings_table, *args):
        for option in widget_option_mapping:
            value = gtk_helpers.get_widget_value(widget_option_mapping[option])
            if dynamic_settings_table[option]["gtk_type"] == "Multiline Entry with Label":
                value = value.split("\n")
            self.core.set_game_setting(game, option, value)

    def cb_post_settings_genload(self, widget_table, group, option, value):
        self.widget_table = widget_table
        widget_name = widget_table[group][option]["gtk_widget_name"]
        widget = self.builder.get_object(widget_name)

        gtk_helpers.set_widget_value(widget, value)
        gtk_helpers.bind_widget_to_callback(widget, self.update_settings_table)


class App(Gtk.Application):

    """App class."""

    def __init__(self, core_instance, settings_instance):
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

        self.core = core_instance
        self.settings = settings_instance

        self.guiactions = GUIActions(self, self.builder, self.core)

    def on_startup(self, app):
        """
        Startup function.
        Loads the GtkBuilder resources, settings and start the main loop.
        """

        try:
            # Load settings
            print(SEPARATOR_MSG + "\n" + i18n._(GTK_MSG), i18n._("Obozrenie is starting"), "\n" + SEPARATOR_MSG)
            self.status = "starting"
            self.guiactions.fill_game_store()
            self.settings.load(callback_postgenload=self.guiactions.cb_post_settings_genload)

            # Connect signals
            self.builder.connect_signals(self.guiactions)
            gtk_helpers.set_widget_value(self.guiactions.gtk_widgets["game-combobox"], self.settings.settings_table["common"]["selected-game-browser"])
            self.guiactions.cb_game_treeview_togglebutton_clicked()
            self.guiactions.cb_server_connect_data_changed()

            # Add main window
            main_window = self.guiactions.gtk_widgets["main-window"]
            self.add_window(main_window)

            # Create menu actions
            about_action = Gio.SimpleAction.new("about", None)
            quit_action = Gio.SimpleAction.new("quit", None)

            about_action.connect("activate", self.guiactions.cb_about, main_window)
            quit_action.connect("activate", self.guiactions.cb_quit, self)

            self.add_action(about_action)
            self.add_action(quit_action)

            self.set_app_menu(self.builder.get_object("app-menu"))

            gtk_helpers.set_object_properties(self.guiactions.gtk_widgets, GTK_STRING_TABLE)

            self.status = "up"
        except Exception as e:
            print(e)
            app.quit()

    def on_activate(self, app):
        window = self.guiactions.gtk_widgets["main-window"]
        window.show_all()

    def on_shutdown(self, app):
        if self.status == "up":
            self.settings.save()
            self.status = "shutting down"
            print(SEPARATOR_MSG + "\n" + i18n._(GTK_MSG), i18n._("Shutting down"), "\n" + SEPARATOR_MSG)
        else:
            self.status = "start failed"
            print(SEPARATOR_MSG + "\n" + i18n._(GTK_MSG), i18n._("Initialization failed. Aborting."), "\n", SEPARATOR_MSG)

if __name__ == "__main__":
    os.setpgrp()  # create new process group, become its leader
    try:
        core_instance = core.Core()
        settings_instance = core.Settings(core_instance, os.path.expanduser(PROFILE_PATH))
        app_instance = App(core_instance, settings_instance)
        app_instance.run(None)
    finally:
        os.killpg(0, signal.SIGTERM)  # kill all processes in my group
