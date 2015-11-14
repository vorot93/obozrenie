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


import ast
import os
import signal

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

        self.gtk_widgets = gtk_helpers.get_object_dict(self.builder, {"game-list-store":                        "game-list-model",
                                                                      "server-list-filter":                     "server-list-filter",
                                                                      "server-list-sort":                       "server-list-sort",
                                                                      "server-list-store":                      "server-list-model",
                                                                      "player-list-store":                      "player-list-model",
                                                                      "Main_Window":                            "main-window",
                                                                      "Game_ComboBox":                          "game-combobox",
                                                                      "Game_TreeView":                          "game-treeview",
                                                                      "Game_TreeView_Column":                   "game-treeview-column",
                                                                      "Game_ComboBox_Revealer":                 "game-combobox-revealer",
                                                                      "Game_View_Revealer":                     "game-view-revealer",
                                                                      "Game_View_ToggleButton":                 "game-view-togglebutton",
                                                                      "Game_Preferences_Button":                "game-preferences-button",
                                                                      "Update_Button":                          "action-update-button",
                                                                      "Info_Button":                            "action-info-button",
                                                                      "Connect_Button":                         "action-connect-button",
                                                                      "filters-revealer":                       "filters-revealer",
                                                                      "filters-button":                         "filters-button",
                                                                      "filter-mod-label":                       "filter-mod-label",
                                                                      "filter-type-label":                      "filter-type-label",
                                                                      "filter-terrain-label":                   "filter-terrain-label",
                                                                      "filter-ping-label":                      "filter-ping-label",
                                                                      "filter-secure-label":                    "filter-secure-label",
                                                                      "filter-mod-entry":                       "filter-mod",
                                                                      "filter-type-entry":                      "filter-type",
                                                                      "filter-terrain-entry":                   "filter-terrain",
                                                                      "filter-ping-adjustment":                 "filter-ping",
                                                                      "filter-secure-comboboxtext":             "filter-secure",
                                                                      "filter-notfull-checkbutton":             "filter-notfull",
                                                                      "filter-notempty-checkbutton":            "filter-notempty",
                                                                      "filter-nopassword-checkbutton":          "filter-nopassword",
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
        self.game_list_model_format = ("game_id",
                                       "name",
                                       "backend",
                                       "game_icon",
                                       "status_icon")

        self.server_list_model_format = ("host",
                                         "password",
                                         "player_count",
                                         "player_limit",
                                         "ping",
                                         "secure",
                                         "country",
                                         "name",
                                         "game_id",
                                         "game_mod",
                                         "game_type",
                                         "terrain",
                                         "game_icon",
                                         "password_icon",
                                         "secure_icon",
                                         "country_icon",
                                         "full",
                                         "empty")

        self.player_list_model_format = ("name",
                                         "score",
                                         "ping")

        self.filter_secure_list = ({"id": "None",  "text": i18n._("(all)")},
                                   {"id": "True",  "text": i18n._("True")},
                                   {"id": "False", "text": i18n._("False")})

        self.filter_criteria = [{"column": "game_mod",  "type": "in",               "widget": "filter-mod"},
                                {"column": "game_type", "type": "in",               "widget": "filter-type"},
                                {"column": "terrain",   "type": "in",               "widget": "filter-terrain"},
                                {"column": "ping",      "type": "<=",               "widget": "filter-ping"},
                                {"column": "secure",    "type": "bool is ast bool", "widget": "filter-secure"},
                                {"column": "full",      "type": "not true if true", "widget": "filter-notfull"},
                                {"column": "empty",     "type": "not true if true", "widget": "filter-notempty"},
                                {"column": "password",  "type": "not true if true", "widget": "filter-nopassword"}]

        self.serverlist_notebook_pages = gtk_helpers.get_notebook_page_dict(self.gtk_widgets["serverlist-notebook"], {"servers": self.gtk_widgets["serverlist-scrolledwindow"],
                                                                                                                      "welcome": self.gtk_widgets["serverlist-welcome-label"],
                                                                                                                      "loading": self.gtk_widgets["serverlist-refresh-spinner"],
                                                                                                                      "error":   self.gtk_widgets["error-grid"]
                                                                                                                      })

        self.gtk_widgets["serverlist-notebook"].set_property("page", self.serverlist_notebook_pages["welcome"])


        # Load flags
        try:
            country_db = self.core.geolocation.const.COUNTRY_CODES
            self.flag_icons = gtk_helpers.get_icon_dict(country_db, 'flag', ['svg'], ICON_FLAGS_DIR, 24, 18)
        except TypeError and AttributeError:
            self.flag_icons = {}
        game_list = self.core.get_game_set()
        self.game_icons = gtk_helpers.get_icon_dict(game_list, 'game', ['png', 'svg'], ICON_GAMES_DIR, 24, 24)

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
            player_model = self.gtk_widgets["player-list-model"]
            player_scrolledview = self.gtk_widgets["serverinfo-players-scrolledview"]

            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-name"], server_entry["name"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-host"], server_entry["host"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-game"], game_table[server_entry["game_id"]]["info"]["name"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-terrain"], server_entry["terrain"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-players"], i18n._("%(player_count)s / %(player_limit)s") % {'player_count': str(server_entry["player_count"]), 'player_limit': str(server_entry["player_limit"])})
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
        game = self.app.settings.settings_table["common"]["selected-game-connect"]
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
        about_dialog = templates.AboutDialog(parent, PROJECT, DESCRIPTION, WEBSITE, VERSION, AUTHORS, ARTISTS, COPYRIGHT, Gtk.License.GPL_3_0, ICON_NAME)
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
        game_id_colnum = self.game_list_model_format.index("game_id")

        gtk_helpers.set_widget_value(treeview, game_id, treeview_colnum=game_id_colnum)

    def cb_game_treeview_togglebutton_clicked(self, *args):
        """Switches between TreeView and ComboBox game selection."""
        button = self.gtk_widgets["game-view-togglebutton"]
        combobox_revealer = self.gtk_widgets["game-combobox-revealer"]
        treeview_revealer = self.gtk_widgets["game-view-revealer"]

        if gtk_helpers.get_widget_value(button) is True:
            combobox_revealer.set_reveal_child(False)
            treeview_revealer.set_reveal_child(True)
        else:
            combobox_revealer.set_reveal_child(True)
            treeview_revealer.set_reveal_child(False)

    def cb_game_treeview_selection_changed(self, *args):
        game_id = self.app.settings.settings_table["common"]["selected-game-browser"]
        query_status = self.core.get_query_status(game_id)

        gtk_helpers.set_widget_value(self.gtk_widgets["game-combobox"], game_id)
        if query_status == self.core.QUERY_STATUS.EMPTY:  # Refresh server list on first access
            self.cb_update_button_clicked()
        else:
            if query_status == self.core.QUERY_STATUS.WORKING:
                self.set_loading_state("working")
            GLib.idle_add(self.show_game_page, game_id)

    def cb_filters_button_clicked(self, *args):
        button = self.gtk_widgets["filters-button"]
        revealer = self.gtk_widgets["filters-revealer"]
        show_filters = gtk_helpers.get_widget_value(button)

        revealer.set_reveal_child(show_filters)

    def cb_update_button_clicked(self, *args):
        """Actions on server list update button click"""
        game = self.app.settings.settings_table["common"]["selected-game-browser"]

        self.set_loading_state("working")
        self.set_game_state(game, self.core.QUERY_STATUS.WORKING)

        self.core.update_server_list(game, self.cb_update_server_list)

    def cb_update_server_list(self, game):
        GLib.idle_add(self.show_game_page, game)

    def fill_game_store(self):
        """
        Loads game list into a list store
        """

        game_table = self.core.get_game_table_copy()
        game_icons = self.game_icons
        game_model = self.gtk_widgets["game-list-model"]

        game_store_table = []
        for entry in game_table:
            game_store_table.append({})
            game_store_table[-1]["game_id"] = entry
            game_store_table[-1]["name"] = game_table[entry]["info"]["name"]
            game_store_table[-1]["backend"] = game_table[entry]["info"]["backend"]
            game_store_table[-1]["status_icon"] = None
            game_store_table[-1]["game_icon"] = game_icons[entry]

        game_store_table = helpers.sort_dict_table(game_store_table, "name")
        game_store_list = helpers.dict_to_list(game_store_table, self.game_list_model_format)

        for list_entry in game_store_list:
            game_model.append(list_entry)

    def show_game_page(self, game):
        """Set of actions to do after query is complete."""
        query_status = self.app.core.get_query_status(str(game))
        query_status_enum = self.core.QUERY_STATUS
        server_table = self.app.core.get_servers_data(str(game))
        selected_game = self.app.settings.settings_table["common"]["selected-game-browser"]

        model = self.gtk_widgets["server-list-sort"]
        view = self.gtk_widgets["serverlist-view"]

        self.set_game_state(game, query_status)  # Display game status in GUI
        if selected_game == game:  # Is callback for the game that is currently viewed?
            if query_status == query_status_enum.READY:
                self.set_loading_state("filling list")
                view.set_model(None)  # Speed hack
                self.fill_server_list_model(server_table)
                view.set_model(model)
                self.set_loading_state("ready")
            elif query_status == query_status_enum.WORKING:
                self.set_loading_state("working")
            elif query_status == query_status_enum.ERROR:
                self.set_loading_state("error")

        self.cb_server_connect_data_changed()  # In case selected server's existence is altered

    def set_game_state(self, game, state):
        icon = ""
        query_status_enum = self.core.QUERY_STATUS

        if state == query_status_enum.WORKING:
            icon = "emblem-synchronizing-symbolic"
        elif state == query_status_enum.READY:
            icon = "emblem-ok-symbolic"
        elif state == query_status_enum.ERROR:
            icon = "error"
        else:
            return

        model = self.gtk_widgets["game-list-model"]
        column = self.game_list_model_format.index("game_id")
        game_index = gtk_helpers.search_model(model, column, game)

        model[game_index][self.game_list_model_format.index("status_icon")] = icon

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

        view_table = helpers.deepcopy(server_table)

        model = self.gtk_widgets["server-list-model"]
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
        gtk_helpers.set_widget_value(self.gtk_widgets["server-connect-game"], game_selection, treeview_colnum=self.game_list_model_format.index("game_id"))

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

            if entry["secure"] is True:
                entry["secure_icon"] = "security-high-symbolic"
            else:
                entry["secure_icon"] = None

            # Country flags
            entry["country_icon"] = flag_icons.get(country)

            # Filtering stuff
            entry["full"] = entry["player_count"] >= entry["player_limit"]
            entry["empty"] = entry["player_count"] == 0

        view_table = helpers.sort_dict_table(view_table, "ping")
        server_list = helpers.dict_to_list(view_table, model_format)

        for entry in server_list:
            model_append(entry)


    # Server list filtering

    def server_filter_func(self, model, treeiter, *args):
        """Tests if row matches filter settings"""
        filter_criteria = self.filter_criteria
        server_list_model_format_index = self.server_list_model_format.index
        result = None

        # Cycle through all criteria and break if at least one isn't satisfied
        for criterium in filter_criteria:
            if result is not False:
                column = criterium["column"]
                comparison = criterium["type"]
                column_index = server_list_model_format_index(column)
                entry_value = model[treeiter][column_index]
                comparison_value = criterium["value"]
                if comparison_value is None or comparison_value == "" or comparison_value == "None":
                    result = True
                else:
                    if comparison == "==":
                        result = entry_value == comparison_value
                    elif comparison == "!=":
                        result = entry_value != comparison_value
                    if comparison == "bool is ast bool":
                        result = entry_value is ast.literal_eval(comparison_value)
                    elif comparison == "not true if true":
                        if comparison_value is True:
                            result = entry_value != comparison_value
                        else:
                            result = True
                    elif comparison == "<":
                        if comparison_value == 0:
                            result = True
                        else:
                            result = entry_value < comparison_value
                    elif comparison == "<=":
                        if comparison_value == 0:
                            result = True
                        else:
                            result = entry_value <= comparison_value
                    elif comparison == ">":
                        result = entry_value > comparison_value
                    elif comparison == "in":
                        if entry_value is None:
                            result = False
                        else:
                            result = comparison_value in entry_value
        return result

    def cb_server_filters_changed(self, *args):
        filter_criteria = self.filter_criteria
        for criterium in filter_criteria:
            criterium["value"] = gtk_helpers.get_widget_value(self.gtk_widgets[criterium["widget"]])

        self.gtk_widgets["server-list-filter"].refilter()

    # Server selection

    def cb_server_list_selection_changed(self, *args):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.gtk_widgets["server-connect-host"]
        game_selection = self.gtk_widgets["server-connect-game"]
        treeview = self.gtk_widgets["serverlist-view"]

        try:
            text = gtk_helpers.get_widget_value(treeview)[self.server_list_model_format.index("host")]
            game = gtk_helpers.get_widget_value(treeview)[self.server_list_model_format.index("game_id")]

            gtk_helpers.set_widget_value(entry_field, text)
            gtk_helpers.set_widget_value(game_selection, game, treeview_colnum=self.game_list_model_format.index("game_id"))
        except:
            pass

    def cb_server_list_view_row_activated(self, widget, path, column, *data):
        """Launches the game"""
        self.cb_server_list_selection_changed()
        self.cb_connect_button_clicked()

    def cb_server_connect_data_changed(self, *args):
        """Resets button sensitivity on server connect data change"""
        game = self.app.settings.settings_table["common"]["selected-game-connect"]
        try:
            server_list_table = self.core.get_servers_data(game)
        except (ValueError, KeyError):
            server_list_table = []
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
                    colnum = self.widget_table[group][option]["gtk_widget_colnum"]
                    try:
                        value = value[colnum]
                    except TypeError:
                        pass
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
            helpers.debug_msg([GTK_MSG, i18n._("Obozrenie is starting")])
            guiactions = self.guiactions

            self.status = "starting"
            guiactions.fill_game_store()

            self.settings.load(callback_postgenload=self.guiactions.cb_post_settings_genload)

            # Connect signals
            self.builder.connect_signals(self.guiactions)
            guiactions.gtk_widgets["server-list-filter"].set_visible_func(guiactions.server_filter_func)

            gtk_helpers.set_widget_value(self.guiactions.gtk_widgets["game-combobox"], self.settings.settings_table["common"]["selected-game-browser"])
            for entry in self.guiactions.filter_secure_list:
                guiactions.gtk_widgets["filter-secure"].append(entry["id"], entry["text"])
            guiactions.cb_game_treeview_togglebutton_clicked()
            guiactions.cb_server_filters_changed()
            try:
                guiactions.cb_server_connect_data_changed()
            except ValueError:
                pass

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
            app.quit()
            raise e

    def on_activate(self, app):
        window = self.guiactions.gtk_widgets["main-window"]
        window.show_all()

    def on_shutdown(self, app):
        if self.status == "up":
            self.settings.save()
            self.status = "shutting down"
            helpers.debug_msg([GTK_MSG, i18n._("Shutting down")])
        else:
            self.status = "start failed"
            helpers.debug_msg([GTK_MSG, i18n._("Initialization failed. Aborting.")])
