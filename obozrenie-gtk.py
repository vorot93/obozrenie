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

"""This is a PyGObject-based GUI for Rigs of Rods configurator."""

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

import backends.rigsofrods

APP_CONFIG = os.path.join(os.path.dirname(__file__), "obozrenie.ini")
UI_PATH = os.path.join(os.path.dirname(__file__), "obozrenie-gtk.ui")


class Callbacks:

    """Responses to events from GUI"""

    def __init__(self, backend, user_settings_file, builder, game_path):
        self.backend = backend
        self.user_settings_file = user_settings_file
        self.builder = builder
        self.game_path = game_path

    def cb_set_widget_sensitivity(self):
        """Sets sensitivity for dependent widgets."""
        pass

    def cb_info_button_clicked(self, *args):
        """Shows server information window."""
        pass

    def cb_connect_button_clicked(self, *args):
        """Starts the game."""
        Gtk.main_quit()
        start_game(self.game_path)

    @staticmethod
    def cb_about_item_clicked(self, window):
        """Opens the About dialog."""
        Gtk.Dialog.run(window)
        Gtk.Dialog.hide(window)

    @staticmethod
    def cb_quit(*args):
        """Exits the program."""
        Gtk.main_quit()

    def cb_update_button_clicked(self, listmodel, *data):
        """Refills the server list model"""
        ping_button = Gtk.Builder.get_object(
            self.builder, "PingingEnable_CheckButton")
        ping_column = Gtk.Builder.get_object(
            self.builder, "Ping_ServerList_TreeViewColumn")

        bool_ping = ping_button.get_active()

        Gtk.TreeViewColumn.set_visible(
            ping_column, bool_ping)

        App.update_server_list(listmodel, bool_ping)

    def cb_server_list_selection_changed(self, widget, *data):
        """Updates text in Entry on TreeView selection change."""
        entry = Gtk.Builder.get_object(self.builder, "ServerHost_Entry")

        model, treeiter = widget.get_selected()
        try:
            text = model[treeiter][backends.rigsofrods.MASTER_HOST_COLUMN[-1] - 1]
        except TypeError:
            return

        if text != "SERVER_FULL":
            try:
                Gtk.Entry.set_text(entry, text)
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

    def __init__(self, backend, user_settings_file, builder):
        """Loads base variables into the class."""
        self.schema_base_id = 'org.rigsofrods.rigsofrods'

        self.keyfile_config = GLib.KeyFile.new()
        self.keyfile_config.load_from_file(APP_CONFIG, GLib.KeyFileFlags.NONE)

        self.backend = backend
        self.user_settings_file = user_settings_file
        self.builder = builder

        if self.backend == "gkeyfile":
            self.keyfile = GLib.KeyFile.new()

            try:
                self.keyfile.load_from_file(
                    self.user_settings_file, GLib.KeyFileFlags.NONE)
            except GLib.Error:
                pass

    def get_groups(self):
        """Compile a list of available settings groups."""
        mapping_cat = []
        for i in range(self.keyfile_config.get_groups()[1]):
            if (GLib.KeyFile.get_value(self.keyfile_config,
                                       GLib.KeyFile.get_groups(self.keyfile_config)[0][i], "group")
                    in mapping_cat) == False:

                mapping_cat.append(GLib.KeyFile.get_value(self.keyfile_config,
                                                          GLib.KeyFile.get_groups(self.keyfile_config)[0][i], "group"))
        return mapping_cat

    def load(self):
        """Settings loading function. Supports GKeyFile and GSettings backends."""

        for i in range(GLib.KeyFile.get_groups(self.keyfile_config)[1]):
            # Define variables
            key = self.keyfile_config.get_groups()[0][i]
            group = self.keyfile_config.get_value(
                self.keyfile_config.get_groups()[0][i], "group")
            widget = Gtk.Builder.get_object(self.builder, self.keyfile_config.get_value(
                self.keyfile_config.get_groups()[0][i], "widget"))

            schema_id = self.schema_base_id + "." + group

            if self.backend == "gsettings":
                # GSettings magic
                gsettings = Gio.Settings.new(schema_id)
                if isinstance(widget, Gtk.Adjustment):
                    value = gsettings.get_int(key)
                elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                    value = gsettings.get_boolean(key)
                elif isinstance(widget, Gtk.ComboBoxText) or isinstance(widget, Gtk.Entry):
                    value = gsettings.get_string(key)
            elif self.backend == "gkeyfile":
                try:
                    value = self.keyfile.get_string(group, key)
                except GLib.Error:
                    continue

            if isinstance(widget, Gtk.Adjustment):
                Gtk.Adjustment.set_value(widget, int(value))
            elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                try:
                    value = ast.literal_eval(value)
                except ValueError:
                    value = False

                Gtk.ToggleButton.set_active(widget, value)
            elif isinstance(widget, Gtk.ComboBoxText):
                Gtk.ComboBox.set_active_id(widget, str(value))
            elif isinstance(widget, Gtk.Entry):
                Gtk.Entry.set_text(widget, str(value))

    def save(self):
        """Save selected configuration. Supports GKeyFile and GSettings backends."""
        print("\nYour selected configuration:\n--------------")

        for i in range(self.keyfile_config.get_groups()[1]):

            # Define variables
            key = self.keyfile_config.get_groups()[0][i]
            group = self.keyfile_config.get_value(
                self.keyfile_config.get_groups()[0][i], "group")
            widget = Gtk.Builder.get_object(self.builder, self.keyfile_config.get_value(
                self.keyfile_config.get_groups()[0][i], "widget"))

            if isinstance(widget, Gtk.Adjustment):
                value = int(Gtk.Adjustment.get_value(widget))
            elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
                value = bool(Gtk.ToggleButton.get_active(widget))
            elif isinstance(widget, Gtk.ComboBoxText):
                value = str(Gtk.ComboBox.get_active_id(widget))
            elif isinstance(widget, Gtk.Entry):
                value = str(Gtk.Entry.get_text(widget))

            if self.backend == "gsettings":
                schema_id = self.schema_base_id + "." + group
                gsettings = Gio.Settings.new(schema_id)
                if isinstance(value, bool):
                    gsettings.set_boolean(key, value)
                elif isinstance(value, int):
                    gsettings.set_int(key, value)
                elif isinstance(value, str):
                    gsettings.set_string(key, value)
            elif self.backend == "gkeyfile":
                self.keyfile.set_string(group, key, str(value))

            print(key, "=", value)

        if self.backend == "gkeyfile":
            self.keyfile.save_to_file(self.user_settings_file)


