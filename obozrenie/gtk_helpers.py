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

"""Useful functions for GTK+ UI"""

import ast

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GLib, GdkPixbuf, Gtk


def _scale_keeping_aspect(loader, image_width, image_height, box):
    """size-prepared handler: fit within box, preserving aspect ratio."""
    box_width, box_height = box
    scale = min(box_width / image_width, box_height / image_height)
    loader.set_size(max(1, round(image_width * scale)),
                    max(1, round(image_height * scale)))


def _read_icon_bytes(icon, icon_format):
    """Read an icon resource, normalising problematic SVGs.

    Several bundled flag SVGs place a large comment/RDF metadata block
    before the <svg> root element. That trips both gdk-pixbuf format
    detection (which only sniffs the first ~1 KiB) and the SVG decoder
    itself. Dropping everything before <svg> makes them load.
    """
    data = icon.read_bytes()
    if icon_format == "svg":
        start = data.find(b"<svg")
        if start > 0:
            data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + data[start:]
    return data


def load_icon(icon, icon_format, width=None, height=None):
    data = _read_icon_bytes(icon, icon_format)
    loader = GdkPixbuf.PixbufLoader.new_with_type(icon_format)
    if width is not None and height is not None:
        loader.connect(
            "size-prepared",
            _scale_keeping_aspect,
            (width, height),
        )
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


def get_icon_for_entry(entry, icon_formats, icon_root, width, height):
    """Load an icon pixbuf scaled to fit within width x height.

    Uses an explicitly-typed PixbufLoader fed normalised bytes rather than
    Pixbuf.new_from_file_at_size, so SVGs with a leading metadata block
    still load. Aspect ratio is preserved, matching the previous behaviour.
    """
    for icon_format in icon_formats:
        icon_resource = icon_root.joinpath(
            entry.lower() + "." + icon_format
        )
        if not icon_resource.is_file():
            continue
        try:
            icon = load_icon(
                icon_resource,
                icon_format,
                width,
                height,
            )
        except (GLib.GError, OSError):
            continue
        if icon is not None:
            return icon

    raise FileNotFoundError


def get_icon_dict(key_list, icon_formats, icon_root, width, height):
    """Loads icon pixbufs into memory for later usage"""
    icon_dict = {}
    fallback = None
    for entry in key_list:
        try:
            icon_dict[entry] = get_icon_for_entry(
                entry, icon_formats, icon_root, width, height)
        except FileNotFoundError:
            if fallback is None:
                fallback = get_icon_for_entry(
                    "image-missing",
                    icon_formats,
                    icon_root,
                    width,
                    height,
                )
            icon_dict[entry] = fallback

    return icon_dict


def country_code_to_flag_emoji(code):
    """Convert an ISO 3166-1 alpha-2 country code to a flag emoji.

    Returns "" for empty, None, "unknown", or otherwise invalid input, so
    the result can be used directly as cell text (an empty string shows no
    flag).
    """
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def get_object_dict(builder, object_mapping):
    object_dict = {}

    builder_object = builder.get_object

    for entry in object_mapping:
        object_dict[object_mapping[entry]] = builder_object(entry)

    return object_dict


def set_object_properties(object_dict, property_dict):
    for entry in property_dict:
        widget = object_dict[entry]
        for property_entry in property_dict[entry]:
            widget.set_property(
                property_entry, property_dict[entry][property_entry])


def get_notebook_page_dict(notebook, widget_mapping):
    """Get mapping for notebook pages."""
    notebook_pages = {}
    for entry in widget_mapping:
        notebook_pages[entry] = notebook.page_num(widget_mapping[entry])

    return notebook_pages


def search_model(model, column, value):
    row_index = None
    for row in range(len(model)):
        if model[row][column] == value:
            row_index = row
            break
    return row_index


def set_widget_value(widget, value, treeview_colnum=0):
    """Applies setting to widget."""
    if value == 'None':
        value = None
    if isinstance(widget, Gtk.Adjustment):
        widget.set_property("value", int(value))
    elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
        try:
            value = ast.literal_eval(str(value))
        except ValueError:
            value = False
        widget.set_property("active", bool(value))
    elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
        widget.set_property("active-id", str(value))
    elif isinstance(widget, Gtk.Entry):
        if value is None:
            value = ""
        widget.set_property("text", str(value))
        if value == "":
            widget.emit("changed")
    elif isinstance(widget, Gtk.Label):
        widget.set_property("label", str(value))
    elif isinstance(widget, Gtk.TreeView):
        model = widget.get_model()
        rownum = search_model(model, treeview_colnum, value)
        if rownum is not None:
            widget.get_selection().select_path(rownum)
    elif isinstance(widget, Gtk.TextBuffer):
        widget.set_property("text", value)


def get_widget_value(widget):
    """Fetches widget setting."""
    value = None
    if isinstance(widget, Gtk.Adjustment):
        value = widget.get_property("value")
    elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
        value = widget.get_property("active")
    elif isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
        value = widget.get_property("active-id")
    elif isinstance(widget, Gtk.Entry):
        value = widget.get_property("text")
    elif isinstance(widget, Gtk.Label):
        value = widget.get_property("label")
    elif isinstance(widget, Gtk.TreeView):
        selection = None
        if isinstance(widget, Gtk.TreeSelection):
            selection = widget
        else:
            selection = widget.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter is not None:
            value = tuple(model[treeiter])
    elif isinstance(widget, Gtk.TextBuffer):
        value = widget.get_property("text")

    return value


def bind_widget_to_callback(widget, callback, *data):
    if isinstance(widget, Gtk.Entry) or isinstance(widget, Gtk.ComboBox) or isinstance(widget, Gtk.ComboBoxText):
        widget.connect("changed", callback, data)

    elif isinstance(widget, Gtk.CheckButton) or isinstance(widget, Gtk.ToggleButton):
        widget.connect("clicked", callback, data)

    elif isinstance(widget, Gtk.TreeView):
        widget.get_selection().connect("changed", callback, data)


def sort_dict_table(dict_table: list, sort_key) -> list:
    sorted_dict_list = sorted(dict_table, key=lambda k: k[sort_key])

    return sorted_dict_list


def dict_to_list(dict_table, key_list):
    list_table = []

    if dict_table is not None:
        for entry in dict_table:
            list_table.append([])

            for key in key_list:
                try:
                    list_table[-1].append(entry[key])
                except KeyError:
                    list_table[-1].append(None)

    return list_table
