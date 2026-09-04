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

"""The presenter over a fake view: no widget, no display."""

import unittest
from types import SimpleNamespace
from unittest import mock

from obozrenie import game_table, i18n, presenter, resources
from obozrenie import settings as settings_module
from obozrenie.list_rows import ServerRow
from obozrenie.models import Endpoint, Player, Server


class FakeView:
    """Record every call the presenter makes and answer every read."""

    def __init__(self):
        self.calls = []
        self.presenter = None
        self.games = []
        self.servers = []
        self.browser = None
        self.connect = None
        self.host = ""
        self.password = ""
        self.sidebar = True
        self.filters = {}
        self.selected = None
        self.page = None
        self.error_text = None
        self.enabled = {}
        self.refilters = 0
        self.errors = []
        self.infos = []
        self.preferences = []

    def attach_presenter(self, presenter_instance):
        self.presenter = presenter_instance

    def browser_game(self):
        return self.browser

    def connect_game(self):
        return self.connect

    def host_text(self):
        return self.host

    def password_text(self):
        return self.password

    def sidebar_shown(self):
        return self.sidebar

    def filter_values(self):
        return dict(self.filters)

    def selected_server(self):
        return self.selected

    def set_games(self, rows):
        self.games = list(rows)
        self.calls.append(("set_games", [row.game_id for row in rows]))

    def set_servers(self, rows):
        self.servers = list(rows)
        self.calls.append(("set_servers", [row.host for row in rows]))

    def select_browser_game(self, game_id):
        self.browser = game_id
        self.calls.append(("select_browser_game", game_id))

    def select_connect_game(self, game_id):
        self.connect = game_id
        self.calls.append(("select_connect_game", game_id))

    def set_host_text(self, text):
        self.host = text
        self.calls.append(("set_host_text", text))

    def show_sidebar(self, shown):
        self.sidebar = shown
        self.calls.append(("show_sidebar", shown))

    def show_page(self, page):
        # The presenter names pages through the enum, never a bare string.
        assert isinstance(page, presenter.Page), page
        self.page = page
        self.calls.append(("show_page", page))

    def set_error_text(self, text):
        self.error_text = text
        self.calls.append(("set_error_text", text))

    def set_action_enabled(self, action, enabled):
        self.enabled[action] = enabled

    def refilter(self):
        self.refilters += 1

    def present_error(self, message, on_closed=None):
        self.errors.append((message, on_closed))

    def present_server_info(self, details, on_connect):
        self.infos.append((details, on_connect))

    def present_preferences(self, title, fields, on_save):
        self.preferences.append((title, fields, on_save))


class RecordingCore:
    def __init__(self, table):
        self.game_table = table
        self.launches = []
        self.refreshes = []

    def start_game(self, game, server, password, callback=None):
        self.launches.append((game, server, password, callback))
        return True

    def update_server_list(self, game, stat_callback=None):
        self.refreshes.append((game, stat_callback))
        return self.game_table.begin_query(game) is not None


def make_game_table(servers=()):
    table = game_table.GameTable({
        "browser": {
            "name": "Browser Game",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": ["path", "master_uri", "steam_launch"],
        },
        "connect": {
            "name": "Connect Game",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": [],
        },
    })
    servers = list(servers)
    if servers:
        token = table.begin_query("connect")
        table.finish_query("connect", token, servers)
    return table


def make_presenter(host="example.org:30000", servers=(), icons=None, table=None):
    if table is None:
        table = make_game_table(servers)
    view = FakeView()
    view.connect = "connect"
    view.host = host
    settings = SimpleNamespace(settings_table={
        "common": {
            "selected-game-browser": "browser",
            "selected-game-connect": "connect",
            "server-host": host,
            "game-listview": True,
        },
    })
    instance = presenter.MainPresenter(view, RecordingCore(table), settings, icons or {})
    view.attach_presenter(instance)
    return instance, view


