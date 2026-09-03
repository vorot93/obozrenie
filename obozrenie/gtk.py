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

"""Simple and easy to use game server browser."""


import ast
import threading
import time
from collections.abc import Callable

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gio, Gtk

import obozrenie.gtk_templates as templates
import obozrenie.core as core
from obozrenie.game_table import QueryStatus
from obozrenie import geoip, resources
from obozrenie.diagnostics import diagnostic_for_exception
import obozrenie.gtk_helpers as gtk_helpers
import obozrenie.helpers as helpers
import obozrenie.i18n as i18n
from obozrenie.global_strings import *
from obozrenie.global_settings import *
from obozrenie.models import Endpoint
from obozrenie.network_calls import validate_deadline


_BUILDER_RESOURCES = (
    "obozrenie_gtk.ui",
    "obozrenie_gtk_appmenu.ui",
)


def load_builder_resources(builder, translations=None):
    for filename in _BUILDER_RESOURCES:
        builder.add_from_string(i18n.translate_builder_xml(
            resources.read_text("ui", filename),
            translations,
        ))


def derive_action_sensitivity(
    browser_game_valid: bool,
    connect_game_valid: bool,
    endpoint_valid: bool,
    query_status: QueryStatus | None,
    selected_server_present: bool,
) -> dict[str, bool]:
    """Derive action availability without reading or mutating GTK state."""
    return {
        "preferences": browser_game_valid,
        "refresh": (
            browser_game_valid
            and query_status is not QueryStatus.WORKING
        ),
        "connect": connect_game_valid and endpoint_valid,
        "info": (
            connect_game_valid
            and endpoint_valid
            and selected_server_present
        ),
    }


