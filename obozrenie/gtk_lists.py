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

"""List-item factories that render the rows of `list_rows`.

The cell functions take the list item itself so they can be exercised with
a stand-in; the factories only route the factory signals to them.
"""

from collections.abc import Callable

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from obozrenie import presenter


LOCK_ICON = "network-wireless-encrypted-symbolic"
SECURE_ICON = "security-high-symbolic"


def box_children(box) -> list:
    """The children of a box, first to last."""
    children = []
    child = box.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    return children


def label_factory(text_of: Callable[[object], str], xalign: float = 0.0) -> Gtk.SignalListItemFactory:
    """A factory rendering one row as the text `text_of` derives from it."""
    factory = Gtk.SignalListItemFactory()
    factory.connect(
        "setup",
        lambda _factory, item: item.set_child(
            Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)))
    factory.connect(
        "bind",
        lambda _factory, item: item.get_child().set_text(text_of(item.get_item())))
    return factory


def players_text(row) -> str:
    """One server row's player count, as the column view's text function."""
    return presenter.players_text(row.player_count, row.player_limit)


def setup_server_name_cell(item) -> None:
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Image(pixel_size=16))
    box.append(Gtk.Image(icon_name=LOCK_ICON))
    box.append(Gtk.Image(icon_name=SECURE_ICON))
    box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END))
    item.set_child(box)


def bind_server_name_cell(item) -> None:
    row = item.get_item()
    icon, lock, shield, label = box_children(item.get_child())
    icon.set_from_paintable(row.game_icon)
    lock.set_visible(row.password)
    shield.set_visible(row.secure)
    label.set_text(row.name)


def server_name_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _factory, item: setup_server_name_cell(item))
    factory.connect("bind", lambda _factory, item: bind_server_name_cell(item))
    return factory


def setup_game_cell(item, show_status: bool) -> None:
    box = Gtk.Box(spacing=6)
    box.append(Gtk.Image(pixel_size=24))
    box.append(Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
    if show_status:
        box.append(Gtk.Image(pixel_size=16))
    item.set_child(box)


def bind_game_cell(item, show_status: bool) -> None:
    row = item.get_item()
    children = box_children(item.get_child())
    children[0].set_from_paintable(row.icon)
    children[1].set_text(row.name)
    if show_status:
        # The status changes while the row is shown, so it is tracked, not
        # copied; `unbind_game_cell` disconnects the handler with the row.
        # A `bind_property` transform cannot stand in for this: PyGObject
        # treats a `None` return from a transform function as "no
        # conversion performed" and leaves the target property untouched,
        # so a status that clears back to `None` would never reach the
        # image, and a cell recycled onto a row with no status would keep
        # showing the previous game's. A `notify::status-icon` handler sets
        # every value, `None` included, so the status icon is applied once
        # up front and again on every change.
        status = children[2]
        status.set_from_icon_name(row.status_icon)
        item.status_row = row
        item.status_handler = row.connect(
            "notify::status-icon",
            lambda emitter, _pspec: status.set_from_icon_name(emitter.status_icon))


def unbind_game_cell(item) -> None:
    row = getattr(item, "status_row", None)
    handler = getattr(item, "status_handler", None)
    if row is not None and handler is not None:
        row.disconnect(handler)
    item.status_row = None
    item.status_handler = None


def game_row_factory(show_status: bool) -> Gtk.SignalListItemFactory:
    """A factory rendering a game row: icon, name, and optionally its status."""
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _factory, item: setup_game_cell(item, show_status))
    factory.connect("bind", lambda _factory, item: bind_game_cell(item, show_status))
    factory.connect("unbind", lambda _factory, item: unbind_game_cell(item))
    return factory