class ActionSensitivityTests(unittest.TestCase):
    def derive(self, **state):
        return presenter.derive_action_sensitivity(**state)

    def test_empty_and_independent_selections_gate_their_own_actions(self):
        cases = (
            (
                "empty",
                dict(browser_game_valid=False, connect_game_valid=False, endpoint_valid=False,
                     query_status=None, selected_server_present=False),
                {"preferences": False, "refresh": False, "connect": False, "info": False},
            ),
            (
                "browser only",
                dict(browser_game_valid=True, connect_game_valid=False, endpoint_valid=False,
                     query_status=game_table.QueryStatus.EMPTY, selected_server_present=False),
                {"preferences": True, "refresh": True, "connect": False, "info": False},
            ),
            (
                "connect only",
                dict(browser_game_valid=False, connect_game_valid=True, endpoint_valid=True,
                     query_status=None, selected_server_present=True),
                {"preferences": False, "refresh": False, "connect": True, "info": True},
            ),
        )
        for label, state, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(self.derive(**state), expected)

    def test_working_query_disables_only_refresh_for_otherwise_valid_state(self):
        self.assertEqual(
            self.derive(browser_game_valid=True, connect_game_valid=True, endpoint_valid=True,
                        query_status=game_table.QueryStatus.WORKING, selected_server_present=True),
            {"preferences": True, "refresh": False, "connect": True, "info": True},
        )

    def test_info_requires_a_matching_server_but_connect_does_not(self):
        self.assertEqual(
            self.derive(browser_game_valid=True, connect_game_valid=True, endpoint_valid=True,
                        query_status=game_table.QueryStatus.READY, selected_server_present=False),
            {"preferences": True, "refresh": True, "connect": True, "info": False},
        )

    def test_presenter_applies_all_actions_and_uses_canonical_endpoint_rules(self):
        server = Server(endpoint=Endpoint("example.org", 30000), game_id="connect")
        instance, view = make_presenter(host="example.org", servers=[server])

        sensitivity = instance.update_action_sensitivity()

        self.assertEqual(sensitivity, {"preferences": True, "refresh": True, "connect": False, "info": False})
        self.assertEqual(view.enabled, sensitivity)

        view.host = str(server.endpoint)
        instance.core.game_table.begin_query("browser")
        sensitivity = instance.update_action_sensitivity()

        self.assertEqual(sensitivity, {"preferences": True, "refresh": False, "connect": True, "info": True})
        self.assertEqual(view.enabled, sensitivity)