def _parse_endpoint(value: object) -> Endpoint | None:
    if type(value) is not str:
        return None
    try:
        return Endpoint.parse(value)
    except ValueError:
        return None


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
                                                                       "Error_Message_Content_Label":            "error-message-content-label",
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
                                                                      "serverinfo-gameid-label":                "serverinfo-gameid-label",
                                                                      "serverinfo-gameid-data":                 "serverinfo-gameid",
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
                                                                      "serverinfo-players-score-treeviewcolumn": "serverinfo-players-score-column",
                                                                      "serverinfo-players-ping-treeviewcolumn": "serverinfo-players-ping-column"
                                                                      })
        self.game_list_model_format = ("game_id",
                                       "name",
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
                                {"column": "game_type", "type": "in",
                                    "widget": "filter-type"},
                                {"column": "terrain",   "type": "in",
                                    "widget": "filter-terrain"},
                                {"column": "ping",      "type": "<=",
                                    "widget": "filter-ping"},
                                {"column": "secure",    "type": "bool is ast bool",
                                    "widget": "filter-secure"},
                                {"column": "full",      "type": "not true if true",
                                    "widget": "filter-notfull"},
                                {"column": "empty",     "type": "not true if true",
                                    "widget": "filter-notempty"},
                                {"column": "password",  "type": "not true if true", "widget": "filter-nopassword"}]

        self.serverlist_notebook_pages = gtk_helpers.get_notebook_page_dict(self.gtk_widgets["serverlist-notebook"], {"servers": self.gtk_widgets["serverlist-scrolledwindow"],
                                                                                                                      "welcome": self.gtk_widgets["serverlist-welcome-label"],
                                                                                                                      "loading": self.gtk_widgets["serverlist-refresh-spinner"],
                                                                                                                      "error":   self.gtk_widgets["error-grid"]
                                                                                                                      })

        self.gtk_widgets["serverlist-notebook"].set_property(
            "page", self.serverlist_notebook_pages["welcome"])

        game_list = self.core.game_table.get_game_set()
        self.game_icons = gtk_helpers.get_icon_dict(
            game_list,
            ('png', 'svg'),
            resources.resource("icons", "games"),
            24,
            24,
        )
        self.logo = gtk_helpers.load_icon(
            resources.resource(
                "icons",
                "hicolor",
                "scalable",
                "apps",
                "obozrenie-short.svg",
            ),
            "svg",
        )

    def cb_game_preferences_button_clicked(self, *args):
        game = self.app.settings.settings_table["common"]["selected-game-browser"]
        if game not in self.core.game_table.get_game_set():
            return
        prefs_dialog = templates.PreferencesDialog(self.gtk_widgets["main-window"],
                                                   game,
                                                   self.core.game_table.get_game_info(
                                                       game),
                                                   self.core.game_table.get_game_settings(
                                                       game),
                                                   self.app.settings.dynamic_widget_table,
                                                   callback_start=self.apply_settings_to_preferences_dialog,
                                                   callback_close=self.update_game_settings_table)
        prefs_dialog.run()
        prefs_dialog.destroy()

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        dialog = self.gtk_widgets["serverinfo-dialog"]

        game = self.app.settings.settings_table["common"]["selected-game-connect"]
        host = self.app.settings.settings_table["common"]["server-host"]
        endpoint = _parse_endpoint(host)
        if game not in self.core.game_table.get_game_set() or endpoint is None:
            return
        server = self._find_selected_server(game, endpoint)
        if server is not None:
            server_entry = server.to_mapping()
            player_model = self.gtk_widgets["player-list-model"]

            gtk_helpers.set_widget_value(
                self.gtk_widgets["serverinfo-name"], server_entry["name"])
            gtk_helpers.set_widget_value(
                self.gtk_widgets["serverinfo-host"], server_entry["host"])
            gtk_helpers.set_widget_value(
                self.gtk_widgets["serverinfo-game"],
                self.core.game_table.get_game_info(
                    server_entry["game_id"])["name"],
            )
            gtk_helpers.set_widget_value(
                self.gtk_widgets['serverinfo-gameid'], server_entry['game_id'])
            gtk_helpers.set_widget_value(
                self.gtk_widgets["serverinfo-terrain"], server_entry["terrain"])
            gtk_helpers.set_widget_value(self.gtk_widgets["serverinfo-players"], i18n._("%(player_count)s / %(player_limit)s") % {
                                         'player_count': str(server_entry["player_count"]), 'player_limit': str(server_entry["player_limit"])})
            gtk_helpers.set_widget_value(
                self.gtk_widgets["serverinfo-ping"], server_entry["ping"])

            player_model.clear()
            # Nothing here can fail on a server's account: `players` is a
            # tuple of validated `Player` records, `dict_to_list` supplies
            # `None` for a key a record lacks, and the store accepts every
            # value those records can hold — a missing score included, which
            # it stores as zero. A failure would therefore be a programming
            # error, and hiding the whole player list is the last thing that
            # should happen to one. This view's visibility belongs to the
            # GtkBuilder document, exactly like every other widget's.
            for entry in helpers.dict_to_list(
                    server_entry["players"], self.player_list_model_format):
                player_model.append(entry)

            dialog.run()
            dialog.hide()

    def cb_connect_button_clicked(self, *args):
        game = self.app.settings.settings_table["common"]["selected-game-connect"]
        server = self.app.settings.settings_table["common"]["server-host"]
        endpoint = _parse_endpoint(server)
        if game not in self.core.game_table.get_game_set() or endpoint is None:
            return
        password = gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-pass"])
        self.cb_server_connect(game, str(endpoint), password)

    def cb_serverinfo_connect_button_clicked(self, *args):
        game = gtk_helpers.get_widget_value(
            self.gtk_widgets["serverinfo-gameid"])
        server = gtk_helpers.get_widget_value(
            self.gtk_widgets["serverinfo-host"])
        endpoint = _parse_endpoint(server)
        if game not in self.core.game_table.get_game_set() or endpoint is None:
            return
        if self._find_selected_server(game, endpoint) is None:
            return
        password = gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-pass"])
        self.cb_server_connect(game, str(endpoint), password)

    def show_error_dialog(self, message: str) -> bool:
        """Show a reusable modal error dialog on the GTK main thread."""
        dialog = Gtk.MessageDialog(
            transient_for=self.gtk_widgets["main-window"],
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message,
        )
        try:
            dialog.run()
        finally:
            dialog.destroy()
        return False

    def cb_server_connect(self, game, server, password):
        """Starts the game."""
        def launch_complete(error):
            if error is None:
                return
            details = str(error)
            if password:
                details = details.replace(password, "***")
            message = i18n._("Unable to launch the game: %(error)s") % {
                "error": details,
            }
            GLib.idle_add(self.show_error_dialog, message)

        try:
            self.core.start_game(game, server, password, launch_complete)
        except Exception as error:
            launch_complete(error)

    def cb_about(self, action, dialog, parent, *args):
        """Opens the About dialog."""
        kwargs = {
            'parent': parent,
            'program_name': PROJECT,
            'comments': DESCRIPTION,
            'version': VERSION,
            'authors': AUTHORS,
            'copyright': COPYRIGHT,
            'license_type': Gtk.License.GPL_3_0
        }
        kwargs.update({'logo': self.logo})

        about_dialog = Gtk.AboutDialog(**kwargs)
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

        gtk_helpers.set_widget_value(
            treeview, game_id, treeview_colnum=game_id_colnum)

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
        if game_id not in self.core.game_table.get_game_set():
            self.update_action_sensitivity()
            return
        query_status = self.core.game_table.get_query_status(game_id)

        self.update_action_sensitivity()
        gtk_helpers.set_widget_value(
            self.gtk_widgets["game-combobox"], game_id)
        if query_status == QueryStatus.EMPTY:  # Refresh server list on first access
            self.cb_update_button_clicked()
        else:
            if query_status == QueryStatus.WORKING:
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
        if game not in self.core.game_table.get_game_set():
            return
        if (
            self.core.game_table.get_query_status(game)
            is QueryStatus.WORKING
        ):
            self.update_action_sensitivity()
            return

        admitted = self.core.update_server_list(
            game, stat_callback=self.cb_update_server_list)
        if not admitted:
            self.update_action_sensitivity()
            return
        query_status = self.core.game_table.get_query_status(game)
        if query_status is QueryStatus.WORKING:
            self.set_loading_state("working")
            self.set_game_state(game, query_status)
        self.update_action_sensitivity()

    def cb_update_server_list(self, game: str) -> None:
        GLib.idle_add(self.show_game_page, game)

    def fill_game_store(self):
        """
        Loads game list into a list store
        """

        game_table = self.core.game_table.get_game_table_copy()
        game_icons = self.game_icons
        game_model = self.gtk_widgets["game-list-model"]

        game_store_table = []
        for entry in game_table:
            game_store_table.append({})
            game_store_table[-1]["game_id"] = entry
            game_store_table[-1]["name"] = game_table[entry]["info"]["name"]
            game_store_table[-1]["status_icon"] = None
            game_store_table[-1]["game_icon"] = game_icons[entry]

        game_store_table = helpers.sort_dict_table(game_store_table, "name")
        game_store_list = helpers.dict_to_list(
            game_store_table, self.game_list_model_format)

        for list_entry in game_store_list:
            game_model.append(list_entry)

    def show_game_page(self, game: str) -> None:
        """Set of actions to do after query is complete."""
        query_status = self.app.core.game_table.get_query_status(str(game))
        server_table = self.app.core.game_table.get_servers_data(str(game))
        selected_game = self.app.settings.settings_table["common"]["selected-game-browser"]

        model = self.gtk_widgets["server-list-sort"]
        view = self.gtk_widgets["serverlist-view"]

        self.set_game_state(game, query_status)  # Display game status in GUI
        if selected_game == game:  # Is callback for the game that is currently viewed?
            if query_status == QueryStatus.READY:
                self.set_loading_state("filling list")
                view.set_model(None)  # Speed hack
                self.fill_server_list_model(server_table)
                view.set_model(model)
                self.set_loading_state("ready")
            elif query_status == QueryStatus.WORKING:
                self.set_loading_state("working")
            elif query_status == QueryStatus.ERROR:
                self.gtk_widgets["error-message-content-label"].set_property(
                    "label",
                    self.app.core.game_table.get_query_error(str(game)),
                )
                self.set_loading_state("error")

        # In case selected server's existence is altered
        self.update_action_sensitivity()

    def set_game_state(self, game: str, state: str) -> None:
        icon = ""

        if state == QueryStatus.WORKING:
            icon = "emblem-synchronizing-symbolic"
        elif state == QueryStatus.READY:
            icon = "emblem-ok-symbolic"
        elif state == QueryStatus.ERROR:
            icon = "error"
        else:
            return

        model = self.gtk_widgets["game-list-model"]
        column = self.game_list_model_format.index("game_id")
        game_index = gtk_helpers.search_model(model, column, game)

        model[game_index][self.game_list_model_format.index(
            "status_icon")] = icon

    def set_loading_state(self, state: str) -> None:
        notebook = self.gtk_widgets["serverlist-notebook"]

        if state == "working":
            notebook.set_property(
                "page", self.serverlist_notebook_pages["loading"])
        elif state == "filling list" or state == "ready":
            notebook.set_property(
                "page", self.serverlist_notebook_pages["servers"])
        elif state == "error":
            notebook.set_property(
                "page", self.serverlist_notebook_pages["error"])

    def fill_server_list_model(self, server_table):
        """Fill the server view"""

        view_table = [server.to_mapping() for server in server_table]

        model = self.gtk_widgets["server-list-model"]
        model_append = model.append
        model_format = self.server_list_model_format

        game_icons = self.game_icons

        # Clears the model

        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        host_selection = gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-host"])
        game_selection = gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-game"])
        model.clear()
        gtk_helpers.set_widget_value(
            self.gtk_widgets["server-connect-host"], host_selection)
        gtk_helpers.set_widget_value(
            self.gtk_widgets["server-connect-game"], game_selection, treeview_colnum=self.game_list_model_format.index("game_id"))

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

            # Country flag (emoji derived from the ISO country code)
            entry["country_icon"] = gtk_helpers.country_code_to_flag_emoji(country)

            # Filtering stuff
            entry["full"] = entry["player_count"] >= entry["player_limit"]
            entry["empty"] = entry["player_count"] == 0

        view_table = helpers.sort_dict_table(view_table, "ping")
        server_list = helpers.dict_to_list(view_table, model_format)

        for entry in server_list:
            model_append(entry)

    # Server list filtering

    def server_filter_func(self, model: Gtk.TreeModel, treeiter: Gtk.TreeIter, *args) -> bool:
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
                if comparison_value in (None, "", "None"):
                    result = True
                else:
                    if comparison == "==":
                        result = entry_value == comparison_value
                    elif comparison == "!=":
                        result = entry_value != comparison_value
                    elif comparison == "bool is ast bool":
                        result = entry_value is ast.literal_eval(
                            comparison_value)
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
            criterium["value"] = gtk_helpers.get_widget_value(
                self.gtk_widgets[criterium["widget"]])

        self.gtk_widgets["server-list-filter"].refilter()

    # Server selection

    def cb_server_list_selection_changed(self, *args):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.gtk_widgets["server-connect-host"]
        game_selection = self.gtk_widgets["server-connect-game"]
        treeview = self.gtk_widgets["serverlist-view"]

        try:
            text = gtk_helpers.get_widget_value(
                treeview)[self.server_list_model_format.index("host")]
            game = gtk_helpers.get_widget_value(
                treeview)[self.server_list_model_format.index("game_id")]

            gtk_helpers.set_widget_value(entry_field, text)
            gtk_helpers.set_widget_value(
                game_selection, game, treeview_colnum=self.game_list_model_format.index("game_id"))
        except:
            pass

    def cb_server_list_view_row_activated(self, widget, path, column, *data):
        """Launches the game"""
        self.cb_server_list_selection_changed()
        self.cb_connect_button_clicked()

    def cb_server_connect_data_changed(self, *args):
        """Recompute action sensitivity after Connect data changes."""
        self.update_action_sensitivity()

    def _find_selected_server(self, game, endpoint):
        try:
            server_list_table = self.core.game_table.get_servers_data(game)
        except ValueError:
            return None
        return next(
            (
                server
                for server in server_list_table
                if server.game_id == game and server.endpoint == endpoint
            ),
            None,
        )

    def update_action_sensitivity(self) -> dict[str, bool]:
        common_settings = self.app.settings.settings_table["common"]
        games = self.core.game_table.get_game_set()
        browser_game = common_settings.get("selected-game-browser")
        connect_game = gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-game"])
        endpoint = _parse_endpoint(gtk_helpers.get_widget_value(
            self.gtk_widgets["server-connect-host"]))

        browser_game_valid = browser_game in games
        connect_game_valid = connect_game in games
        query_status = None
        if browser_game_valid:
            query_status = self.core.game_table.get_query_status(browser_game)
        selected_server = None
        if connect_game_valid and endpoint is not None:
            selected_server = self._find_selected_server(connect_game, endpoint)

        sensitivity = derive_action_sensitivity(
            browser_game_valid=browser_game_valid,
            connect_game_valid=connect_game_valid,
            endpoint_valid=endpoint is not None,
            query_status=query_status,
            selected_server_present=selected_server is not None,
        )
        action_widgets = {
            "preferences": "game-preferences-button",
            "refresh": "action-update-button",
            "connect": "action-connect-button",
            "info": "action-info-button",
        }
        for action, widget_name in action_widgets.items():
            self.gtk_widgets[widget_name].set_property(
                "sensitive", sensitivity[action])
        return sensitivity

    def cb_listed_widget_changed(self, *args):
        self.update_settings_table()

    def apply_settings_to_preferences_dialog(self, game: str, widget_option_mapping: dict, dynamic_settings_table: dict) -> None:
        for option in widget_option_mapping:
            value = self.core.game_table.get_game_settings(game)[option]
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
            self.core.game_table.set_game_setting(game, option, value)

    def cb_post_settings_genload(self, widget_table, group, option, value):
        self.widget_table = widget_table
        widget_name = widget_table[group][option]["gtk_widget_name"]
        widget = self.builder.get_object(widget_name)

        gtk_helpers.set_widget_value(widget, value)
        gtk_helpers.bind_widget_to_callback(widget, self.update_settings_table)


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

    def __init__(self, core_instance: core.Core) -> None:
        self._core = core_instance
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
            if self._core.geolocation is not None:
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
                self._core.load_geoip(path)
        finally:
            if callback is not None:
                callback()


