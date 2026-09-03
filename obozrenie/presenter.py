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

"""The main window's behaviour, over a view protocol and without GTK.

`MainPresenter` decides everything the window shows and does; `MainView`
is the whole of what it may ask of the window. The window reports user
actions by calling the presenter's notification methods and answers the
protocol's reads from its widgets, so this module runs without a display.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from gi.repository import GLib

from obozrenie import i18n
from obozrenie.game_table import QueryStatus
from obozrenie.list_rows import GameRow, PlayerRow, ServerRow
from obozrenie.models import Endpoint


PAGES = ("servers", "welcome", "loading", "error")
ACTIONS = ("preferences", "refresh", "connect", "info")


class OptionKind(Enum):
    """How one per-game option is edited."""

    TEXT = "text"
    LINES = "lines"
    FLAG = "flag"


def game_options() -> dict[str, dict[str, object]]:
    """The per-game option schema: label, description, and kind per option."""
    return {
        "path": {
            "name": i18n._("Game path"),
            "description": i18n._("Path to the game."),
            "kind": OptionKind.TEXT,
        },
        "workdir": {
            "name": i18n._("Working directory"),
            "description": i18n._("Working directory of the game."),
            "kind": OptionKind.TEXT,
        },
        "master_uri": {
            "name": i18n._("Master URI list"),
            "description": i18n._("List of master servers to query."),
            "kind": OptionKind.LINES,
        },
        "steam_launch": {
            "name": i18n._("Launch via Steam"),
            "description": i18n._("Enables launching via Steam client. Required for VAC-enabled servers."),
            "kind": OptionKind.FLAG,
        },
        "steam_path": {
            "name": i18n._("Steam path"),
            "description": i18n._("Steam client path."),
            "kind": OptionKind.TEXT,
        },
        "nickname": {
            "name": i18n._("Nickname"),
            "description": i18n._("Your nickname."),
            "kind": OptionKind.TEXT,
        },
    }


@dataclass(frozen=True, slots=True)
class PreferenceField:
    """One editable per-game option and its current value."""

    option: str
    kind: OptionKind
    label: str
    description: str
    value: object


@dataclass(frozen=True, slots=True)
class ServerDetails:
    """What the server information dialog shows."""

    name: str
    host: str
    game_name: str
    terrain: str
    players_text: str
    ping_text: str
    players: tuple[PlayerRow, ...]


class MainView(Protocol):
    """What the presenter needs from the main window."""

    def attach_presenter(self, presenter: MainPresenter) -> None: ...
    def browser_game(self) -> str | None: ...
    def connect_game(self) -> str | None: ...
    def host_text(self) -> str: ...
    def password_text(self) -> str: ...
    def sidebar_shown(self) -> bool: ...
    def filter_values(self) -> Mapping[str, object]: ...
    def selected_server(self) -> ServerRow | None: ...
    def set_games(self, rows: Sequence[GameRow]) -> None: ...
    def set_servers(self, rows: Sequence[ServerRow]) -> None: ...
    def select_browser_game(self, game_id: str | None) -> None: ...
    def select_connect_game(self, game_id: str | None) -> None: ...
    def set_host_text(self, text: str) -> None: ...
    def show_sidebar(self, shown: bool) -> None: ...
    def show_page(self, name: str) -> None: ...
    def set_error_text(self, text: str) -> None: ...
    def set_action_enabled(self, action: str, enabled: bool) -> None: ...
    def refilter(self) -> None: ...
    def present_error(
        self, message: str, on_closed: Callable[[], None] | None = None,
    ) -> None: ...
    def present_server_info(
        self, details: ServerDetails, on_connect: Callable[[], None],
    ) -> None: ...
    def present_preferences(
        self,
        title: str,
        fields: Sequence[PreferenceField],
        on_save: Callable[[Mapping[str, object]], None],
    ) -> None: ...


def _as_game_id(value: object) -> str | None:
    if value is None or value in ("", "None"):
        return None
    return str(value)


def _as_text(value: object) -> str:
    return "" if value is None else str(value)


def _as_flag(value: object) -> bool:
    try:
        return bool(ast.literal_eval(str(value)))
    except (ValueError, SyntaxError):
        return False


# Persisted common option -> (view reader, view writer, writer coercion).
COMMON_OPTION_ACCESSORS = MappingProxyType({
    "selected-game-connect": ("connect_game", "select_connect_game", _as_game_id),
    "selected-game-browser": ("browser_game", "select_browser_game", _as_game_id),
    "server-host": ("host_text", "set_host_text", _as_text),
    "game-listview": ("sidebar_shown", "show_sidebar", _as_flag),
})

# A filter whose value is one of these is not applied.
_UNSET_FILTER_VALUES = (None, "", "None")
_SECURE_FILTER_VALUES = {"True": True, "False": False}


def _contains_text(row_value, filter_value) -> bool:
    return row_value is not None and filter_value in row_value


def _within_ceiling(row_value, filter_value) -> bool:
    return filter_value == 0 or row_value <= filter_value


def _is_chosen_boolean(row_value, filter_value) -> bool:
    return row_value is _SECURE_FILTER_VALUES[filter_value]


def _hidden_when_checked(row_value, filter_value) -> bool:
    return filter_value is not True or row_value is not True


@dataclass(frozen=True, slots=True)
class _FilterCriterion:
    key: str
    attribute: str
    matches: Callable[[object, object], bool]


_FILTER_CRITERIA = (
    _FilterCriterion("mod", "game_mod", _contains_text),
    _FilterCriterion("type", "game_type", _contains_text),
    _FilterCriterion("terrain", "terrain", _contains_text),
    _FilterCriterion("ping", "ping", _within_ceiling),
    _FilterCriterion("secure", "secure", _is_chosen_boolean),
    _FilterCriterion("notfull", "full", _hidden_when_checked),
    _FilterCriterion("notempty", "empty", _hidden_when_checked),
    _FilterCriterion("nopassword", "password", _hidden_when_checked),
)
FILTER_KEYS = tuple(criterion.key for criterion in _FILTER_CRITERIA)

# Query status -> the icon the game row shows; other statuses leave it.
_STATUS_ICONS = MappingProxyType({
    QueryStatus.WORKING: "emblem-synchronizing-symbolic",
    QueryStatus.READY: "emblem-ok-symbolic",
    QueryStatus.ERROR: "dialog-error-symbolic",
})


def derive_action_sensitivity(
    browser_game_valid: bool,
    connect_game_valid: bool,
    endpoint_valid: bool,
    query_status: QueryStatus | None,
    selected_server_present: bool,
) -> dict[str, bool]:
    """Derive action availability without reading or mutating view state."""
    return {
        "preferences": browser_game_valid,
        "refresh": (
            browser_game_valid
            and query_status is not QueryStatus.WORKING
        ),
        "connect": connect_game_valid and endpoint_valid,
        "info": (
            connect_game_valid
            and endpoint_valid
            and selected_server_present
        ),
    }


def _parse_endpoint(value: object) -> Endpoint | None:
    if type(value) is not str:
        return None
    try:
        return Endpoint.parse(value)
    except ValueError:
        return None


class MainPresenter:

    """Every behaviour of the main window, driving one `MainView`."""

    def __init__(self, view: MainView, core, settings, game_icons: Mapping[str, object]) -> None:
        self.view = view
        self.core = core
        self.settings = settings
        self.game_icons = game_icons
        self.game_options = game_options()
        self._game_rows: dict[str, GameRow] = {}
        self._filter_values: dict[str, object] = {}

    # Startup

    def load_games(self) -> None:
        """Build one row per configured game, sorted by display name."""
        game_table = self.core.game_table.get_game_table_copy()
        rows = [
            GameRow(game_id, game_table[game_id]["info"]["name"], self.game_icons.get(game_id))
            for game_id in game_table
        ]
        rows.sort(key=lambda row: row.name)
        self._game_rows = {row.game_id: row for row in rows}
        self.view.set_games(rows)

    def bind_settings(self) -> None:
        """Show every loaded common option through its view accessor."""
        common = self.settings.settings_table["common"]
        for option, (_reader, writer, coerce) in COMMON_OPTION_ACCESSORS.items():
            getattr(self.view, writer)(coerce(common[option]))

    def update_settings_table(self) -> None:
        """Copy every accessor's value into the settings table as text."""
        common = self.settings.settings_table["common"]
        for option, (reader, _writer, _coerce) in COMMON_OPTION_ACCESSORS.items():
            common[option] = str(getattr(self.view, reader)())

    def render_loaded_state(self) -> None:
        """Render the bound state once the view reports to this presenter."""
        self.browser_game_changed()
        self.sidebar_toggled()
        self.filters_changed()
        self.connect_data_changed()

    # View notifications

    def browser_game_changed(self) -> None:
        self.update_settings_table()
        game_id = self.settings.settings_table["common"]["selected-game-browser"]
        if game_id not in self.core.game_table.get_game_set():
            self.update_action_sensitivity()
            return
        query_status = self.core.game_table.get_query_status(game_id)
        self.update_action_sensitivity()
        if query_status is QueryStatus.EMPTY:  # Refresh server list on first access
            self.refresh_requested()
        else:
            if query_status is QueryStatus.WORKING:
                self.view.show_page("loading")
            GLib.idle_add(self.show_game_page, game_id)

    def connect_data_changed(self) -> None:
        self.update_settings_table()
        self.update_action_sensitivity()

    def sidebar_toggled(self) -> None:
        self.update_settings_table()

    def filters_changed(self) -> None:
        values = self.view.filter_values()
        self._filter_values = {key: values.get(key) for key in FILTER_KEYS}
        self.view.refilter()

    def refresh_requested(self) -> None:
        game = self.settings.settings_table["common"]["selected-game-browser"]
        if game not in self.core.game_table.get_game_set():
            return
        if self.core.game_table.get_query_status(game) is QueryStatus.WORKING:
            self.update_action_sensitivity()
            return

        admitted = self.core.update_server_list(game, stat_callback=self.on_query_finished)
        if not admitted:
            self.update_action_sensitivity()
            return
        query_status = self.core.game_table.get_query_status(game)
        if query_status is QueryStatus.WORKING:
            self.view.show_page("loading")
            self._set_status_icon(game, query_status)
        self.update_action_sensitivity()

    def preferences_requested(self) -> None:
        game = self.settings.settings_table["common"]["selected-game-browser"]
        if game not in self.core.game_table.get_game_set():
            return
        game_settings = self.core.game_table.get_game_settings(game)
        fields = []
        for option, value in game_settings.items():
            schema = self.game_options[option]
            kind = schema["kind"]
            if kind is OptionKind.LINES:
                value = "\n".join(value)
            elif kind is OptionKind.FLAG:
                value = _as_flag(value)
            else:
                value = _as_text(value)
            fields.append(PreferenceField(option, kind, schema["name"], schema["description"], value))
        title = i18n._("%(game)s preferences") % {
            "game": self.core.game_table.get_game_info(game)["name"],
        }
        self.view.present_preferences(
            title, tuple(fields), lambda values: self._save_preferences(game, values))

    def _save_preferences(self, game: str, values: Mapping[str, object]) -> None:
        for option, value in values.items():
            if self.game_options[option]["kind"] is OptionKind.LINES:
                value = value.split("\n")
            self.core.game_table.set_game_setting(game, option, value)

    def info_requested(self) -> None:
        common = self.settings.settings_table["common"]
        game = common["selected-game-connect"]
        endpoint = _parse_endpoint(common["server-host"])
        if game not in self.core.game_table.get_game_set() or endpoint is None:
            return
        server = self._find_selected_server(game, endpoint)
        if server is None:
            return
        details = ServerDetails(
            name=server.name,
            host=str(server.endpoint),
            game_name=self.core.game_table.get_game_info(server.game_id)["name"],
            terrain=server.terrain,
            players_text=i18n._("%(player_count)s / %(player_limit)s") % {
                "player_count": str(server.player_count),
                "player_limit": str(server.player_limit),
            },
            ping_text=str(server.ping),
            players=tuple(PlayerRow(player) for player in server.players),
        )
        self.view.present_server_info(
            details, lambda: self._connect_from_info(game, endpoint))

    def _connect_from_info(self, game: str, endpoint: Endpoint) -> None:
        """The information dialog's Connect: re-validate when it is pressed."""
        if game not in self.core.game_table.get_game_set():
            return
        if self._find_selected_server(game, endpoint) is None:
            return
        self.launch(game, str(endpoint), self.view.password_text())

    def connect_requested(self) -> None:
        common = self.settings.settings_table["common"]
        game = common["selected-game-connect"]
        endpoint = _parse_endpoint(common["server-host"])
        if game not in self.core.game_table.get_game_set() or endpoint is None:
            return
        self.launch(game, str(endpoint), self.view.password_text())

    def server_selected(self) -> None:
        """Copy the selected row into the Connect controls; no row changes nothing.

        The view never reports a write the presenter asked for, so the one
        Connect-data notification the copy deserves is issued here.
        """
        row = self.view.selected_server()
        if row is None:
            return
        self.view.set_host_text(row.host)
        self.view.select_connect_game(row.game_id)
        self.connect_data_changed()

    def server_activated(self) -> None:
        self.server_selected()
        self.connect_requested()

    # Filtering

    def server_matches(self, row: ServerRow) -> bool:
        """Report whether one server row satisfies every applied filter."""
        for criterion in _FILTER_CRITERIA:
            filter_value = self._filter_values.get(criterion.key)
            if filter_value in _UNSET_FILTER_VALUES:
                continue
            if not criterion.matches(getattr(row, criterion.attribute), filter_value):
                return False
        return True

    # Query presentation

    def on_query_finished(self, game: str) -> None:
        """Core's completion callback: render on the main loop."""
        GLib.idle_add(self.show_game_page, game)

    def show_game_page(self, game: str) -> bool:
        """Render one game's query state; the selected game gets its page."""
        game = str(game)
        query_status = self.core.game_table.get_query_status(game)
        server_table = self.core.game_table.get_servers_data(game)
        selected_game = self.settings.settings_table["common"]["selected-game-browser"]

        self._set_status_icon(game, query_status)
        if selected_game == game:
            if query_status is QueryStatus.READY:
                self.view.set_servers(self._server_rows(server_table))
                self.view.show_page("servers")
            elif query_status is QueryStatus.WORKING:
                self.view.show_page("loading")
            elif query_status is QueryStatus.ERROR:
                self.view.set_error_text(self.core.game_table.get_query_error(game))
                self.view.show_page("error")

        # In case selected server's existence is altered
        self.update_action_sensitivity()
        return False

    def _server_rows(self, servers) -> list[ServerRow]:
        rows = [ServerRow(server, self.game_icons.get(server.game_id)) for server in servers]
        rows.sort(key=lambda row: row.ping)
        return rows

    def _set_status_icon(self, game: str, status) -> None:
        icon = _STATUS_ICONS.get(status)
        row = self._game_rows.get(game)
        if icon is None or row is None:
            return
        row.status_icon = icon

    # Launching

    def launch(self, game: str, server: str, password: str) -> None:
        """Start the game; a failure is reported on the main loop, sanitized."""
        def launch_complete(error):
            if error is None:
                return
            details = str(error)
            if password:
                details = details.replace(password, "***")
            message = i18n._("Unable to launch the game: %(error)s") % {
                "error": details,
            }
            GLib.idle_add(self.show_launch_error, message)

        try:
            self.core.start_game(game, server, password, launch_complete)
        except Exception as error:
            launch_complete(error)

    def show_launch_error(self, message: str) -> bool:
        self.view.present_error(message)
        return False

    # Shared

    def _find_selected_server(self, game, endpoint):
        try:
            server_list_table = self.core.game_table.get_servers_data(game)
        except ValueError:
            return None
        return next(
            (
                server
                for server in server_list_table
                if server.game_id == game and server.endpoint == endpoint
            ),
            None,
        )

    def update_action_sensitivity(self) -> dict[str, bool]:
        common_settings = self.settings.settings_table["common"]
        games = self.core.game_table.get_game_set()
        browser_game = common_settings.get("selected-game-browser")
        connect_game = self.view.connect_game()
        endpoint = _parse_endpoint(self.view.host_text())

        browser_game_valid = browser_game in games
        connect_game_valid = connect_game in games
        query_status = None
        if browser_game_valid:
            query_status = self.core.game_table.get_query_status(browser_game)
        selected_server = None
        if connect_game_valid and endpoint is not None:
            selected_server = self._find_selected_server(connect_game, endpoint)

        sensitivity = derive_action_sensitivity(
            browser_game_valid=browser_game_valid,
            connect_game_valid=connect_game_valid,
            endpoint_valid=endpoint is not None,
            query_status=query_status,
            selected_server_present=selected_server is not None,
        )
        for action in ACTIONS:
            self.view.set_action_enabled(action, sensitivity[action])
        return sensitivity