class ConnectTests(unittest.TestCase):
    def test_both_connect_paths_read_the_current_transient_password(self):
        server = Server(endpoint=Endpoint("example.org", 30000), game_id="connect")
        instance, view = make_presenter(servers=[server])

        view.password = "first secret"
        instance.connect_requested()
        instance.info_requested()
        self.assertEqual(len(view.infos), 1)
        _details, on_connect = view.infos[0]
        view.password = "second secret"
        on_connect()

        self.assertEqual(
            [launch[:3] for launch in instance.core.launches],
            [
                ("connect", "example.org:30000", "first secret"),
                ("connect", "example.org:30000", "second secret"),
            ],
        )
        self.assertEqual(instance.settings.settings_table["common"], {
            "selected-game-browser": "browser",
            "selected-game-connect": "connect",
            "server-host": "example.org:30000",
            "game-listview": True,
        })

    def test_connect_paths_reject_invalid_endpoint_and_stale_server(self):
        server = Server(endpoint=Endpoint("example.org", 30000), game_id="connect")
        instance, view = make_presenter(host="example.org", servers=[server])
        common = instance.settings.settings_table["common"]

        instance.connect_requested()
        instance.info_requested()
        self.assertEqual(view.infos, [])

        common["selected-game-connect"] = "retired"
        common["server-host"] = "example.org:30000"
        instance.connect_requested()
        instance.info_requested()

        # The info dialog's Connect re-validates when it is pressed: a server
        # that vanished meanwhile launches nothing.
        common["selected-game-connect"] = "connect"
        instance.info_requested()
        self.assertEqual(len(view.infos), 1)
        token = instance.core.game_table.begin_query("connect")
        instance.core.game_table.finish_query("connect", token, [])
        view.infos[0][1]()

        self.assertEqual(instance.core.launches, [])

    def test_info_details_convert_the_record_and_keep_a_missing_score(self):
        server = Server(
            endpoint=Endpoint("2001:db8::50", 30000),
            game_id="connect",
            name="Details",
            player_count=1,
            player_limit=16,
            ping=21,
            terrain="Map",
            players=(Player("first", 7, 20), Player("second", None, 30)),
        )
        instance, view = make_presenter(host=str(server.endpoint), servers=[server])

        instance.info_requested()

        details, _on_connect = view.infos[0]
        self.assertEqual(details.name, "Details")
        self.assertEqual(details.host, "[2001:db8::50]:30000")
        self.assertEqual(details.game_name, "Connect Game")
        self.assertEqual(details.terrain, "Map")
        self.assertEqual(details.players_text, "1 / 16")
        self.assertEqual(details.ping_text, "21")
        self.assertEqual(
            [(player.name, player.score, player.ping) for player in details.players],
            [("first", 7, 20), ("second", None, 30)],
        )

    def test_info_presentation_failure_is_not_hidden(self):
        server = Server(endpoint=Endpoint("example.org", 30000), game_id="connect")
        instance, view = make_presenter(servers=[server])
        broken = RuntimeError("dialog rejected the details")
        view.present_server_info = mock.Mock(side_effect=broken)

        with self.assertRaises(RuntimeError) as raised:
            instance.info_requested()

        self.assertIs(raised.exception, broken)

    def test_server_selection_copies_host_and_game_and_activation_connects(self):
        server = Server(endpoint=Endpoint("example.org", 30000), game_id="connect")
        instance, view = make_presenter(host="", servers=[server])
        view.selected = ServerRow(server)

        instance.server_selected()
        self.assertEqual(view.host, "example.org:30000")
        self.assertEqual(view.connect, "connect")
        # The copy is followed by one Connect-data notification, so the
        # settings and the action buttons follow the selection.
        self.assertEqual(instance.settings.settings_table["common"]["server-host"], "example.org:30000")
        self.assertIs(view.enabled["connect"], True)

        view.selected = None
        view.host = "kept.example:1"
        instance.server_selected()
        self.assertEqual(view.host, "kept.example:1")

        view.selected = ServerRow(server)
        view.password = "pw"
        instance.server_activated()
        self.assertEqual(
            [launch[:3] for launch in instance.core.launches],
            [("connect", "example.org:30000", "pw")],
        )


class LaunchErrorTests(unittest.TestCase):
    def test_worker_failure_schedules_sanitized_dialog_on_main_loop(self):
        password = "open sesame"
        instance, view = make_presenter()

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.launch("g", "example.org:30000", password)
            _game, _server, _password, callback = instance.core.launches[0]
            self.assertIsNotNone(callback, "Core must be given a launch completion callback")
            callback(OSError(f"client rejected password {password}"))

        self.assertEqual(view.errors, [])
        idle_add.assert_called_once()
        scheduled, message = idle_add.call_args.args
        self.assertEqual(scheduled, instance.show_launch_error)
        self.assertNotIn(password, message)
        self.assertIn("***", message)

        self.assertIs(scheduled(message), False)
        self.assertEqual(view.errors, [(message, None)])

    def test_successful_worker_callback_does_not_schedule_a_dialog(self):
        instance, view = make_presenter()

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.launch("g", "example.org:30000", "")
            instance.core.launches[0][3](None)

        idle_add.assert_not_called()
        self.assertEqual(view.errors, [])

    def test_synchronous_validation_failure_uses_scheduled_error_path(self):
        password = "open sesame"
        instance, view = make_presenter()
        instance.core = mock.Mock()
        instance.core.start_game.side_effect = ValueError(f"invalid endpoint containing {password}")

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.launch("g", "", password)

        self.assertEqual(view.errors, [])
        idle_add.assert_called_once()
        scheduled, message = idle_add.call_args.args
        self.assertEqual(scheduled, instance.show_launch_error)
        self.assertNotIn(password, message)


