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
        self.game_model = self.builder.get_object("Game_Store")

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

    def cb_game_combobox_changed(self, combobox, *data):
        """Actions on game combobox selection change."""
        game = self.app.settings.settings_table["common"]["selected-game"]

        self.serverlist_model.clear()
        if self.core.game_table[game]["servers"] == []:
            self.cb_update_button_clicked(combobox, *data)
        else:
            self.fill_server_view(game, self.core.game_table[game])

    def cb_update_button_clicked(self, combobox, *data):
        """Actions on server list update button click"""
        ping_button = self.builder.get_object("PingingEnable_CheckButton")
        ping_column = self.builder.get_object("Ping_ServerList_TreeViewColumn")

        game = self.app.settings.settings_table["common"]["selected-game"]

        self.serverlist_notebook.set_property("page", self.serverlist_notebook_loading_page)

        self.serverlist_update_button.set_sensitive(False)
        self.game_combobox.set_sensitive(False)

        # UGLY HACK!
        # Workaround for chaotic TreeViewSelection on ListModel erase
        a = self.serverhost_entry.get_text()
        self.serverlist_model.clear()
        self.serverhost_entry.set_text(a)

        self.core.update_server_list(game, self.fill_server_view)

    def get_games_list_store(self):
        """
        Loads game list into a list store
        """
        table = self.core.game_table.copy()

        self.game_store = self.builder.get_object("Game_Store")

        for game_id in table:
            entry = []
            entry.append(game_id)
            entry.append(table[game_id]["info"]["name"])
            entry.append(table[game_id]["info"]["backend"])

            icon = game_id + '.png'
            icon_missing = "image-missing.png"

            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon), 24, 24))
            except GLib.Error:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon_missing), 24, 24))

            treeiter = self.game_store.append(entry)

    def fill_server_view(self, game, game_table_slice):
        """Fill the server view"""
        self.view_format = ("game_type",
                            "player_count",
                            "player_limit",
                            "password",
                            "host",
                            "name",
                            "terrain",
                            "ping",
                            "country")

        server_table = helpers.dict_to_list(game_table_slice["servers"],
                                            self.view_format)

        # Goodies for GUI
        for i in range(len(server_table)):
            entry = server_table[i].copy()

            # Game icon
            try:
                if entry[self.view_format.index("game_type")] is not None:
                    entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, entry[self.view_format.index("game_type")] + '.png'), 24, 24))
                else:
                    entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, game + '.png'), 24, 24))
            except GLib.Error:
                icon_missing = "image-missing.png"
                print(N_("Error appending game type icon for game type"), self.view_format.index("game_type"), "; host:", entry[self.view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_GAMES_DIR, icon_missing), 24, 24))

            # Lock icon
            if entry[self.view_format.index("password")] == True:
                entry.append("network-wireless-encrypted-symbolic")
            else:
                entry.append(None)

            # Country flags
            try:
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_FLAGS_DIR, entry[self.view_format.index("country")].lower() + '.svg'), 24, 18))
            except GLib.Error:
                print(N_("Error appending flag icon of ") + entry[self.view_format.index("country")] + " for host: " + entry[self.view_format.index("host")])
                entry.append(GdkPixbuf.Pixbuf.new_from_file_at_size(os.path.join(ICON_FLAGS_DIR, 'unknown' + '.svg'), 24, 18))

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

    def cb_listed_widget_changed(self, *data):
        self.update_settings_table()

    def apply_settings_to_preferences_dialog(self, game, widget_option_mapping):
        for option in widget_option_mapping:
            self.set_widget_value(widget_option_mapping[option], self.core.game_table[game]["settings"][option])

    def update_settings_table(self, *data):
        for group in self.widget_table:
            for option in self.widget_table[group]:
                # Define variables
                widget = self.builder.get_object(self.widget_table[group][option]["gtk_widget_name"])

                self.app.settings.settings_table[group][option] = str(self.get_widget_value(widget))

    def update_game_settings_table(self, game, widget_option_mapping, *args):
        for i in widget_option_mapping:
            self.core.game_table[game]["settings"][i] = self.get_widget_value(widget_option_mapping[i])

    def cb_post_settings_genload(self, widget_table, group, option, value):
        self.widget_table = widget_table
        widget = self.builder.get_object(widget_table[group][option]["gtk_widget_name"])

        self.set_widget_value(widget, value)
        self.bind_widget_to_callback(widget, self.update_settings_table)

    # Static methods
    @staticmethod
    def set_widget_value(widget, value):
        """Applies setting to widget."""
        if value == 'None':
            value is None
        if isinstance(widget, Gtk.Adjustment):
            widget.set_value(int(value))
        elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
            try:
                value = ast.literal_eval(value)
            except ValueError:
                value = False
            widget.set_active(value)

        elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
            widget.set_active_id(str(value))

        elif isinstance(widget, Gtk.Entry):
            widget.set_text(str(value))
            if value == "":
                widget.emit("changed")

    @staticmethod
    def get_widget_value(widget):
        """Fetches widget setting."""
        if isinstance(widget, Gtk.Adjustment):
            value = widget.get_value()
        elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
            value = widget.get_active()
        elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
            value = widget.get_active_id()
        elif isinstance(widget, Gtk.Entry):
            value = widget.get_text()

        return value

    @staticmethod
    def bind_widget_to_callback(widget, callback, *data):
        if isinstance(widget, Gtk.Entry) or isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
            widget.connect("changed", callback, data)

        elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
            widget.connect("clicked", callback, data)


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
        self.guiactions.get_games_list_store()
        self.settings.load(callback_postgenload=self.guiactions.cb_post_settings_genload)

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

if __name__ == "__main__":
    app = App(Core)
    app.run(None)
