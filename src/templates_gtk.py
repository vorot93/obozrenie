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


def get_option_widget(option_dict):
    name = option_dict["name"]
    description = option_dict["description"]
    widget_type = option_dict["gtk_type"]
    if widget_type == "CheckButton":
        widget = get_checkbutton(label_text=name+":",
                                 tooltip_text=description)
    if widget_type == "Entry with Label":
        widget = get_entry_with_label(label_text=name+":",
                                      tooltip_text=description)
    else:
        print("No widget generated for type " + widget_type)
        widget = None

    return widget


def get_checkbutton(label_text="", tooltip_text=""):
    checkbutton = Gtk.CheckButton.new()

    checkbutton.set_label(label_text)
    checkbutton.set_tooltip_text(tooltip_text)

    return checkbutton, checkbutton


def get_entry_with_label(label_text="", tooltip_text=""):
    grid = Gtk.Grid()
    entry = Gtk.Entry()
    label = Gtk.Label()

    label.set_text(label_text)
    label.set_halign(Gtk.Align.START)

    entry.set_tooltip_text(tooltip_text)
    entry.set_halign(Gtk.Align.END)

    grid.add(label)
    grid.add(entry)
    grid.set_column_spacing(5)

    return grid, entry


def get_preferences_dialog(game, game_table, dynamic_settings_table):
    dialog = Gtk.Dialog()
    content_area = dialog.get_content_area()
    listbox = Gtk.ListBox()

    widget_option_mapping = {}

    for option in game_table[game]["settings"]:
        widget = get_option_widget(dynamic_settings_table[option])
        row = Gtk.ListBoxRow()

        widget_grid = widget[0]
        widget_main = widget[1]

        row.add(widget_grid)

        listbox.add(row)
        print("Adding widget " + str(type(widget_grid)) + " with tooltip " + widget_main.get_tooltip_text() + " to option " + option)
        widget_option_mapping[option] = widget_main

    content_area.pack_start(listbox, True, True, 0)

    return dialog, listbox, widget_option_mapping