class BrowserGameTests(unittest.TestCase):
    def test_preferences_and_refresh_reject_an_invalid_game_and_a_working_refresh(self):
        instance, view = make_presenter()
        common = instance.settings.settings_table["common"]

        common["selected-game-browser"] = "retired"
        instance.preferences_requested()
        instance.refresh_requested()

        self.assertEqual(view.preferences, [])
        self.assertEqual(instance.core.refreshes, [])
        self.assertEqual(view.calls, [])

        common["selected-game-browser"] = "browser"
        instance.core.game_table.begin_query("browser")
        view.enabled["refresh"] = True
        instance.refresh_requested()

        self.assertEqual(instance.core.refreshes, [])
        self.assertEqual(view.calls, [])
        self.assertIs(view.enabled["refresh"], False)

    def test_first_access_of_an_empty_game_refreshes_and_a_ready_game_schedules_its_page(self):
        instance, view = make_presenter()
        view.browser = "browser"

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.browser_game_changed()

        self.assertEqual([game for game, _callback in instance.core.refreshes], ["browser"])
        self.assertIs(view.page, presenter.Page.LOADING)
        idle_add.assert_not_called()

        token = instance.core.game_table.begin_query("connect")
        instance.core.game_table.finish_query("connect", token, [])
        view.browser = "connect"
        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.browser_game_changed()

        self.assertEqual(instance.settings.settings_table["common"]["selected-game-browser"], "connect")
        idle_add.assert_called_once_with(instance.show_game_page, "connect")

    def test_an_invalid_browser_selection_only_recomputes_sensitivity(self):
        instance, view = make_presenter()
        view.browser = None

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.browser_game_changed()

        self.assertEqual(instance.settings.settings_table["common"]["selected-game-browser"], "None")
        self.assertEqual(instance.core.refreshes, [])
        idle_add.assert_not_called()
        self.assertIs(view.enabled["preferences"], False)


