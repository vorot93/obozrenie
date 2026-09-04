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

"""The main window: a composite template that is the presenter's view.

Every user action reaches the presenter through `_notify`; every write the
presenter asks for runs under `_syncing`, so the window never reports a
change it was told to make. The browser game is the window's own state
(`_browser_game`): the sidebar list and the compact drop-down both show it,
and a search that hides the selected row does not lose it.
"""

from collections.abc import Callable, Mapping, Sequence

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from obozrenie import gtk_dialogs, gtk_icons, gtk_lists, gtk_preferences
from obozrenie.list_rows import GameRow, ServerRow
from obozrenie.presenter import PreferenceField, ServerDetails


# Secure filter drop-down position -> the value the presenter's criteria read.
SECURE_FILTER_CHOICES = ("None", "True", "False")


def _position_of(model, game_id) -> int:
    """The position of a game row in a list model, or the invalid position."""
    if game_id is not None:
        for position in range(model.get_n_items()):
            if model.get_item(position).game_id == game_id:
                return position
    return Gtk.INVALID_LIST_POSITION


@Gtk.Template(string=gtk_dialogs.load_template("main_window.ui"))
class MainWindow(Adw.ApplicationWindow):

    """The main window; see `presenter.MainView` for what it answers to."""

    __gtype_name__ = "ObozrenieMainWindow"

    sidebar_toggle = Gtk.Template.Child()
    compact_game_revealer = Gtk.Template.Child()
    compact_game_dropdown = Gtk.Template.Child()
    preferences_button = Gtk.Template.Child()
    menu_button = Gtk.Template.Child()
    sidebar_revealer = Gtk.Template.Child()
    game_search = Gtk.Template.Child()
    game_list = Gtk.Template.Child()
    pages = Gtk.Template.Child()
    server_view = Gtk.Template.Child()
    name_column = Gtk.Template.Child()
    host_column = Gtk.Template.Child()
    ping_column = Gtk.Template.Child()
    players_column = Gtk.Template.Child()
    game_mod_column = Gtk.Template.Child()
    game_type_column = Gtk.Template.Child()
    terrain_column = Gtk.Template.Child()
    welcome_page = Gtk.Template.Child()
    error_page = Gtk.Template.Child()
    filters_revealer = Gtk.Template.Child()
    filter_mod_label = Gtk.Template.Child()
    filter_type_label = Gtk.Template.Child()
    filter_terrain_label = Gtk.Template.Child()
    filter_ping_label = Gtk.Template.Child()
    filter_secure_label = Gtk.Template.Child()
    filter_mod = Gtk.Template.Child()
    filter_type = Gtk.Template.Child()
    filter_terrain = Gtk.Template.Child()
    filter_ping = Gtk.Template.Child()
    filter_secure = Gtk.Template.Child()
    filter_notfull = Gtk.Template.Child()
    filter_notempty = Gtk.Template.Child()
    filter_nopassword = Gtk.Template.Child()
    filters_toggle = Gtk.Template.Child()
    refresh_button = Gtk.Template.Child()
    connect_game_dropdown = Gtk.Template.Child()
    host_entry = Gtk.Template.Child()
    password_entry = Gtk.Template.Child()
    info_button = Gtk.Template.Child()
    connect_button = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._presenter = None
        self._browser_game = None
        self._syncing = False
        self.logo = gtk_icons.load_logo()
        self._action_buttons = {
            "preferences": self.preferences_button,
            "refresh": self.refresh_button,
            "connect": self.connect_button,
            "info": self.info_button,
        }
        self._build_game_models()
        self._build_server_models()
        self.filter_secure.connect("notify::selected", self._on_filters_changed)
        # GtkEditable.set_text() emits "changed" twice when it replaces
        # non-empty text (a delete then an insert), so host_entry's
        # connect-data notification is wired to "notify::text" here instead
        # of the "changed" signal main_window.ui declares for it; unlike
        # "changed", "notify::text" fires exactly once per set_text() call.
        self.host_entry.connect("notify::text", self._on_connect_data_changed)

    def _build_game_models(self) -> None:
        """One store behind the sidebar list and both game drop-downs."""
        self.game_store = Gio.ListStore(item_type=GameRow)
        name_expression = Gtk.PropertyExpression.new(GameRow, None, "name")
        self._game_filter = Gtk.StringFilter.new(name_expression)
        self._game_filter.set_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        self._game_filter.set_ignore_case(True)
        self._sidebar_model = Gtk.FilterListModel(model=self.game_store, filter=self._game_filter)
        self.sidebar_selection = Gtk.SingleSelection(
            model=self._sidebar_model, autoselect=False, can_unselect=True)
        self.sidebar_selection.connect("notify::selected-item", self._on_sidebar_selection_changed)
        self.game_list.set_model(self.sidebar_selection)
        self.game_list.set_factory(gtk_lists.game_row_factory(show_status=True))

        for dropdown in (self.compact_game_dropdown, self.connect_game_dropdown):
            dropdown.set_model(self.game_store)
            dropdown.set_expression(name_expression)
        self.compact_game_dropdown.set_factory(gtk_lists.game_row_factory(show_status=True))
        self.connect_game_dropdown.set_factory(gtk_lists.game_row_factory(show_status=False))
        self.compact_game_dropdown.connect("notify::selected", self._on_compact_game_selected)
        self.connect_game_dropdown.connect("notify::selected", self._on_connect_data_changed)

    def _build_server_models(self) -> None:
        """Store -> presenter filter -> column sorter -> single selection."""
        self.server_store = Gio.ListStore(item_type=ServerRow)
        self._server_filter = Gtk.CustomFilter.new(self._server_matches)
        filtered = Gtk.FilterListModel(model=self.server_store, filter=self._server_filter)
        sorted_servers = Gtk.SortListModel(model=filtered, sorter=self.server_view.get_sorter())
        self.server_selection = Gtk.SingleSelection(
            model=sorted_servers, autoselect=False, can_unselect=True)
        self.server_selection.connect("notify::selected-item", self._on_server_selection_changed)
        self.server_view.set_model(self.server_selection)

        def expression(name):
            return Gtk.PropertyExpression.new(ServerRow, None, name)

        for column, name in (
            (self.name_column, "name"),
            (self.host_column, "host"),
            (self.game_mod_column, "game-mod"),
            (self.game_type_column, "game-type"),
            (self.terrain_column, "terrain"),
        ):
            column.set_sorter(Gtk.StringSorter.new(expression(name)))
        self.ping_column.set_sorter(Gtk.NumericSorter.new(expression("ping")))
        self.players_column.set_sorter(Gtk.NumericSorter.new(expression("player-count")))

        self.name_column.set_factory(gtk_lists.server_name_factory())
        self.host_column.set_factory(gtk_lists.label_factory(
            lambda row: f"{row.flag} {row.host}".strip()))
        self.ping_column.set_factory(gtk_lists.label_factory(lambda row: str(row.ping), xalign=1))
        self.players_column.set_factory(gtk_lists.label_factory(gtk_lists.players_text, xalign=1))
        self.game_mod_column.set_factory(gtk_lists.label_factory(lambda row: row.game_mod))
        self.game_type_column.set_factory(gtk_lists.label_factory(lambda row: row.game_type))
        self.terrain_column.set_factory(gtk_lists.label_factory(lambda row: row.terrain))
        self.server_view.sort_by_column(self.ping_column, Gtk.SortType.ASCENDING)

    # MainView: wiring and reads

    def attach_presenter(self, presenter) -> None:
        self._presenter = presenter

    def browser_game(self) -> str | None:
        return self._browser_game

    def connect_game(self) -> str | None:
        item = self.connect_game_dropdown.get_selected_item()
        return None if item is None else item.game_id

    def host_text(self) -> str:
        return self.host_entry.get_text()

    def password_text(self) -> str:
        return self.password_entry.get_text()

    def sidebar_shown(self) -> bool:
        return self.sidebar_toggle.get_active()

    def filter_values(self) -> Mapping[str, object]:
        return {
            "mod": self.filter_mod.get_text(),
            "type": self.filter_type.get_text(),
            "terrain": self.filter_terrain.get_text(),
            "ping": int(self.filter_ping.get_value()),
            "secure": SECURE_FILTER_CHOICES[self.filter_secure.get_selected()],
            "notfull": self.filter_notfull.get_active(),
            "notempty": self.filter_notempty.get_active(),
            "nopassword": self.filter_nopassword.get_active(),
        }

    def selected_server(self) -> ServerRow | None:
        return self.server_selection.get_selected_item()

    # MainView: writes

    def set_games(self, rows: Sequence[GameRow]) -> None:
        self._syncing = True
        try:
            self.game_store.splice(0, self.game_store.get_n_items(), list(rows))
        finally:
            self._syncing = False
        self._sync_browser_selection()

    def set_servers(self, rows: Sequence[ServerRow]) -> None:
        self.server_store.splice(0, self.server_store.get_n_items(), list(rows))

    def select_browser_game(self, game_id: str | None) -> None:
        self._browser_game = game_id
        self._sync_browser_selection()

    def select_connect_game(self, game_id: str | None) -> None:
        self._write(self.connect_game_dropdown.set_selected, _position_of(self.game_store, game_id))

    def set_host_text(self, text: str) -> None:
        self._write(self.host_entry.set_text, text)

    def show_sidebar(self, shown: bool) -> None:
        self._write(self.sidebar_toggle.set_active, shown)

    def show_page(self, name: str) -> None:
        self.pages.set_visible_child_name(name)

    def set_error_text(self, text: str) -> None:
        self.error_page.set_description(text)

    def set_action_enabled(self, action: str, enabled: bool) -> None:
        self._action_buttons[action].set_sensitive(enabled)

    def refilter(self) -> None:
        self._server_filter.changed(Gtk.FilterChange.DIFFERENT)

    # MainView: dialogs

    def present_error(self, message: str, on_closed: Callable[[], None] | None = None) -> None:
        gtk_dialogs.present_error(self, message, on_closed)

    def present_server_info(self, details: ServerDetails, on_connect: Callable[[], None]) -> None:
        gtk_dialogs.ServerInfoDialog(details, on_connect).present(self)

    def present_preferences(
        self,
        title: str,
        fields: Sequence[PreferenceField],
        on_save: Callable[[Mapping[str, object]], None],
    ) -> None:
        gtk_preferences.PreferencesDialog(title, fields, on_save).present(self)

    def present_about(self) -> Gtk.AboutDialog:
        return gtk_dialogs.present_about(self, self.logo)

    # Internals

    def _write(self, setter, value) -> None:
        """Apply one presenter-requested write without reporting it back."""
        self._syncing = True
        try:
            setter(value)
        finally:
            self._syncing = False

    def _notify(self, event: str) -> None:
        if self._syncing or self._presenter is None:
            return
        getattr(self._presenter, event)()

    def _server_matches(self, row) -> bool:
        return self._presenter is None or self._presenter.server_matches(row)

    def _sync_browser_selection(self) -> None:
        """Show `_browser_game` in the sidebar and the compact drop-down."""
        self._syncing = True
        try:
            self.compact_game_dropdown.set_selected(
                _position_of(self.game_store, self._browser_game))
            self.sidebar_selection.set_selected(
                _position_of(self._sidebar_model, self._browser_game))
        finally:
            self._syncing = False

    def _browser_game_picked(self, item) -> None:
        """One selector chose a game: keep the other in step, report once."""
        if self._syncing or item is None or item.game_id == self._browser_game:
            return
        self._browser_game = item.game_id
        self._sync_browser_selection()
        self._notify("browser_game_changed")

    def _on_sidebar_selection_changed(self, selection, _pspec) -> None:
        self._browser_game_picked(selection.get_selected_item())

    def _on_compact_game_selected(self, dropdown, _pspec) -> None:
        self._browser_game_picked(dropdown.get_selected_item())

    def _on_server_selection_changed(self, *_args) -> None:
        self._notify("server_selected")

    def _on_connect_data_changed(self, *_args) -> None:
        self._notify("connect_data_changed")

    @Gtk.Template.Callback()
    def _on_sidebar_toggled(self, button) -> None:
        shown = button.get_active()
        self.sidebar_revealer.set_reveal_child(shown)
        self.compact_game_revealer.set_reveal_child(not shown)
        self._notify("sidebar_toggled")

    @Gtk.Template.Callback()
    def _on_game_search_changed(self, entry) -> None:
        self._game_filter.set_search(entry.get_text())
        self._sync_browser_selection()

    @Gtk.Template.Callback()
    def _on_filters_toggled(self, button) -> None:
        self.filters_revealer.set_reveal_child(button.get_active())

    @Gtk.Template.Callback()
    def _on_filters_changed(self, *_args) -> None:
        self._notify("filters_changed")

    @Gtk.Template.Callback()
    def _on_refresh_clicked(self, _button) -> None:
        self._notify("refresh_requested")

    @Gtk.Template.Callback()
    def _on_preferences_clicked(self, _button) -> None:
        self._notify("preferences_requested")

    @Gtk.Template.Callback()
    def _on_info_clicked(self, _button) -> None:
        self._notify("info_requested")

    @Gtk.Template.Callback()
    def _on_connect_clicked(self, _button) -> None:
        self._notify("connect_requested")

    @Gtk.Template.Callback()
    def _on_server_activated(self, _view, _position) -> None:
        self._notify("server_activated")
