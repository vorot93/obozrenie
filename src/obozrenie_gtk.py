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
import threading
import pdb

from gi.repository import GdkPixbuf, Gtk, Gio, GLib

from obozrenie_core import Core
import helpers

try:
    import pygeoip
    GEOIP_DATA = '/usr/share/GeoIP/GeoIP.dat'
    try:
        open(GEOIP_DATA)
        print("GeoIP data file", GEOIP_DATA, "opened successfully")
        GEOIP_ENABLED = True
    except:
        print("GeoIP data file not found. Disabling geolocation.")
        GEOIP_ENABLED = False
except ImportError:
    print("PyGeoIP not found. Disabling geolocation.")
    GEOIP_ENABLED = False

import backends

APP_CONFIG = os.path.join(os.path.dirname(__file__), "obozrenie_widgets.ini")
UI_PATH = os.path.join(os.path.dirname(__file__), "assets", "obozrenie_gtk.ui")
SCHEMA_BASE_ID = 'com.github.skybon.obozrenie'


class Callbacks:

    """Responses to events from GUI"""

    def __init__(self, app, builder, core_library):
        self.app = app
        self.builder = builder
        self.core = core_library
        self.core.game_table

        self.game_combobox = self.builder.get_object("Game_ComboBox")

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

    def cb_set_widgets_active(self, status):
        """Sets sensitivity and visibility for conditional widgets."""
        pass

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        pass

    def cb_connect_button_clicked(self, *args):
        """Starts the game."""
        self.app.quit()
        self.core.start_game()

    @staticmethod
    def cb_about(action, data, dialog):
        """Opens the About dialog."""
        Gtk.Dialog.run(dialog)
        Gtk.Dialog.hide(dialog)

    def cb_quit(self, *args):
        """Exits the program."""
        self.app.quit()

    def cb_game_combobox_changed(self, combobox, *data):
        """Actions on game combobox selection change."""
        game = combobox.get_active_id()

        game_index = helpers.search_dict_table(self.core.game_table, "id", game)

        self.serverlist_model.clear()
        if self.core.game_table[game_index]["servers"] == []:
            self.cb_update_button_clicked(combobox, *data)
        else:
            self.fill_server_view(self.core.game_table[game_index])

    def cb_update_button_clicked(self, combobox, *data):
        """Actions on server list update button click"""
        ping_button = self.builder.get_object("PingingEnable_CheckButton")
        ping_column = self.builder.get_object("Ping_ServerList_TreeViewColumn")

        game = combobox.get_active_id()
        bool_ping = ping_button.get_active()

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_loading_page)

        self.serverlist_update_button.set_sensitive(False)
        self.game_combobox.set_sensitive(False)

        Gtk.TreeViewColumn.set_visible(ping_column, bool_ping)

        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        a = self.serverhost_entry.get_text()
        self.serverlist_model.clear()
        self.serverhost_entry.set_text(a)

        self.core.update_server_list(game, bool_ping, self.fill_server_view)

    def get_games_list_store(self):
        """
        Loads game list into a list store
        """
        table = self.core.game_table.copy()

        self.game_store = self.builder.get_object("Game_Store")

        for i in range(len(table)):
            entry = []
            entry.append(table[i]["id"])
            entry.append(table[i]["info"]["name"])
            entry.append(table[i]["info"]["backend"])

            icon_base = os.path.dirname(__file__) + '/assets/icons/games/'
            icon = entry[0] + '.png'
            icon_missing = "image-missing.png"

            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_base + icon, 24, 24))
            except GLib.Error:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_base + icon_missing, 24, 24))

            treeiter = self.game_store.append(entry)

    def fill_server_view(self, game_table_slice):
        """Fill the server view"""
        self.view_format = ("game",
                            "player_count",
                            "player_limit",
                            "password",
                            "host",
                            "name",
                            "terrain",
                            "ping")

        server_table = helpers.dict_to_list(game_table_slice["servers"],
                                            self.view_format)

        # Goodies for GUI
        for i in range(len(server_table)):
            entry = server_table[i].copy()

            # Game icon
            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/assets/icons/games/' +
                    entry[0] + '.png', 24, 24))
            except GLib.Error:
                print("Error appending icon for host: " + entry[
                    self.view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/assets/icons/games/' +
                    entry[0] + '.png', 24, 24))

            # Lock icon
            if entry[self.view_format.index("password")] == True:
                entry.append("network-wireless-encrypted-symbolic")
            else:
                entry.append(None)

            # Country flags
            if GEOIP_ENABLED is True:
                host = entry[self.view_format.index("host")].split(':')[0]
                try:
                    country_code = pygeoip.GeoIP(
                        GEOIP_DATA).country_code_by_addr(host)
                except OSError:
                    country_code = pygeoip.GeoIP(
                        GEOIP_DATA).country_code_by_name(host)
                except:
                    country_code = 'unknown'
                if country_code == '':
                    country_code = 'unknown'
            else:
                country_code = 'unknown'
            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/assets/icons/flags/' +
                    country_code.lower() + '.svg', 24, 18))
            except GLib.Error:
                print("Error appending flag icon of " + country_code + " for host: " + entry[self.view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/assets/icons/flags/' + 'unknown' +
                    '.svg', 24, 18))

            # Total / max players
            entry.append(str(entry[1]) + '/' + str(entry[2]))

            treeiter = self.serverlist_model.append(entry)

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_servers_page)

        self.serverlist_update_button.set_sensitive(True)
        self.game_combobox.set_sensitive(True)

    def cb_server_list_selection_changed(self, widget, *data):
        """Updates text in Entry on TreeView selection change."""
        entry_field = self.serverhost_entry

        model, treeiter = widget.get_selected()
        try:
            text = model[treeiter][self.view_format.index("host")]
        except TypeError:
            return

        if text != "SERVER_FULL":
            try:
                entry_field.set_text(text)
            except TypeError:
                pass

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


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.
    """

    def __init__(self, app):
        """Loads base variables into the class."""
        self.schema_base_id = SCHEMA_BASE_ID

        self.keyfile_config = GLib.KeyFile.new()
        self.keyfile_config.load_from_file(APP_CONFIG, GLib.KeyFileFlags.NONE)

        self.builder = app.builder

    def get_setting_groups(self):
        """Compile a list of available settings groups."""
        mapping_cat = []
        for i in range(self.keyfile_config.get_groups()[1]):
            if (GLib.KeyFile.get_value(self.keyfile_config,
                                       GLib.KeyFile.get_groups(self.keyfile_config)[0][i], "group")
                    in mapping_cat) == False:

                mapping_cat.append(GLib.KeyFile.get_value(self.keyfile_config,
                                                          GLib.KeyFile.get_groups(self.keyfile_config)[0][i], "group"))
        return mapping_cat

    def switch_game_id(self):
        pass

    def load(self):
        """Loads configuration."""

        for i in range(self.keyfile_config.get_groups()[1]):
            # Define variables
            key = self.keyfile_config.get_groups()[0][i]
            group = self.keyfile_config.get_value(key, "group")
            widget = self.builder.get_object(self.keyfile_config.get_value(key, "widget"))

            schema_id = self.schema_base_id + "." + group

            # Receive setting
            gsettings = Gio.Settings.new(schema_id)
            if isinstance(widget, Gtk.Adjustment):
                value = gsettings.get_int(key)
            elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                value = gsettings.get_boolean(key)
            elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText) or isinstance(widget, Gtk.Entry):
                value = gsettings.get_string(key)

            # Apply setting to widget
            if isinstance(widget, Gtk.Adjustment):
                widget.set_value(int(value))
            elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                try:
                    value = ast.literal_eval(value)
                except ValueError:
                    value = False

                widget.set_active(value)
                gsettings.bind(key, widget, "active", Gio.SettingsBindFlags.DEFAULT)
            elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
                widget.set_active_id(str(value))
                gsettings.bind(key, widget, "active-id", Gio.SettingsBindFlags.DEFAULT)
            elif isinstance(widget, Gtk.Entry):
                widget.set_text(str(value))
                gsettings.bind(key, widget, "text", Gio.SettingsBindFlags.DEFAULT)
                if value == "":
                    widget.emit("changed")


class App(Gtk.Application):

    """App class."""

    def __init__(self, Core):
        Gtk.Application.__init__(self,
                                 application_id="com.github.skybon.obozrenie",
                                 flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)

        # Create builder
        self.builder = Gtk.Builder()
        self.builder.add_from_file(UI_PATH)

        self.core = Core()
        self.callbacks = Callbacks(self, self.builder, self.core)
        self.settings = Settings(self)

    def on_startup(self, app):
        """
        Startup function.
        Loads the GtkBuilder resources, settings and start the main loop.
        """

        # Connect signals
        self.builder.connect_signals(self.callbacks)

        # Load settings
        self.callbacks.get_games_list_store()
        self.settings.load()

        # Menu actions
        about_dialog = self.builder.get_object("About_Dialog")
        about_action = Gio.SimpleAction.new("about", None)
        quit_action = Gio.SimpleAction.new("quit", None)

        about_action.connect("activate", self.callbacks.cb_about, about_dialog)
        quit_action.connect("activate", self.callbacks.cb_quit, self)

        self.add_action(about_action)
        self.add_action(quit_action)

        window = self.builder.get_object("Main_Window")
        menumodel = Gio.Menu()
        menumodel.append("About", "app.about")
        menumodel.append("Quit", "app.quit")
        self.set_app_menu(menumodel)
        self.add_window(window)

    def on_activate(self, app):
        window = self.builder.get_object("Main_Window")
        window.show_all()

if __name__ == "__main__":
    app = App(Core)
    app.run(None)