class QueryStateTests(unittest.TestCase):
    def make(self):
        instance, view = make_presenter()
        view.browser = "browser"
        instance.load_games()
        view.page = "current-page"
        view.error_text = "existing error"
        return instance, view

    def status_icon(self, instance, game):
        return instance._game_rows[game].status_icon

    def test_admitted_working_query_disables_refresh_and_renders_working(self):
        instance, view = self.make()

        instance.refresh_requested()

        self.assertIs(instance.core.game_table.get_query_status("browser"), game_table.QueryStatus.WORKING)
        self.assertIs(view.page, presenter.Page.LOADING)
        self.assertEqual(self.status_icon(instance, "browser"), "emblem-synchronizing-symbolic")
        self.assertIs(view.enabled["refresh"], False)

    def test_rejected_racing_duplicate_preserves_presentation_and_recomputes_sensitivity(self):
        instance, view = self.make()

        class RacingDuplicateCore(RecordingCore):
            def update_server_list(self, game, stat_callback=None):
                self.refreshes.append((game, stat_callback))
                self.game_table.begin_query(game)
                return False

        instance.core = RacingDuplicateCore(instance.core.game_table)
        view.enabled.update({"preferences": False, "refresh": True, "connect": False, "info": True})

        instance.refresh_requested()

        self.assertEqual(len(instance.core.refreshes), 1)
        self.assertIs(instance.core.game_table.get_query_status("browser"), game_table.QueryStatus.WORKING)
        self.assertEqual(view.page, "current-page")
        self.assertIsNone(self.status_icon(instance, "browser"))
        self.assertEqual(view.enabled, {"preferences": True, "refresh": False, "connect": True, "info": False})

    def test_synchronous_terminal_admission_never_renders_working(self):
        instance, view = self.make()
        safe_error = "Safe synchronous query failure."

        class SynchronousFailureCore(RecordingCore):
            def update_server_list(self, game, stat_callback=None):
                self.refreshes.append((game, stat_callback))
                token = self.game_table.begin_query(game)
                if token is not None:
                    self.game_table.fail_query(game, token, safe_error)
                    if stat_callback is not None:
                        stat_callback(game)
                return token is not None

        instance.core = SynchronousFailureCore(instance.core.game_table)
        view.enabled["refresh"] = False

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.refresh_requested()

        idle_add.assert_called_once_with(instance.show_game_page, "browser")
        self.assertIs(instance.core.game_table.get_query_status("browser"), game_table.QueryStatus.ERROR)
        self.assertEqual(view.page, "current-page")
        self.assertIsNone(self.status_icon(instance, "browser"))
        self.assertIs(view.enabled["refresh"], True)

    def test_worker_completion_only_schedules_exact_error_rendering(self):
        instance, view = self.make()
        safe_error = "Safe stored query failure."
        token = instance.core.game_table.begin_query("browser")
        instance.core.game_table.fail_query("browser", token, safe_error)
        view.enabled["refresh"] = False

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.on_query_finished("browser")

            self.assertEqual(view.page, "current-page")
            self.assertEqual(view.error_text, "existing error")
            self.assertIs(view.enabled["refresh"], False)
            idle_add.assert_called_once_with(instance.show_game_page, "browser")
            scheduled, game = idle_add.call_args.args
            self.assertIs(scheduled(game), False)

        self.assertEqual(view.error_text, safe_error)
        self.assertIs(view.page, presenter.Page.ERROR)
        self.assertEqual(self.status_icon(instance, "browser"), "dialog-error-symbolic")
        self.assertIs(view.enabled["refresh"], True)

    def test_ready_page_fills_rows_sorted_by_ping_with_game_icons(self):
        icons = {"browser": object()}
        instance, view = make_presenter(icons=icons)
        view.browser = "browser"
        instance.load_games()
        servers = [
            Server(endpoint=Endpoint("slow.example", 1), game_id="browser", ping=90),
            Server(endpoint=Endpoint("2001:db8::40", 30000), game_id="browser", ping=10, country="US"),
        ]
        token = instance.core.game_table.begin_query("browser")
        instance.core.game_table.finish_query("browser", token, servers)

        self.assertIs(instance.show_game_page("browser"), False)

        self.assertIs(view.page, presenter.Page.SERVERS)
        self.assertEqual([row.host for row in view.servers], ["[2001:db8::40]:30000", "slow.example:1"])
        self.assertIs(view.servers[0].game_icon, icons["browser"])
        self.assertEqual(view.servers[0].flag, "\U0001F1FA\U0001F1F8")
        self.assertEqual(self.status_icon(instance, "browser"), "emblem-ok-symbolic")

    def test_another_games_completion_updates_its_icon_only(self):
        instance, view = self.make()
        token = instance.core.game_table.begin_query("connect")
        instance.core.game_table.finish_query("connect", token, [])

        instance.show_game_page("connect")

        self.assertEqual(view.page, "current-page")
        self.assertEqual(self.status_icon(instance, "connect"), "emblem-ok-symbolic")


class GameListTests(unittest.TestCase):
    def test_load_games_builds_rows_sorted_by_name_with_icons(self):
        icons = {"browser": object(), "connect": object()}
        instance, view = make_presenter(icons=icons)

        instance.load_games()

        self.assertEqual(view.calls, [("set_games", ["browser", "connect"])])
        self.assertEqual([row.name for row in view.games], ["Browser Game", "Connect Game"])
        self.assertIs(view.games[0].icon, icons["browser"])
        self.assertTrue(all(row.status_icon is None for row in view.games))