class App(Gtk.Application):

    """App class."""

    def __init__(self, core_instance, settings_instance, services):
        Gtk.Application.__init__(self,
                                 application_id=APPLICATION_ID,
                                 flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)
        self.connect("shutdown", self.on_shutdown)

        # Create builder
        self.builder = Gtk.Builder()
        load_builder_resources(self.builder)

        self.core = core_instance
        self.settings = settings_instance
        self.services = services
        # The shutdown handler reads this whatever startup managed to do, so
        # it exists before the first thing that could fail.
        self.status = "starting"
        self.handler_failure = None
        self._pending_settings_warnings = []
        self._startup_dialogs_pending = False

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

            guiactions.fill_game_store()

            self._pending_settings_warnings = self.settings.load(
                callback_postgenload=self.guiactions.cb_post_settings_genload)

            # Connect signals
            self.builder.connect_signals(self.guiactions)
            guiactions.gtk_widgets["server-list-filter"].set_visible_func(
                guiactions.server_filter_func)

            gtk_helpers.set_widget_value(
                self.guiactions.gtk_widgets["game-combobox"], self.settings.settings_table["common"]["selected-game-browser"])
            for entry in self.guiactions.filter_secure_list:
                guiactions.gtk_widgets["filter-secure"].append(
                    entry["id"], entry["text"])
            guiactions.cb_game_treeview_togglebutton_clicked()
            guiactions.cb_server_filters_changed()
            guiactions.cb_server_connect_data_changed()

            # Add main window
            main_window = self.guiactions.gtk_widgets["main-window"]
            self.add_window(main_window)

            # Create menu actions
            about_action = Gio.SimpleAction.new("about", None)
            quit_action = Gio.SimpleAction.new("quit", None)

            about_action.connect(
                "activate", self.guiactions.cb_about, main_window)
            quit_action.connect("activate", self.guiactions.cb_quit, self)

            self.add_action(about_action)
            self.add_action(quit_action)

            self.set_app_menu(self.builder.get_object("app-menu"))

            gtk_helpers.set_object_properties(
                self.guiactions.gtk_widgets, GTK_STRING_TABLE)

            self.status = "up"
        except Exception as error:
            self._record_handler_failure(error)
            app.quit()

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

        Widget visibility belongs to the GtkBuilder document, so activation
        only presents. Startup admission is held from the first activation
        until the GeoIP coordinator reports the offer it accepted is finished,
        so an activation arriving meanwhile only presents the window again.
        """
        window = self.guiactions.gtk_widgets["main-window"]
        window.present()
        if self._startup_dialogs_pending:
            return

        pending_warnings = self._pending_settings_warnings
        self._pending_settings_warnings = []
        self._startup_dialogs_pending = True

        def release_admission():
            self._startup_dialogs_pending = False

        def show_startup_dialogs():
            offered = False
            try:
                for warning in pending_warnings:
                    self.guiactions.show_error_dialog(warning)
                offered = self.services.geoip_downloads.offer(
                    window, release_admission)
            finally:
                # An offer that never started promises no completion, so this
                # dispatcher is the last thing holding admission.
                if not offered:
                    release_admission()
            return False

        GLib.idle_add(show_startup_dialogs)

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
