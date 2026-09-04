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
from gi.repository import GObject, Gtk, Pango

from obozrenie import i18n


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
    return i18n._("%(player_count)s / %(player_limit)s") % {
        "player_count": str(row.player_count),
        "player_limit": str(row.player_limit),
    }


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
        # The status changes while the row is shown, so it is bound, not
        # copied; `unbind_game_cell` releases the binding with the row.
        # `status-icon` is `type=object` (so a `None` default sticks, unlike
        # `type=str`; see `list_rows.GameRow`), and GBinding has no built-in
        # transform from a boxed PyObject to a `gchararray`, so the value is
        # passed through explicitly instead of relying on the automatic one.
        item.status_binding = row.bind_property(
            "status-icon", children[2], "icon-name",
            GObject.BindingFlags.SYNC_CREATE,
            transform_to=lambda _binding, value: value)


def unbind_game_cell(item) -> None:
    binding = getattr(item, "status_binding", None)
    if binding is not None:
        binding.unbind()
        item.status_binding = None


def game_row_factory(show_status: bool) -> Gtk.SignalListItemFactory:
    """A factory rendering a game row: icon, name, and optionally its status."""
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _factory, item: setup_game_cell(item, show_status))
    factory.connect("bind", lambda _factory, item: bind_game_cell(item, show_status))
    factory.connect("unbind", lambda _factory, item: unbind_game_cell(item))
    return factory