class SettingsBindingTests(unittest.TestCase):
    def make(self):
        instance, view = make_presenter()
        view.browser = "browser"
        return instance, view

    def test_bind_settings_shows_every_common_option_through_its_accessor(self):
        instance, view = self.make()
        instance.settings.settings_table["common"]["game-listview"] = "False"

        instance.bind_settings()

        self.assertEqual(view.calls, [
            ("select_connect_game", "connect"),
            ("select_browser_game", "browser"),
            ("set_host_text", "example.org:30000"),
            ("show_sidebar", False),
        ])

    def test_bind_settings_coerces_unset_games_none_host_and_text_flags(self):
        instance, view = self.make()
        instance.settings.settings_table["common"].update({
            "selected-game-connect": "",
            "selected-game-browser": "None",
            "server-host": None,
            "game-listview": True,
        })

        instance.bind_settings()

        self.assertEqual(view.calls, [
            ("select_connect_game", None),
            ("select_browser_game", None),
            ("set_host_text", ""),
            ("show_sidebar", True),
        ])

    def test_update_settings_table_stores_accessor_values_as_text(self):
        instance, view = self.make()
        view.sidebar = True

        instance.update_settings_table()

        self.assertEqual(instance.settings.settings_table["common"], {
            "selected-game-connect": "connect",
            "selected-game-browser": "browser",
            "server-host": "example.org:30000",
            "game-listview": "True",
        })

    def test_update_settings_table_records_an_empty_selection_as_none(self):
        instance, view = self.make()
        view.browser = None

        instance.update_settings_table()

        self.assertEqual(instance.settings.settings_table["common"]["selected-game-browser"], "None")

    def test_notifications_copy_settings_back(self):
        instance, view = self.make()
        view.sidebar = False
        instance.sidebar_toggled()
        self.assertEqual(instance.settings.settings_table["common"]["game-listview"], "False")

        view.host = "other.example:2"
        instance.connect_data_changed()
        self.assertEqual(instance.settings.settings_table["common"]["server-host"], "other.example:2")
        self.assertIn("connect", view.enabled)

    def test_render_loaded_state_renders_every_bound_control_once(self):
        instance, view = self.make()
        instance.load_games()
        view.calls.clear()
        view.filters = {"mod": "x"}

        with mock.patch.object(presenter.GLib, "idle_add") as idle_add:
            instance.render_loaded_state()

        # Startup shows the welcome page and renders the bound controls,
        # and asks for nothing else: no query, no page of its own.
        self.assertEqual(instance.core.refreshes, [])
        idle_add.assert_not_called()
        self.assertIs(view.page, presenter.Page.WELCOME)
        self.assertEqual(view.refilters, 1)
        self.assertEqual(instance._filter_values["mod"], "x")
        self.assertEqual(instance.settings.settings_table["common"]["game-listview"], "True")

    def test_accessor_table_and_persisted_option_table_cover_the_same_keys(self):
        self.assertEqual(
            set(presenter.COMMON_OPTION_ACCESSORS),
            set(settings_module.COMMON_OPTIONS["common"]),
        )


