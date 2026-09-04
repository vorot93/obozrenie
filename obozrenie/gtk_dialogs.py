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

"""The main window's dialogs: errors, server information, about."""

from collections.abc import Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from obozrenie import gtk_lists, i18n, resources
from obozrenie.global_settings import VERSION
from obozrenie.global_strings import AUTHORS, COPYRIGHT, DESCRIPTION, PROJECT
from obozrenie.list_rows import PlayerRow
from obozrenie.presenter import ServerDetails


def load_template(filename: str) -> str:
    """Read one packaged UI document, translated for the selected locale.

    Translation happens here, before GTK parses the document, with the same
    catalog object Python strings use; the marker is removed so GTK cannot
    translate the text a second time.
    """
    return i18n.translate_builder_xml(resources.read_text("ui", filename))


def present_error(
    parent: Gtk.Widget,
    message: str,
    on_closed: Callable[[], None] | None = None,
) -> Adw.AlertDialog:
    """Show one modal error on `parent`; `on_closed` runs once, on its response.

    The handler disconnects itself before it calls back, so a second
    `response` — force-closing an `Adw.AlertDialog` emits its close response
    again — reaches nothing: the startup warning chain relies on each close
    advancing it exactly once.
    """
    dialog = Adw.AlertDialog(heading=i18n._("Error"), body=message)
    dialog.add_response("close", i18n._("Close"))
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    if on_closed is not None:
        def respond_once(responded, _response):
            responded.disconnect(handler)
            on_closed()

        handler = dialog.connect("response", respond_once)
    dialog.present(parent)
    return dialog


def present_about(parent: Gtk.Window, logo) -> Gtk.AboutDialog:
    """Show the about dialog with the packaged logo texture.

    `Adw.AboutDialog` accepts only icon-theme names, and the logo is a
    package resource the icon theme cannot serve, so this is `Gtk.AboutDialog`.
    """
    about = Gtk.AboutDialog(
        transient_for=parent,
        modal=True,
        program_name=PROJECT,
        comments=DESCRIPTION,
        version=VERSION,
        authors=AUTHORS,
        copyright=COPYRIGHT,
        license_type=Gtk.License.GPL_3_0,
        logo=logo,
        hide_on_close=False,
    )
    about.present()
    return about


@Gtk.Template(string=load_template("server_info_dialog.ui"))
class ServerInfoDialog(Adw.Dialog):

    """The details of one listed server, with Close and Connect."""

    __gtype_name__ = "ObozrenieServerInfoDialog"

    name_value = Gtk.Template.Child()
    host_value = Gtk.Template.Child()
    game_value = Gtk.Template.Child()
    terrain_value = Gtk.Template.Child()
    players_value = Gtk.Template.Child()
    ping_value = Gtk.Template.Child()
    player_view = Gtk.Template.Child()
    player_name_column = Gtk.Template.Child()
    player_score_column = Gtk.Template.Child()
    player_ping_column = Gtk.Template.Child()
    connect_button = Gtk.Template.Child()
    close_button = Gtk.Template.Child()

    def __init__(self, details: ServerDetails, on_connect: Callable[[], None]) -> None:
        super().__init__()
        self._on_connect = on_connect
        self.name_value.set_text(details.name)
        self.host_value.set_text(details.host)
        self.game_value.set_text(details.game_name)
        self.terrain_value.set_text(details.terrain)
        self.players_value.set_text(details.players_text)
        self.ping_value.set_text(details.ping_text)

        self.players = Gio.ListStore(item_type=PlayerRow)
        self.players.splice(0, 0, list(details.players))
        self.player_view.set_model(Gtk.NoSelection(model=self.players))
        self.player_name_column.set_factory(gtk_lists.label_factory(lambda row: row.name))
        self.player_score_column.set_factory(gtk_lists.label_factory(gtk_lists.score_text, xalign=1))
        self.player_ping_column.set_factory(gtk_lists.label_factory(
            lambda row: str(row.ping), xalign=1))

    @Gtk.Template.Callback()
    def _on_connect_clicked(self, _button) -> None:
        # Close first: the callback re-validates and may launch a game.
        self.close()
        self._on_connect()

    @Gtk.Template.Callback()
    def _on_close_clicked(self, _button) -> None:
        self.close()
