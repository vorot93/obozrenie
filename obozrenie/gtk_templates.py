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

"""Contains templates for insertion into GUI body"""

from gi.repository import Gtk

import obozrenie.gtk_helpers as gtk_helpers
import obozrenie.i18n as i18n


def get_checkbutton(label_text="", tooltip_text=""):
    checkbutton = Gtk.CheckButton.new()

    widget_object_dict = {"checkbutton": checkbutton}
    widget_property_dict = {"checkbutton": {"label": i18n._(label_text), "tooltip-text": i18n._(tooltip_text)}}

    gtk_helpers.set_object_properties(widget_object_dict, widget_property_dict)

    widget_group = {"container": checkbutton, "substance": checkbutton}

    return widget_group


def get_entry_with_label(label_text="", tooltip_text=""):
    grid = Gtk.Grid()
    entry = Gtk.Entry()
    label = Gtk.Label()

    widget_object_dict = {"grid": grid, "entry": entry, "label": label}
    widget_property_dict = {"grid":  {"column-homogeneous": True,           "column-spacing": 5},
                            "entry": {"tooltip-text": i18n._(tooltip_text), "halign": Gtk.Align.FILL, "hexpand-set": True, "hexpand": True},
                            "label": {"label": i18n._(label_text),          "halign": Gtk.Align.END}}

    gtk_helpers.set_object_properties(widget_object_dict, widget_property_dict)

    grid.add(entry)

    widget_group = {"container": grid, "label": label, "substance": entry}

    return widget_group


def get_textview_with_label(label_text="", tooltip_text="Single entry per line", placeholder_text=""):
    grid = Gtk.Grid()
    text_view = Gtk.TextView()
    text_buffer = text_view.get_buffer()
    label = Gtk.Label()

    widget_object_dict = {"grid": grid, "text_view": text_view, "label": label}
    widget_property_dict = {"grid":      {"column-homogeneous": True,           "column-spacing": 5},
                            "text_view": {"tooltip-text": i18n._(tooltip_text), "halign": Gtk.Align.FILL, "hexpand-set": True, "hexpand": True, "left-margin": 8, "right-margin": 8},
                            "label":     {"label": i18n._(label_text),          "halign": Gtk.Align.END,  "valign": Gtk.Align.START}}

    gtk_helpers.set_object_properties(widget_object_dict, widget_property_dict)

    grid.add(text_view)

    widget_group = {"container": grid, "label": label, "substance": text_buffer}

    return widget_group


def get_option_widget(option_dict):
    name = option_dict["name"]
    description = option_dict["description"]
    widget_type = option_dict["gtk_type"]
    if widget_type == "CheckButton":
        widget = get_checkbutton(label_text=name, tooltip_text=description)
    elif widget_type == "Entry with Label":
        widget = get_entry_with_label(label_text=name+":", tooltip_text=description)
    elif widget_type == "Multiline Entry with Label":
        widget = get_textview_with_label(label_text=name+":", tooltip_text=description)
    else:
        print(i18n._("No widget generated for type %(widget_type)s") % {'widget_type': widget_type})
        widget = None

    return widget


def get_preferences_grid(game, game_settings, dynamic_settings_table):
    grid = Gtk.Grid()

    grid.insert_column(0)

    grid.set_orientation(Gtk.Orientation.VERTICAL)
    grid.set_property("row-spacing", 5)
    grid.set_property("column-spacing", 5)
    grid.set_property("margin-bottom", 10)

    widget_option_mapping = {}

    i = 0
    for option in game_settings:
        option_object = get_option_widget(dynamic_settings_table[option])

        try:
            grid.attach(option_object["label"], 0, i, 1, 1)
        except KeyError:
            grid.attach(Gtk.Label(), 0, i, 1, 1)
        grid.attach(option_object["container"], 1, i, 1, 1)

        widget_option_mapping[option] = option_object["substance"]
        i += 1
    del i
    preferences_grid_info = {"widget": grid, "mapping": widget_option_mapping}

    return preferences_grid_info


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, parent, game, game_info, game_settings, dynamic_settings_table, callback_start=None, callback_close=None):
        Gtk.Dialog.__init__(self, None, parent)

        self.callback_close = None

        if callback_close is not None:
            self.callback_close = callback_close

        self.game = game
        self.dynamic_settings_table = dynamic_settings_table

        preferences_grid_info = get_preferences_grid(game, game_settings, dynamic_settings_table)

        preferences_grid = preferences_grid_info["widget"]
        self.widget_option_mapping = preferences_grid_info["mapping"]

        if callback_start is not None:
            callback_start(self.game, self.widget_option_mapping, self.dynamic_settings_table)

        self.set_resizable(False)
        self.set_border_width(10)
        self.set_title(i18n._("%(game)s preferences") % {'game': game_info["name"]})
        self.get_content_area().pack_start(preferences_grid, True, True, 0)

        button = self.add_button(i18n._("Save"), Gtk.ResponseType.CLOSE)
        button.connect("clicked", self.cb_close_button_clicked)

        self.show_all()

    def cb_close_button_clicked(self, widget):
        if self.callback_close is not None:
            self.callback_close(self.game, self.widget_option_mapping, self.dynamic_settings_table)
        self.destroy()