class ServerFilterTests(unittest.TestCase):
    def make(self, **filters):
        instance, view = make_presenter()
        view.filters = filters
        instance.filters_changed()
        self.assertEqual(view.refilters, 1)
        return instance

    @staticmethod
    def row(**fields):
        values = dict(endpoint=Endpoint("h.example", 1), game_id="g")
        values.update(fields)
        return ServerRow(Server(**values))

    def test_unset_filters_pass_every_row(self):
        instance = self.make(mod=None, type="", terrain="None", ping=0, secure="None",
                             notfull=False, notempty=False, nopassword=False)
        self.assertTrue(instance.server_matches(self.row(password=True, player_count=0, player_limit=0)))

    def test_substring_filters_require_the_text(self):
        instance = self.make(mod="ins", type="ctf", terrain="q3")
        self.assertTrue(instance.server_matches(self.row(game_mod="insta", game_type="ctf", terrain="q3dm")))
        self.assertFalse(instance.server_matches(self.row(game_mod="vanilla", game_type="ctf", terrain="q3dm")))

    def test_ping_ceiling_applies_only_when_nonzero(self):
        instance = self.make(ping=50)
        self.assertTrue(instance.server_matches(self.row(ping=50)))
        self.assertFalse(instance.server_matches(self.row(ping=51)))
        self.assertTrue(self.make(ping=0).server_matches(self.row(ping=9999)))

    def test_secure_filter_matches_the_chosen_boolean_exactly(self):
        self.assertTrue(self.make(secure="True").server_matches(self.row(secure=True)))
        self.assertFalse(self.make(secure="True").server_matches(self.row(secure=False)))
        self.assertTrue(self.make(secure="False").server_matches(self.row(secure=False)))

    def test_checked_hide_filters_reject_only_true_rows(self):
        instance = self.make(notfull=True, notempty=True, nopassword=True)
        self.assertTrue(instance.server_matches(self.row(player_count=1, player_limit=2)))
        self.assertFalse(instance.server_matches(self.row(player_count=2, player_limit=2)))
        self.assertFalse(instance.server_matches(self.row(player_count=0, player_limit=2)))
        self.assertFalse(instance.server_matches(self.row(player_count=1, player_limit=2, password=True)))


class PreferencesTests(unittest.TestCase):
    def test_packaged_game_defaults_keep_every_declared_nickname(self):
        declared = presenter.game_options()

        self.assertIn("nickname", declared)
        self.assertEqual(
            declared["nickname"],
            presenter.OptionSchema(
                presenter.OptionKind.TEXT, i18n._("Nickname"), i18n._("Your nickname.")),
        )
        self.assertEqual(
            declared["path"],
            presenter.OptionSchema(
                presenter.OptionKind.TEXT, i18n._("Game path"), i18n._("Path to the game.")),
        )
        self.assertIs(declared["master_uri"].kind, presenter.OptionKind.LINES)
        self.assertIs(declared["steam_launch"].kind, presenter.OptionKind.FLAG)
        self.assertEqual(
            set(declared), {"path", "workdir", "master_uri", "steam_launch", "steam_path", "nickname"})

        game_lists = resources.load_toml("settings", "internal", "game_lists.toml")
        packaged = resources.load_toml("settings", "defaults", "games.toml")
        nickname_games = {game for game, info in game_lists.items() if "nickname" in info["settings"]}

        self.assertTrue(nickname_games)
        for game in nickname_games:
            self.assertIn("nickname", packaged[game])

    def test_preferences_present_typed_fields_and_save_writes_them_back(self):
        instance, view = make_presenter()
        table = instance.core.game_table
        table.set_game_setting("browser", "path", "client")
        table.set_game_setting("browser", "master_uri", ["a://1", "b://2"])
        table.set_game_setting("browser", "steam_launch", "True")

        instance.preferences_requested()

        title, fields, on_save = view.preferences[0]
        declared = presenter.game_options()
        self.assertEqual(title, "Browser Game preferences")
        # Every field carries the declared schema of its option, not a copy of parts of it.
        self.assertEqual(
            [(field.option, field.schema, field.value) for field in fields],
            [
                ("path", declared["path"], "client"),
                ("master_uri", declared["master_uri"], "a://1\nb://2"),
                ("steam_launch", declared["steam_launch"], True),
            ],
        )

        on_save({"path": "/opt/client", "master_uri": "c://3\nd://4", "steam_launch": False})

        self.assertEqual(table.get_game_settings("browser"), {
            "path": "/opt/client",
            "master_uri": ["c://3", "d://4"],
            "steam_launch": False,
        })


if __name__ == "__main__":
    unittest.main()
