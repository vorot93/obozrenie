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

"""Simple and easy to use Game Server Browser."""

import argparse
import ast
import os
import threading

from gi.repository import GdkPixbuf, Gtk, Gio, GLib

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

APP_CONFIG = os.path.join(os.path.dirname(__file__), "obozrenie.ini")
GAME_CONFIG = os.path.join(os.path.dirname(__file__), "games.ini")
UI_PATH = os.path.join(os.path.dirname(__file__), "obozrenie-gtk.ui")
SCHEMA_BASE_ID = 'com.github.skybon.obozrenie'


class Callbacks:

    """Responses to events from GUI"""

    def __init__(self, app, builder):
        self.app = app
        self.builder = builder

    def cb_set_widget_sensitivity(self):
        """Sets sensitivity for dependent widgets."""
        pass

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        pass

    def cb_connect_button_clicked(self, *args):
        """Starts the game."""
        self.app.quit()
        start_game()

    @staticmethod
    def cb_about(action, data, dialog):
        """Opens the About dialog."""
        Gtk.Dialog.run(dialog)
        Gtk.Dialog.hide(dialog)

    def cb_quit(self, *args):
        """Exits the program."""
        self.app.quit()

    def cb_game_combobox_changed(self, listmodel, *data):
        listmodel.clear()
        # settings.switch_backend()

    def cb_update_button_clicked(self, listmodel, *data):
        """Refills the server list model"""
        ping_button = self.builder.get_object("PingingEnable_CheckButton")
        ping_column = self.builder.get_object("Ping_ServerList_TreeViewColumn")

        self.serverlist_update_button = self.builder.get_object("Update_Button")
        self.serverlist_model = self.builder.get_object("ServerList_Store")

        bool_ping = ping_button.get_active()

        Gtk.TreeViewColumn.set_visible(ping_column, bool_ping)

        self.serverlist_update_button.set_sensitive(False)

        self.serverlist_model.clear()

        core.update_server_list(bool_ping, self.fill_server_view)

    def fill_server_view(self, table):
        # Goodies for GUI
        for i in range(len(table[0][1])):
            entry = table[0][1][i].copy()

            # Game icon
            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/icons/games/' +
                    entry[backends.rigsofrods.core.MASTER_GAME_COLUMN[-1] - 1] +
                    '.png', 24, 24))
            except GLib.Error:
                print("Error appending flag icon for host: " + entry[
                    backends.rigsofrods.core.MASTER_HOST_COLUMN[-1] - 1])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/icons/games/' +
                    entry[backends.rigsofrods.core.MASTER_GAME_COLUMN[-1] - 1] +
                    '.png', 24, 24))

            # Lock icon
            if entry[backends.rigsofrods.core.MASTER_PASS_COLUMN[-1] - 1] == True:
                entry.append("network-wireless-encrypted-symbolic")
            else:
                entry.append(None)

            # Country flags
            if GEOIP_ENABLED is True:
                host = entry[
                    backends.rigsofrods.core.MASTER_HOST_COLUMN[-1] - 1].split(':')[0]
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
                    os.path.dirname(__file__) + '/icons/flags/' + country_code.lower() +
                    '.svg', 24, 18))
            except GLib.Error:
                print("Error appending flag icon of " + country_code + " for host: " + entry[
                    backends.rigsofrods.core.MASTER_HOST_COLUMN[-1] - 1])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/icons/flags/' + 'unknown' +
                    '.svg', 24, 18))

            # Total / max players
            entry.append(str(entry[backends.rigsofrods.core.MASTER_PLAYERCOUNT_COLUMN[-1] - 1]) +
                              '/' +
                              str(entry[backends.rigsofrods.core.MASTER_PLAYERLIMIT_COLUMN[-1] - 1]))

            treeiter = self.serverlist_model.append(entry)

        self.serverlist_update_button.set_sensitive(True)

    def cb_server_list_selection_changed(self, widget, *data):
        """Updates text in Entry on TreeView selection change."""
        entry_field = Gtk.Builder.get_object(self.builder, "ServerHost_Entry")

        model, treeiter = widget.get_selected()
        try:
            text = model[treeiter][backends.rigsofrods.core.MASTER_HOST_COLUMN[-1] - 1]
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
        self.cb_connect_button_clicked()


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.
    """

    def __init__(self, app):
        """Loads base variables into the class."""
        self.schema_base_id = SCHEMA_BASE_ID

        self.keyfile_config = GLib.KeyFile.new()
        self.keyfile_config.load_from_file(APP_CONFIG, GLib.KeyFileFlags.NONE)

        self.game_config = GLib.KeyFile.new()
        self.game_config.load_from_file(GAME_CONFIG, GLib.KeyFileFlags.NONE)

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

    def get_games_list_store(self):
        """
        Loads game list into a list store
        """
        table = core.create_games_table(self.game_config)

        self.game_store = self.builder.get_object("Game_Store")

        for i in range(len(table)):
            entry = table[i].copy()
            icon_base = os.path.dirname(__file__) + '/icons/games/'
            icon = entry[0] + '.png'
            icon_missing = "image-missing.png"

            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_base + icon, 24, 24))
            except GLib.Error:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_base + icon_missing, 24, 24))

            treeiter = self.game_store.append(entry)

    def load(self):
        """Loads configuration."""

        for i in range(self.keyfile_config.get_groups()[1]):
            # Define variables
            key = self.keyfile_config.get_groups()[0][i]
            group = self.keyfile_config.get_value(key, "group")
            widget = Gtk.Builder.get_object(self.builder,
                self.keyfile_config.get_value(key, "widget"))

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
                Gtk.Adjustment.set_value(widget, int(value))
            elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                try:
                    value = ast.literal_eval(value)
                except ValueError:
                    value = False

                Gtk.ToggleButton.set_active(widget, value)
                gsettings.bind(key, widget, "active", Gio.SettingsBindFlags.DEFAULT)
            elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
                Gtk.ComboBox.set_active_id(widget, str(value))
                gsettings.bind(key, widget, "active-id", Gio.SettingsBindFlags.DEFAULT)
            elif isinstance(widget, Gtk.Entry):
                Gtk.Entry.set_text(widget, str(value))
                gsettings.bind(key, widget, "text", Gio.SettingsBindFlags.DEFAULT)


class Core:
    """Contains core logic of the app."""
    def __init__(self):
        pass
    def create_games_table(self, game_config):
        """
        Loads game list into a table
        """
        # self.game_config = game_config
        self.game_table = []
        print("Forming game table for:")
        for i in range(game_config.get_groups()[1]):
            print(game_config.get_groups()[0][i] + "... ")

            self.game_table.append([])

            game_id = game_config.get_groups()[0][i]
            name = game_config.get_value(game_id, "name")
            backend = game_config.get_value(game_id, "backend")

            self.game_table[i].append(game_id)
            self.game_table[i].append(name)
            self.game_table[i].append(backend)
            print("OK!")

        return self.game_table


    def update_server_list(self, bool_ping, callback):
        """Updates server lists"""
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(bool_ping, callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, bool_ping, callback):
        """Separate update thread"""
        self.game_table[0][1] = backends.rigsofrods.core.stat_master(bool_ping)
        GLib.idle_add(callback, self.game_table)

    def get_server_info():
        pass

    def start_game(path):
        """Start game"""
        from subprocess import call

        try:
            call(path)
            return 0
        except OSError:
            print("Error launching the game.")


class App(Gtk.Application):

    """App class."""

    def __init__(self):
        Gtk.Application.__init__(self,
                                 application_id="com.github.skybon.obozrenie",
                                 flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)

        # Create builder
        self.builder = Gtk.Builder()
        Gtk.Builder.add_from_file(self.builder, UI_PATH)

        self.settings = Settings(self)

    def on_startup(self, app):
        """
        Startup function.
        Loads the GtkBuilder resources, settings and start the main loop.
        """

        # Load settings
        self.settings.get_games_list_store()
        self.settings.load()

        # Connect signals
        callbacks = Callbacks(self, self.builder)
        Gtk.Builder.connect_signals(self.builder, callbacks)

        # Menu actions
        about_dialog = Gtk.Builder.get_object(self.builder, "About_Dialog")
        about_action = Gio.SimpleAction.new("about", None)
        quit_action = Gio.SimpleAction.new("quit", None)

        about_action.connect("activate", callbacks.cb_about, about_dialog)
        quit_action.connect("activate", callbacks.cb_quit, self)

        self.add_action(about_action)
        self.add_action(quit_action)

        window = (Gtk.Builder.get_object(self.builder, "Main_Window"))
        menumodel = Gio.Menu()
        menumodel.append("About", "app.about")
        menumodel.append("Quit", "app.quit")
        self.set_app_menu(menumodel)
        self.add_window(window)

    def on_activate(self, app):
        window = (Gtk.Builder.get_object(self.builder, "Main_Window"))
        window.show_all()

if __name__ == "__main__":
    core = Core()
    app = App()
    app.run(None)
