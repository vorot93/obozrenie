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

"""Contains templates for insertion into GUI body"""

from gi.repository import Gtk

import obozrenie.i18n as i18n


def get_checkbutton(label_text="", tooltip_text=""):
    checkbutton = Gtk.CheckButton.new()

    checkbutton.set_label(i18n._(label_text))
    checkbutton.set_tooltip_text(i18n._(tooltip_text))

    widget_group = {"container": checkbutton, "substance": checkbutton}

    return widget_group


def get_entry_with_label(label_text="", tooltip_text=""):
    grid = Gtk.Grid()
    entry = Gtk.Entry()
    label = Gtk.Label()

    label.set_text(i18n._(label_text))
    label.set_halign(Gtk.Align.START)

    entry.set_tooltip_text(i18n._(tooltip_text))
    entry.set_halign(Gtk.Align.END)

    grid.add(label)
    grid.add(entry)

    grid.set_column_homogeneous(True)
    grid.set_column_spacing(5)

    widget_group = {"container": grid, "substance": entry}

    return widget_group


def get_textview_with_label(label_text="", tooltip_text="Single entry per line", placeholder_text=""):
    grid = Gtk.Grid()
    text_view = Gtk.TextView()
    text_buffer = text_view.get_buffer()
    label = Gtk.Label()

    label.set_text(i18n._(label_text))
    label.set_halign(Gtk.Align.START)

    text_view.set_tooltip_text(i18n._(tooltip_text))
    text_view.set_halign(Gtk.Align.START)

    grid.add(label)
    grid.add(text_view)

    grid.set_column_homogeneous(True)
    grid.set_column_spacing(5)

    widget_group = {"container": grid, "substance": text_buffer}

    return widget_group


def get_option_widget(option_dict):
    name = option_dict["name"]
    description = option_dict["description"]
    widget_type = option_dict["gtk_type"]
    if widget_type == "CheckButton":
        widget = get_checkbutton(label_text=name+":", tooltip_text=description)
    elif widget_type == "Entry with Label":
        widget = get_entry_with_label(label_text=name+":", tooltip_text=description)
    elif widget_type == "Multiline Entry with Label":
        widget = get_textview_with_label(label_text=name+":", tooltip_text=description)
    else:
        print(i18n._("No widget generated for type %(widget_type)s") % {'widget_type': widget_type})
        widget = None

    return widget


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, parent, game, game_table, dynamic_settings_table, callback_start=None, callback_close=None):
        Gtk.Dialog.__init__(self, None, parent)

        self.callback_close = None

        if callback_close is not None:
            self.callback_close = callback_close

        self.game = game
        self.dynamic_settings_table = dynamic_settings_table

        preferences_grid_info = get_preferences_grid(game, game_table, dynamic_settings_table)

        preferences_grid = preferences_grid_info["widget"]
        self.widget_option_mapping = preferences_grid_info["mapping"]

        if callback_start is not None:
            callback_start(self.game, self.widget_option_mapping, self.dynamic_settings_table)

        self.set_resizable(False)
        self.set_border_width(10)
        self.set_title(i18n._("%(game)s preferences") % {'game': game_table[game]["info"]["name"]})
        self.get_content_area().pack_start(preferences_grid, True, True, 0)

        button = self.add_button(i18n._("Save"), Gtk.ResponseType.CLOSE)
        button.connect("clicked", self.cb_close_button_clicked)

        self.show_all()

    def cb_close_button_clicked(self, widget):
        if self.callback_close is not None:
            self.callback_close(self.game, self.widget_option_mapping, self.dynamic_settings_table)
        self.destroy()


class AboutDialog(Gtk.AboutDialog):
    def __init__(self, parent_window, project, description, website, version, authors, artists, copyright, license_type, icon):
        Gtk.AboutDialog.__init__(self, parent=parent_window)

        self.set_program_name(project)
        self.set_comments(description)
        self.set_website(website)
        self.set_version(version)
        self.set_authors(authors)
        self.set_artists(artists)
        self.set_copyright(copyright)
        self.set_license_type(license_type)
        self.set_logo_icon_name(icon)

        self.show_all()


def get_preferences_grid(game, game_table, dynamic_settings_table):
    grid = Gtk.Grid()

    grid.set_orientation(Gtk.Orientation.VERTICAL)
    grid.set_row_spacing(5)
    grid.set_margin_bottom(10)

    widget_option_mapping = {}

    for option in game_table[game]["settings"]:
        option_object = get_option_widget(dynamic_settings_table[option])

        grid.add(option_object["container"])

        widget_option_mapping[option] = option_object["substance"]

    preferences_grid_info = {"widget": grid, "mapping": widget_option_mapping}

    return preferences_grid_info