def start_game(path):
    """Start game"""
    from subprocess import call

    try:
        call(path)
        return 0
    except OSError:
        print("Error launching the game.")


class App:

    """App class."""

    def __init__(self):
        self.builder = Gtk.Builder()
        Gtk.Builder.add_from_file(self.builder, UI_PATH)

    @staticmethod
    def update_server_list(listmodel, bool_ping):
        """Updates server lists"""
        listmodel.clear()

        def pinging_target(listmodel, bool_ping):
            """Separate thread inside pinging callback"""
            listing = backends.rigsofrods.stat_master(
                backends.rigsofrods.MASTER_URL, bool_ping)

            # Goodies for GUI
            for i in range(len(listing)):
                # Game icon
                listing[i].append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/icons/games/' + listing[i][backends.rigsofrods.MASTER_GAME_COLUMN[-1] - 1] +
                    '.png', 24, 24))

                # Lock icon
                if listing[i][backends.rigsofrods.MASTER_PASS_COLUMN[-1] - 1] == True:
                    listing[i].append("network-wireless-encrypted-symbolic")
                else:
                    listing[i].append(None)

                # Country flags
                if GEOIP_ENABLED == True:
                    host = listing[i][
                        backends.rigsofrods.MASTER_HOST_COLUMN[-1] - 1].split(':')[0]
                    try:
                        country_code = pygeoip.GeoIP(
                            GEOIP_DATA).country_code_by_addr(host)
                    except OSError:
                        country_code = pygeoip.GeoIP(
                            GEOIP_DATA).country_code_by_name(host)
                    except:
                        country_code = 'unknown'
                else:
                    country_code = 'unknown'
                listing[i].append(GdkPixbuf.Pixbuf.new_from_file_at_size(
                    os.path.dirname(__file__) + '/icons/flags/' + country_code.lower() +
                    '.svg', 24, 18))

                # Total / max players
                listing[i].append(str(listing[i][backends.rigsofrods.MASTER_PLAYERCOUNT_COLUMN[-1] - 1]) +
                                  '/' + str(listing[i][backends.rigsofrods.MASTER_PLAYERLIMIT_COLUMN[-1] - 1]))

                treeiter = listmodel.append(listing[i])

        pinging_thread = threading.Thread(target=pinging_target, args=(listmodel, bool_ping))
        pinging_thread.daemon = True
        pinging_thread.start()

    def main(self):
        """Main function.
        Loads the GtkBuilder resources, settings and start the main loop.
        """
        cmd_parser = argparse.ArgumentParser()

        cmd_parser.add_argument("--backend",
                                type=str,
                                choices=["gkeyfile", "gsettings"],
                                default="gkeyfile",
                                help="Defines the storage backend to be used.")

        cmd_parser.add_argument("--settings-file",
                                type=str,
                                default="~/.config/obozrenie/settings.ini",
                                help="Profile path.")

        cmd_parser.add_argument("--config-file",
                                type=str, default="obozrenie.ini", help="Config file name.")

        cmd_parser.add_argument("--game-path",
                                type=str,
                                default=os.path.dirname(__file__), help="Game path.")

        self.backend = cmd_parser.parse_args().backend
        self.game_path = cmd_parser.parse_args().game_path
        self.user_settings_file = cmd_parser.parse_args().settings_file

        callbacks = Callbacks(
            self.backend, self.user_settings_file, self.builder, self.game_path)
        Gtk.Builder.connect_signals(self.builder, callbacks)

        Gtk.Window.show_all(
            Gtk.Builder.get_object(self.builder, "Main_Window"))

        settings = Settings(
            self.backend, self.user_settings_file, self.builder)
        settings.load()
        callbacks.cb_set_widget_sensitivity()

if __name__ == "__main__":
    app = App()
    app.main()
    Gtk.main()
