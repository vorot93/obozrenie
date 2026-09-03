#!/usr/bin/python
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


import base64
import contextlib
import itertools
import os
import subprocess
import sys
import tempfile
import time
import unittest
import xmltodict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from obozrenie import adapters, helpers, launch
from obozrenie.diagnostics import SafeDiagnostic, SafeDiagnosticError
from obozrenie.models import Endpoint, Player, Server


class HelpersTests(unittest.TestCase):
    """Tests for helpers"""

    def test_log_safe_diagnostic_reports_fixed_text_never_the_exception(self):
        class HostileError(Exception):
            def __str__(self):
                raise AssertionError("str called")

            def __repr__(self):
                raise AssertionError("repr called")

        declared = SafeDiagnostic("Declared safe diagnostic.")
        messages = []
        with mock.patch.object(helpers, "debug_msg", messages.append):
            helpers.log_safe_diagnostic("Prefix", HostileError("secret"))
            helpers.log_safe_diagnostic("Prefix", SafeDiagnosticError(declared))

        self.assertEqual(
            messages,
            [
                [
                    "Prefix",
                    SafeDiagnostic(
                        "Unexpected HostileError; exception details omitted."
                    ),
                ],
                ["Prefix", declared],
            ],
        )
        self.assertIs(messages[1][1], declared)

    def test_log_safe_diagnostic_swallows_a_report_that_cannot_be_written(self):
        # The failure being reported is often the log stream itself, so a
        # report that fails is dropped rather than raised into a caller that
        # is re-raising a primary failure or quitting the application.
        with mock.patch.object(
            helpers,
            "debug_msg",
            side_effect=OSError(9, "Bad file descriptor"),
        ) as report:
            helpers.log_safe_diagnostic("Prefix", RuntimeError("primary"))

        report.assert_called_once()


class LauncherTests(unittest.TestCase):
    """Tests for the launch specification builder"""

    def test_steam_launch_pattern(self):
        spec = launch.build_launch_spec(
            "csgo",
            "steam",
            {"path": "csgo_linux", "steam_path": "steam", "workdir": ""},
            Endpoint("localhost", 27960),
            "abracadabra",
            "12345",
            activation_directory="/start",
        )

        self.assertEqual(
            spec.argv,
            (
                "steam",
                "-applaunch",
                "12345",
                "+connect",
                "localhost:27960",
                "+password",
                "abracadabra",
            ),
        )
        self.assertEqual(spec.cwd, "/start")

    def test_quake_launch_pattern(self):
        spec = launch.build_launch_spec(
            "q3a",
            "quake",
            {"path": "quake", "workdir": ""},
            Endpoint("localhost", 27960),
            "abracadabra",
            activation_directory="/start",
        )

        self.assertEqual(
            spec.argv,
            (
                "quake",
                "+connect",
                "localhost:27960",
                "+password",
                "abracadabra",
            ),
        )
        self.assertEqual(spec.cwd, "/start")


class QStatTests(unittest.TestCase):
    """Tests for QStat backend"""

    def test_parse_master_entry(self):
        xml_string = '<server type="Q2M" address="localhost:12345" status="UP" servers="44"></server>'
        result = adapters.qstat.adapt_qstat_entry(
            xmltodict.parse(xml_string)["server"], "q2", "Q2M", "Q2S"
        )

        self.assertIsNone(result)

    def test_parse_server_entry(self):
        xml_string = '<server type="Q2S" address="localhost:12345" status="UP"><hostname>localhost</hostname><name>Gandalfehtgreen&apos;s Casino (R.I.P.)</name><gametype>action</gametype><map>locknload</map><numplayers>0</numplayers><maxplayers>15</maxplayers><numspectators>0</numspectators><maxspectators>0</maxspectators><ping>1126</ping><retries>1</retries><rules><rule name="*Q2Admin">2.0~3a63381</rule><rule name="actionversion">TNG 2.81~d504f0d</rule><rule name="allitem">0</rule><rule name="allweapon">0</rule><rule name="capturelimit">0</rule><rule name="cheats">0</rule><rule name="ctf">0</rule><rule name="deathmatch">1</rule><rule name="dmflags">8</rule><rule name="fraglimit">0</rule><rule name="game">action</rule><rule name="gamedate">Sep 15 2013</rule><rule name="gamedir">action</rule><rule name="gamename">action</rule><rule name="items">1</rule><rule name="matchmode">0</rule><rule name="needpass">0</rule><rule name="port">12345</rule><rule name="protocol">34</rule><rule name="q2a_mvd">1.6hau</rule><rule name="roundlimit">15</rule><rule name="roundtimelimit">5</rule><rule name="t1">0</rule><rule name="t2">0</rule><rule name="t3">0</rule><rule name="teamplay">1</rule><rule name="tgren">1</rule><rule name="timelimit">60</rule><rule name="use_3teams">0</rule><rule name="use_classic">0</rule><rule name="use_tourney">0</rule><rule name="uptime">297+0:09.46</rule></rules><players><player><name>PlayerA</name><score>0</score><ping>3</ping></player><player><name>PlayerB</name><score>0</score><ping>4</ping></player><player><name>PlayerC</name><score>0</score><ping>5</ping></player></players></server>'
        server = adapters.qstat.adapt_qstat_entry(
            xmltodict.parse(xml_string)["server"], "q2", "Q2M", "Q2S"
        )

        self.assertEqual(server.endpoint, Endpoint("localhost", 12345))
        self.assertEqual(server.game_id, "q2")
        self.assertEqual(server.name, "Gandalfehtgreen's Casino (R.I.P.)")
        self.assertEqual(server.game_mod, "action")
        self.assertEqual(server.game_type, "action")
        self.assertEqual(server.terrain, "locknload")
        self.assertEqual((server.player_count, server.player_limit), (0, 15))
        self.assertEqual(server.ping, 1126)
        self.assertEqual(
            server.players,
            (
                Player("PlayerA", score=0, ping=3),
                Player("PlayerB", score=0, ping=4),
                Player("PlayerC", score=0, ping=5),
            ),
        )
        self.assertEqual(server.rules["game"], "action")
        self.assertEqual(server.rules["gamename"], "action")
        self.assertEqual(server.rules["needpass"], "0")

    def test_build_host_list_preserves_order_and_dedupes(self):
        """Masters must keep their configured order and be de-duplicated.

        qstat returns zero servers when the *last* master in its input times out,
        so the configured order (reachable masters listed last) must survive.
        Regression: list(set(...)) shuffled the order per process (hash seed),
        intermittently moving a dead master last -> Quake III showed no servers.
        """
        master_list = ["master://dead1.example.net:27950",
                       "master://dead2.example.net:27950",
                       "master://live.example.net:27950",
                       "master://dead1.example.net:27950"]  # duplicate dropped, order kept
        result = adapters.qstat.build_host_list(master_list)
        self.assertEqual(result, ["dead1.example.net:27950",
                                  "dead2.example.net:27950",
                                  "live.example.net:27950"])


class RigsofrodsTests(unittest.TestCase):
    def test_parse_server_entry(self):
        spec_entry = {"has-password": True,
                      "current-users": 1,
                      "max-clients": 16,
                      "verified": 1,
                      "is-official": 0,
                      "ip": "1.2.3.4",
                      "port": 12000,
                      "version": "RoRnet_2.45",
                      "terrain-name": "Terrain A",
                      "name": "My Little Server",
                      "json-userlist": [{"username": "PlayerA", "is_bot": 0},
                                        {"username": "RoRBot", "is_bot": 8}]}
        server = adapters.rigsofrods.parse_server_entry("rigsofrods", spec_entry)

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("1.2.3.4", 12000),
                game_id="rigsofrods",
                name="My Little Server",
                password=True,
                player_count=1,
                player_limit=16,
                game_type="rigsofrods",
                terrain="Terrain A",
                players=(Player("PlayerA"), Player("RoRBot")),
                rules={"verified": 1, "is_official": 0},
            ),
        )

    def test_adapt_server_list(self):
        spec_entries = [
            {"has-password": False, "current-users": 3, "max-clients": 16,
             "ip": "1.2.3.4", "port": 12000, "terrain-name": "Terrain A",
             "name": "Server 1", "json-userlist": []},
            {"has-password": True, "current-users": 1, "max-clients": 9,
             "ip": "5.6.7.8", "port": 12001, "terrain-name": "Terrain B",
             "name": "Server 2", "json-userlist": [{"username": "PlayerA"}]}
        ]
        spec_server_list = [
            Server(
                endpoint=Endpoint("1.2.3.4", 12000),
                game_id="rigsofrods",
                name="Server 1",
                player_count=3,
                player_limit=16,
                game_type="rigsofrods",
                terrain="Terrain A",
                rules={"verified": 0, "is_official": 0},
            ),
            Server(
                endpoint=Endpoint("5.6.7.8", 12001),
                game_id="rigsofrods",
                name="Server 2",
                password=True,
                player_count=1,
                player_limit=9,
                game_type="rigsofrods",
                terrain="Terrain B",
                players=(Player("PlayerA"),),
                rules={"verified": 0, "is_official": 0},
            ),
        ]

        self.assertEqual(
            adapters.rigsofrods.adapt_server_list(
                "rigsofrods", spec_entries
            ),
            spec_server_list,
        )

    def test_stat_master_returns_list_not_tuple(self):
        """Adapter contract: stat_master returns a flat list of records.

        Regression: the tuple return caused TypeError in the Core query
        worker's geolocation loop. Empty master_list => no network access.
        """
        from obozrenie.adapters import rigsofrods
        result = rigsofrods.stat_master('rigsofrods', {}, [])
        self.assertIsInstance(result, list)

    def test_master_request_url_appends_json_true(self):
        from obozrenie.adapters import rigsofrods
        self.assertEqual(
            rigsofrods.master_request_url(
                "https://api.rigsofrods.org/server-list"),
            "https://api.rigsofrods.org/server-list?json=true")

    def test_master_request_url_keeps_existing_json_true(self):
        from obozrenie.adapters import rigsofrods
        self.assertEqual(
            rigsofrods.master_request_url(
                "https://api.rigsofrods.org/server-list?json=true"),
            "https://api.rigsofrods.org/server-list?json=true")

class GameTableTests(unittest.TestCase):
    def _table(self, config):
        from obozrenie.game_table import GameTable
        return GameTable(config)

    def test_invalid_set_game_setting_names_the_param(self):
        table = self._table({
            "q3a": {"name": "Quake III", "adapter": "qstat",
                    "launch_pattern": "quake", "settings": ["path"]},
        })
        with self.assertRaises(ValueError) as ctx:
            table.set_game_setting("", "path", "/bin/quake")
        self.assertIn("game id", str(ctx.exception))

    def test_steam_app_id_does_not_leak_to_later_games(self):
        table = self._table({
            "tf": {"name": "TF2", "adapter": "qstat", "launch_pattern": "hl2",
                   "steam_app_id": "440", "settings": ["path"]},
            "gesource": {"name": "GoldenEye", "adapter": "qstat",
                         "launch_pattern": "hl2", "settings": ["path"]},
        })
        self.assertEqual(table.get_game_info("tf")["steam_app_id"], "440")
        self.assertNotIn("steam_app_id", table.get_game_info("gesource"))


class DoLaunchTests(unittest.TestCase):
    def test_do_launch_uses_the_specified_working_directory(self):
        with mock.patch.object(launch.subprocess, "Popen") as popen, \
             mock.patch.object(launch.helpers, "debug_msg"):
            launch.do_launch(launch.LaunchSpec(("true",), "/start"))

        self.assertEqual(popen.call_args.kwargs["cwd"], "/start")


class TomlTableTests(unittest.TestCase):
    """Tests for TOML load/save round-trip"""

    def test_save_then_load_round_trip(self):
        data = {"section": {"key": "value", "number": 42, "flag": True}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "table.toml")
            helpers.save_table(path, data)
            loaded, warning = helpers.load_user_table(path)
            self.assertEqual(loaded, data)
            self.assertIsNone(warning)

    def test_load_missing_file_returns_an_empty_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "does-not-exist.toml")
            self.assertEqual(helpers.load_user_table(path), ({}, None))


class GeoIPModuleTests(unittest.TestCase):
    """Tests for the geoip discovery/download module."""

    def test_find_database_prefers_system_path(self):
        from obozrenie import geoip
        with tempfile.TemporaryDirectory() as d:
            sys_db = os.path.join(d, "system.mmdb")
            cache_db = os.path.join(d, "cache.mmdb")
            open(sys_db, "wb").close()
            open(cache_db, "wb").close()
            with mock.patch.object(geoip, "KNOWN_SYSTEM_PATHS", [sys_db]), \
                 mock.patch.object(geoip, "CACHE_DB_PATH", cache_db):
                self.assertEqual(geoip.find_database(), sys_db)

    def test_find_database_falls_back_to_cache(self):
        from obozrenie import geoip
        with tempfile.TemporaryDirectory() as d:
            cache_db = os.path.join(d, "cache.mmdb")
            open(cache_db, "wb").close()
            with mock.patch.object(geoip, "KNOWN_SYSTEM_PATHS",
                                   [os.path.join(d, "missing.mmdb")]), \
                 mock.patch.object(geoip, "CACHE_DB_PATH", cache_db):
                self.assertEqual(geoip.find_database(), cache_db)

    def test_find_database_returns_none_when_absent(self):
        from obozrenie import geoip
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(geoip, "KNOWN_SYSTEM_PATHS",
                                   [os.path.join(d, "a.mmdb")]), \
                 mock.patch.object(geoip, "CACHE_DB_PATH",
                                   os.path.join(d, "b.mmdb")):
                self.assertIsNone(geoip.find_database())


class IconLoadingTests(unittest.TestCase):
    """Tests for texture loading (gtk_icons)."""

    _SVG_WITH_LEADING_COMMENT = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- ' + 'x' * 1200 + ' -->\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">'
        '<rect width="640" height="480" fill="red"/></svg>'
    )
    _ONE_PIXEL_PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def _gtk_icons(self):
        from obozrenie import gtk_icons
        return gtk_icons

    @staticmethod
    def _size(texture):
        return (texture.get_width(), texture.get_height())

    def test_svg_with_leading_comment_loads_at_its_intrinsic_size(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "xx.svg").write_text(self._SVG_WITH_LEADING_COMMENT, encoding="utf-8")
            icon = gtk_icons.texture_for_entry("xx", ("svg",), root)
        self.assertEqual(self._size(icon), (640, 480))

    def test_missing_icon_raises_file_not_found(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                gtk_icons.texture_for_entry("zz", ("svg",), Path(d))

    def test_icon_dictionary_uses_explicit_image_missing_fallback(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image-missing.png").write_bytes(self._ONE_PIXEL_PNG)
            icons = gtk_icons.game_icon_textures(("not-shipped",), root)
        self.assertEqual(self._size(icons["not-shipped"]), (1, 1))

    def test_invalid_requested_icon_uses_valid_image_missing_fallback(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.png").write_bytes(b"not a PNG")
            (root / "image-missing.png").write_bytes(self._ONE_PIXEL_PNG)
            icons = gtk_icons.game_icon_textures(("broken",), root)
        self.assertEqual(self._size(icons["broken"]), (1, 1))

    def test_games_without_icons_share_one_fallback_texture(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image-missing.png").write_bytes(self._ONE_PIXEL_PNG)
            icons = gtk_icons.game_icon_textures(("first", "second"), root)
        self.assertIs(icons["first"], icons["second"])

    def test_missing_image_missing_fallback_raises_file_not_found(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                gtk_icons.game_icon_textures(("not-shipped",), Path(directory))

    def test_invalid_image_missing_fallback_raises_file_not_found(self):
        gtk_icons = self._gtk_icons()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image-missing.png").write_bytes(b"not a PNG")
            with self.assertRaises(FileNotFoundError):
                gtk_icons.game_icon_textures(("not-shipped",), root)


class _CleanupAbort(BaseException):
    pass


class _RecordingOwner:
    """Stand in for one owned service and record how it is closed."""

    def __init__(self, name, events, *, begin_errors=(), join_error=None):
        self.name = name
        self.events = events
        self.begin_errors = list(begin_errors)
        self.join_error = join_error
        self.deadlines = []

    def begin_close(self):
        self.events.append(("begin", self.name))
        if self.begin_errors:
            error = self.begin_errors.pop(0)
            if error is not None:
                raise error

    def join_until(self, deadline):
        self.events.append(("join", self.name))
        self.deadlines.append(deadline)
        if self.join_error is not None:
            raise self.join_error


class ApplicationServicesTests(unittest.TestCase):
    OWNER_NAMES = ("core", "geoip", "qstat", "dns", "http", "icmp")

    def make_services(self, **owner_options):
        from obozrenie import launch_gtk, network_calls

        self.events = []
        self.owners = {
            name: _RecordingOwner(
                name,
                self.events,
                **owner_options.get(name, {}),
            )
            for name in self.OWNER_NAMES
        }
        return launch_gtk.ApplicationServices(
            self.owners["core"],
            self.owners["geoip"],
            self.owners["qstat"],
            network_calls.NetworkCallPools(
                dns=self.owners["dns"],
                http=self.owners["http"],
                icmp=self.owners["icmp"],
            ),
        )

    @property
    def begins(self):
        return [("begin", name) for name in self.OWNER_NAMES]

    @property
    def joins(self):
        return [("join", name) for name in self.OWNER_NAMES]

    def assert_one_deadline(self, deadline):
        for name in self.OWNER_NAMES:
            with self.subTest(owner=name):
                self.assertEqual(len(self.owners[name].deadlines), 1)
                self.assertIs(self.owners[name].deadlines[0], deadline)

    def test_begin_close_reaches_every_owner_before_any_join(self):
        services = self.make_services()

        self.assertIs(services.core, self.owners["core"])
        self.assertIs(services.geoip_downloads, self.owners["geoip"])
        self.assertIs(services.qstat_processes, self.owners["qstat"])

        services.begin_close()
        self.assertEqual(self.events, self.begins)

        deadline = time.monotonic() + 0.5
        services.join_until(deadline)
        self.assertEqual(self.events, self.begins + self.joins)
        self.assert_one_deadline(deadline)

    def test_close_until_rejects_a_malformed_deadline_before_closing(self):
        services = self.make_services()

        with self.assertRaises(TypeError):
            services.close_until(1)
        with self.assertRaises(TypeError):
            services.join_until(1)
        self.assertEqual(self.events, [])

    def test_close_until_passes_one_unchanged_deadline_and_continues_after_failures(self):
        begin_error = _CleanupAbort("geoip close interrupted")
        join_error = RuntimeError("http join failed")
        services = self.make_services(
            geoip={"begin_errors": (begin_error,)},
            http={"join_error": join_error},
        )

        deadline = time.monotonic() + 0.5
        with self.assertRaises(_CleanupAbort) as raised:
            services.close_until(deadline)

        self.assertIs(raised.exception, begin_error)
        self.assertEqual(self.events, self.begins + self.joins)
        self.assert_one_deadline(deadline)

        services = self.make_services(dns={"join_error": join_error})
        deadline = time.monotonic() + 0.5
        with self.assertRaises(RuntimeError) as raised:
            services.close_until(deadline)

        self.assertIs(raised.exception, join_error)
        self.assertEqual(self.events, self.begins + self.joins)
        self.assert_one_deadline(deadline)

    def test_repeated_begin_close_retries_only_incomplete_owners(self):
        error = RuntimeError("qstat close failed")
        services = self.make_services(qstat={"begin_errors": (error,)})

        with self.assertRaises(RuntimeError) as raised:
            services.begin_close()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.events, self.begins)

        self.events.clear()
        services.begin_close()
        self.assertEqual(self.events, [("begin", "qstat")])

        self.events.clear()
        deadline = time.monotonic() + 0.5
        services.close_until(deadline)
        self.assertEqual(self.events, self.joins)
        self.assert_one_deadline(deadline)


class EntryPointTests(unittest.TestCase):
    _ATTEMPTED_PATHS = {
        "macOS setup": ["macOS setup"],
        "GTK import": ["macOS setup", "GTK import"],
        "game table": ["macOS setup", "game table"],
        "geolocation": ["macOS setup", "game table", "geolocation"],
        "Core": ["macOS setup", "game table", "geolocation", "Core"],
        "coordinator": [
            "macOS setup",
            "game table",
            "geolocation",
            "Core",
            "coordinator",
        ],
        "services": [
            "macOS setup",
            "game table",
            "geolocation",
            "Core",
            "coordinator",
            "services",
        ],
        "Settings": [
            "macOS setup",
            "game table",
            "geolocation",
            "Core",
            "coordinator",
            "services",
            "Settings",
        ],
        "App": [
            "macOS setup",
            "game table",
            "geolocation",
            "Core",
            "coordinator",
            "services",
            "Settings",
            "App",
        ],
        "app.run": [
            "macOS setup",
            "game table",
            "geolocation",
            "Core",
            "coordinator",
            "services",
            "Settings",
            "App",
            "app.run",
        ],
    }
    _STARTED_PATH = _ATTEMPTED_PATHS["app.run"]

    def _run_main(
        self,
        failure=None,
        error=None,
        cleanup_error=None,
        tracebacks_at_cleanup=None,
        ambient_error=None,
        real_cleanup=False,
        record=None,
        handler_failure=None,
    ):
        from obozrenie import launch_gtk, network_calls, qstat_processes

        events = []
        instances = {}
        printed = []
        raised = []
        cleanup_calls = []
        closed_deadlines = []

        if record is not None:
            record.events = events
            record.instances = instances
            record.cleanup_calls = cleanup_calls
            record.closed_deadlines = closed_deadlines

        class FakeProcessOwner:
            """One process-wide service, recording its two shutdown phases."""

            def __init__(instance, name):
                instance.name = name
                instances[name] = instance

            def begin_close(instance):
                events.append(f"{instance.name}.begin_close")

            def join_until(instance, deadline):
                events.append(f"{instance.name}.join_until")
                closed_deadlines.append(deadline)

        class FakeGameTable:
            @classmethod
            def from_shipped_list(cls):
                events.append("game table")
                if failure == "game table":
                    raise error
                instances["game table"] = cls()
                return instances["game table"]

        class FakeGeolocation:
            @classmethod
            def discover(cls):
                events.append("geolocation")
                if failure == "geolocation":
                    raise error
                instances["geolocation"] = cls()
                return instances["geolocation"]

        class FakeCore:
            def __init__(instance, game_table, geolocation):
                events.append("Core")
                if failure == "Core":
                    raise error
                self.assertIs(game_table, instances["game table"])
                self.assertIs(geolocation, instances["geolocation"])
                instances["core"] = instance

            def begin_close(instance):
                events.append("core.begin_close")

            def join_until(instance, deadline):
                events.append("core.join_until")
                closed_deadlines.append(deadline)

        class FakeCoordinator:
            def __init__(instance, geolocation):
                events.append("coordinator")
                if failure == "coordinator":
                    raise error
                self.assertIs(geolocation, instances["geolocation"])
                instances["coordinator"] = instance

            def begin_close(instance):
                events.append("coordinator.begin_close")

            def join_until(instance, deadline):
                events.append("coordinator.join_until")
                closed_deadlines.append(deadline)

        class FakeServices:
            def __init__(
                instance,
                core_instance,
                coordinator_instance,
                supervisor,
                pools,
            ):
                events.append("services")
                if failure == "services":
                    raise error
                self.assertIs(core_instance, instances["core"])
                self.assertIs(coordinator_instance, instances["coordinator"])
                self.assertIs(
                    supervisor,
                    qstat_processes.get_qstat_process_supervisor(),
                )
                self.assertIs(pools, network_calls.get_network_call_pools())
                instances["services"] = instance

            def close_until(instance, deadline):
                closed_deadlines.append(deadline)
                record_cleanup()

        class FakeSettings:
            def __init__(instance, game_table, profile_path):
                events.append("Settings")
                if failure == "Settings":
                    raise error
                self.assertIs(game_table, instances["game table"])
                self.assertEqual(
                    profile_path,
                    os.path.expanduser(launch_gtk.PROFILE_PATH),
                )
                instances["settings"] = instance

        class FakeApp:
            def __init__(
                instance,
                core_instance,
                settings_instance,
                services_instance,
            ):
                events.append("App")
                if failure == "App":
                    raise error
                self.assertIs(core_instance, instances["core"])
                self.assertIs(settings_instance, instances["settings"])
                self.assertIs(services_instance, instances["services"])
                instance.handler_failure = None

            def run(instance, argument):
                events.append("app.run")
                self.assertIsNone(argument)
                if failure == "app.run":
                    raise error
                # A signal handler cannot raise through PyGObject's dispatch,
                # so a failed startup or shutdown reaches the launcher only
                # as this recorded diagnostic.
                instance.handler_failure = handler_failure

        gtk_module = ModuleType("obozrenie.gtk")
        gtk_geoip_module = ModuleType("obozrenie.gtk_geoip")
        if failure == "GTK import":
            def make_fail_gtk_import(raising_names):
                def fail_gtk_import(name):
                    if name in raising_names:
                        events.append("GTK import")
                        raise error
                    raise AttributeError(name)
                return fail_gtk_import

            gtk_module.__getattr__ = make_fail_gtk_import({"App"})
            gtk_geoip_module.__getattr__ = make_fail_gtk_import(
                {"GeoIPDownloadCoordinator"})
        else:
            gtk_module.App = FakeApp
            gtk_geoip_module.GeoIPDownloadCoordinator = FakeCoordinator

        def ensure_macos_library_path():
            events.append("macOS setup")
            if failure == "macOS setup":
                raise error

        def record_cleanup():
            events.append("cleanup")
            if tracebacks_at_cleanup is not None:
                tracebacks_at_cleanup.append(error.__traceback__)
            if cleanup_error is not None:
                raise cleanup_error

        def close_application_services(services, started_owners):
            cleanup_calls.append((services, tuple(started_owners)))
            record_cleanup()

        def print_error(*values, **kwargs):
            self.assertEqual(kwargs, {})
            events.append("print")
            printed.append(values)

        def invoke_main():
            try:
                launch_gtk.main()
            except BaseException as caught:
                events.append("raised")
                raised.append(caught)

        with contextlib.ExitStack() as patches:
            # The QStat supervisor and the network pools exist before `main()`
            # runs, so the launch resolves fakes from their own registries
            # rather than closing this process's real services.
            pools = network_calls.NetworkCallPools(
                dns=FakeProcessOwner("dns"),
                http=FakeProcessOwner("http"),
                icmp=FakeProcessOwner("icmp"),
            )
            patches.enter_context(mock.patch.object(
                network_calls, "_NETWORK_CALL_POOLS", pools))
            previous_supervisor = (
                qstat_processes.install_qstat_process_supervisor(
                    FakeProcessOwner("qstat")
                )
            )
            patches.callback(
                qstat_processes.install_qstat_process_supervisor,
                previous_supervisor,
            )
            patches.enter_context(mock.patch.dict(sys.modules, {
                "obozrenie.gtk": gtk_module,
                "obozrenie.gtk_geoip": gtk_geoip_module,
            }))
            patches.enter_context(mock.patch.object(
                launch_gtk,
                "_ensure_macos_library_path",
                side_effect=ensure_macos_library_path,
            ))
            patches.enter_context(mock.patch.object(
                launch_gtk, "ApplicationServices", FakeServices))
            patches.enter_context(mock.patch.object(
                launch_gtk, "Core", FakeCore))
            patches.enter_context(mock.patch.object(
                launch_gtk, "GameTable", FakeGameTable))
            patches.enter_context(mock.patch.object(
                launch_gtk, "Geolocation", FakeGeolocation))
            patches.enter_context(mock.patch.object(
                launch_gtk, "Settings", FakeSettings))
            patches.enter_context(
                mock.patch("builtins.print", side_effect=print_error))
            if not real_cleanup:
                patches.enter_context(mock.patch.object(
                    launch_gtk,
                    "_close_application_services",
                    side_effect=close_application_services,
                ))
            if ambient_error is None:
                invoke_main()
            else:
                try:
                    raise ambient_error
                except BaseException as caught_ambient:
                    self.assertIs(caught_ambient, ambient_error)
                    invoke_main()

        return events, printed, raised

    def test_main_closes_application_services_once_after_success(self):
        events, printed, raised = self._run_main()

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup"])
        self.assertEqual(printed, [])
        self.assertEqual(raised, [])

    def test_main_closes_application_services_before_failed_startup_exits(self):
        for failure, attempted_path in self._ATTEMPTED_PATHS.items():
            with self.subTest(failure=failure):
                error = RuntimeError(f"{failure} failed")
                events, printed, raised = self._run_main(failure, error)

                self.assertEqual(
                    events,
                    [*attempted_path, "print", "cleanup", "raised"],
                )
                self.assertEqual(printed, [(error,)])
                self.assertEqual(len(raised), 1)
                self.assertIsInstance(raised[0], SystemExit)
                self.assertEqual(raised[0].code, 1)

    def test_main_exits_one_for_a_failure_a_signal_handler_could_not_raise(self):
        # PyGObject prints and swallows what a `startup` or `shutdown`
        # handler raises, so `run()` returns success for a settings file that
        # never loaded or never saved; the recorded diagnostic is what makes
        # the run report the failure it was.
        events, printed, raised = self._run_main(
            handler_failure=SafeDiagnostic("Settings profile is locked."),
        )

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup", "raised"])
        self.assertEqual(printed, [])
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], SystemExit)
        self.assertEqual(raised[0].code, 1)

    def test_main_keeps_a_handler_failure_over_a_later_cleanup_failure(self):
        cleanup_error = RuntimeError("cleanup failed")
        events, printed, raised = self._run_main(
            cleanup_error=cleanup_error,
            handler_failure=SafeDiagnostic("Settings profile is locked."),
        )

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup", "raised"])
        self.assertEqual(printed, [])
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], SystemExit)
        self.assertEqual(raised[0].code, 1)

    def test_main_closes_application_services_when_caller_raises_base_exception(self):
        class CallerAbort(BaseException):
            pass

        error = CallerAbort("caller interrupted")
        events, printed, raised = self._run_main("app.run", error)

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup", "raised"])
        self.assertEqual(printed, [])
        self.assertEqual(len(raised), 1)
        self.assertIs(raised[0], error)

    def test_main_passes_the_services_owner_and_started_owners_to_cleanup(self):
        record = SimpleNamespace()
        self._run_main(record=record)
        services, started_owners = record.cleanup_calls[0]
        self.assertIs(services, record.instances["services"])
        # The process-wide services exist before the launch does, so cleanup
        # owns them however far construction got.
        process_wide = ("qstat", "dns", "http", "icmp")
        self.assertEqual(
            started_owners,
            tuple(
                record.instances[name]
                for name in (*process_wide, "core", "coordinator")
            ),
        )

        for failure, constructed_owners in (
            ("macOS setup", ()),
            ("Core", ()),
            ("coordinator", ("core",)),
            ("services", ("core", "coordinator")),
        ):
            with self.subTest(failure=failure):
                record = SimpleNamespace()
                self._run_main(
                    failure,
                    RuntimeError(f"{failure} failed"),
                    record=record,
                )
                services, started_owners = record.cleanup_calls[0]
                self.assertIsNone(services)
                self.assertEqual(
                    started_owners,
                    tuple(
                        record.instances[name]
                        for name in (*process_wide, *constructed_owners)
                    ),
                )

    def test_main_closes_the_services_owner_against_the_shutdown_deadline(self):
        from obozrenie import launch_gtk

        record = SimpleNamespace()
        started = time.monotonic()
        events, printed, raised = self._run_main(
            real_cleanup=True,
            record=record,
        )

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup"])
        self.assertEqual(raised, [])
        self.assertEqual(len(record.closed_deadlines), 1)
        deadline = record.closed_deadlines[0]
        self.assertIs(type(deadline), float)
        self.assertGreaterEqual(
            deadline,
            started + launch_gtk._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS,
        )
        self.assertLessEqual(
            deadline,
            time.monotonic()
            + launch_gtk._APPLICATION_SHUTDOWN_TIMEOUT_SECONDS,
        )

    def test_main_closes_owners_built_before_a_failed_services_owner(self):
        record = SimpleNamespace()
        error = RuntimeError("services failed")
        events, printed, raised = self._run_main(
            "services",
            error,
            real_cleanup=True,
            record=record,
        )

        self.assertEqual(
            events,
            [
                *self._ATTEMPTED_PATHS["services"],
                "print",
                "qstat.begin_close",
                "dns.begin_close",
                "http.begin_close",
                "icmp.begin_close",
                "core.begin_close",
                "coordinator.begin_close",
                "qstat.join_until",
                "dns.join_until",
                "http.join_until",
                "icmp.join_until",
                "core.join_until",
                "coordinator.join_until",
                "raised",
            ],
        )
        self.assertEqual(printed, [(error,)])
        self.assertEqual(len(set(record.closed_deadlines)), 1)
        self.assertIsInstance(raised[0], SystemExit)

    def test_main_closes_process_wide_owners_when_core_construction_fails(self):
        record = SimpleNamespace()
        error = RuntimeError("Core failed")
        events, printed, raised = self._run_main(
            "Core",
            error,
            real_cleanup=True,
            record=record,
        )

        # Nothing this launch could build exists, but the supervisor and the
        # pools it would have used do, and both phases still reach them.
        self.assertEqual(
            events,
            [
                *self._ATTEMPTED_PATHS["Core"],
                "print",
                "qstat.begin_close",
                "dns.begin_close",
                "http.begin_close",
                "icmp.begin_close",
                "qstat.join_until",
                "dns.join_until",
                "http.join_until",
                "icmp.join_until",
                "raised",
            ],
        )
        self.assertEqual(printed, [(error,)])
        self.assertEqual(len(set(record.closed_deadlines)), 1)
        self.assertIsInstance(raised[0], SystemExit)

    def test_main_propagates_cleanup_base_exception_after_success(self):
        class CleanupAbort(BaseException):
            pass

        cleanup_error = CleanupAbort("cleanup interrupted")
        events, printed, raised = self._run_main(cleanup_error=cleanup_error)

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup", "raised"])
        self.assertEqual(printed, [])
        self.assertEqual(len(raised), 1)
        self.assertIs(raised[0], cleanup_error)

    def test_main_preserves_selected_exit_when_cleanup_raises(self):
        class CleanupAbort(BaseException):
            pass

        app_error = RuntimeError("app.run failed")
        cleanup_error = CleanupAbort("cleanup interrupted")
        events, printed, raised = self._run_main(
            "app.run",
            app_error,
            cleanup_error,
        )

        self.assertEqual(
            events,
            [*self._STARTED_PATH, "print", "cleanup", "raised"],
        )
        self.assertEqual(printed, [(app_error,)])
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], SystemExit)
        self.assertEqual(raised[0].code, 1)

    def test_main_propagates_cleanup_failure_inside_ambient_except(self):
        class CleanupAbort(BaseException):
            pass

        ambient_error = RuntimeError("unrelated handled error")
        cleanup_error = CleanupAbort("cleanup interrupted")
        events, printed, raised = self._run_main(
            cleanup_error=cleanup_error,
            ambient_error=ambient_error,
        )

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup", "raised"])
        self.assertEqual(printed, [])
        self.assertEqual(len(raised), 1)
        self.assertIs(raised[0], cleanup_error)

    def test_main_succeeds_inside_ambient_except_when_cleanup_succeeds(self):
        ambient_error = RuntimeError("unrelated handled error")
        events, printed, raised = self._run_main(ambient_error=ambient_error)

        self.assertEqual(events, [*self._STARTED_PATH, "cleanup"])
        self.assertEqual(printed, [])
        self.assertEqual(raised, [])

    def test_main_preserves_selected_exit_inside_ambient_except(self):
        class CleanupAbort(BaseException):
            pass

        ambient_error = RuntimeError("unrelated handled error")
        app_error = RuntimeError("app.run failed")
        cleanup_error = CleanupAbort("cleanup interrupted")
        events, printed, raised = self._run_main(
            failure="app.run",
            error=app_error,
            cleanup_error=cleanup_error,
            ambient_error=ambient_error,
        )

        self.assertEqual(
            events,
            [*self._STARTED_PATH, "print", "cleanup", "raised"],
        )
        self.assertEqual(printed, [(app_error,)])
        self.assertEqual(len(raised), 1)
        self.assertIsInstance(raised[0], SystemExit)
        self.assertEqual(raised[0].code, 1)

    def test_main_preserves_caller_base_exception_and_original_traceback(self):
        from obozrenie import launch_gtk

        class CallerAbort(BaseException):
            pass

        class CleanupAbort(BaseException):
            pass

        class AmbientError(Exception):
            pass

        for failure, attempted_path in self._ATTEMPTED_PATHS.items():
            for cleanup_fails, ambient_is_active in itertools.product(
                (False, True),
                repeat=2,
            ):
                with self.subTest(
                    failure=failure,
                    cleanup_fails=cleanup_fails,
                    ambient_is_active=ambient_is_active,
                ):
                    caller_error = CallerAbort(f"{failure} interrupted")
                    cleanup_error = (
                        CleanupAbort("cleanup interrupted")
                        if cleanup_fails
                        else None
                    )
                    ambient_error = (
                        AmbientError("unrelated handled error")
                        if ambient_is_active
                        else None
                    )
                    tracebacks_at_cleanup = []
                    events, printed, raised = self._run_main(
                        failure=failure,
                        error=caller_error,
                        cleanup_error=cleanup_error,
                        tracebacks_at_cleanup=tracebacks_at_cleanup,
                        ambient_error=ambient_error,
                    )

                    self.assertEqual(
                        events,
                        [*attempted_path, "cleanup", "raised"],
                    )
                    self.assertEqual(printed, [])
                    self.assertEqual(len(raised), 1)
                    self.assertIs(raised[0], caller_error)
                    self.assertEqual(len(tracebacks_at_cleanup), 1)

                    original_traceback = tracebacks_at_cleanup[0]
                    main_tracebacks = []
                    current_traceback = raised[0].__traceback__
                    while current_traceback is not None:
                        if (
                            current_traceback.tb_frame.f_code
                            is launch_gtk.main.__code__
                        ):
                            main_tracebacks.append(current_traceback)
                        current_traceback = current_traceback.tb_next
                    self.assertEqual(len(main_tracebacks), 1)
                    self.assertIs(main_tracebacks[0], original_traceback)

    def test_import_is_side_effect_free_and_exposes_main(self):
        # Importing the launcher must not start the GUI or import GTK; it must
        # expose a callable main(). Run in a subprocess (with a timeout) so a
        # regression that launches on import fails fast instead of hanging.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import obozrenie.launch_gtk as m; "
                "assert callable(m.main), 'main() entry point missing'",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_macos_library_dir_none_off_darwin(self):
        # Run in a subprocess so that, before the refactor, importing the
        # launch-on-import module cannot hang this test runner (the timeout
        # contains it). After the refactor, import is side-effect-free and the
        # off-darwin branch returns None.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.platform = 'linux'; "
                "import obozrenie.launch_gtk as m; "
                "assert m._macos_library_dir() is None",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ensure_reexecs_when_libdir_missing(self):
        # When a Homebrew lib dir is found and not yet on the loader path,
        # _ensure_macos_library_path adds it and re-execs (os.execv) so dyld
        # picks it up. os.execv is mocked so nothing is actually replaced.
        from obozrenie import launch_gtk
        with mock.patch.object(launch_gtk, "_macos_library_dir",
                               return_value="/fake/brew/lib"), \
                mock.patch.dict(os.environ,
                                {"DYLD_FALLBACK_LIBRARY_PATH": ""}, clear=False), \
                mock.patch.object(launch_gtk.os, "execv") as execv:
            launch_gtk._ensure_macos_library_path()
            self.assertIn(
                "/fake/brew/lib",
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"].split(os.pathsep))
            execv.assert_called_once()
            exe, argv = execv.call_args.args
            self.assertEqual(exe, sys.executable)
            self.assertEqual(argv[:3],
                             [sys.executable, "-m", "obozrenie.launch_gtk"])

    def test_ensure_noop_when_libdir_already_present(self):
        # When the lib dir is already on DYLD_FALLBACK_LIBRARY_PATH, the guard
        # prevents a second re-exec (no infinite loop after the first one).
        from obozrenie import launch_gtk
        with mock.patch.object(launch_gtk, "_macos_library_dir",
                               return_value="/fake/brew/lib"), \
                mock.patch.dict(os.environ,
                                {"DYLD_FALLBACK_LIBRARY_PATH": "/fake/brew/lib"},
                                clear=False), \
                mock.patch.object(launch_gtk.os, "execv") as execv:
            launch_gtk._ensure_macos_library_path()
            execv.assert_not_called()


class PackagingTests(unittest.TestCase):
    def test_game_lists_have_no_proxy_keys(self):
        from obozrenie import resources

        table = resources.load_toml(
            "settings", "internal", "game_lists.toml"
        )
        for game, entry in table.items():
            self.assertNotIn("proxy", entry, game)

    def test_proxy_package_is_gone(self):
        import importlib
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("obozrenie.proxies")

if __name__ == "__main__":
    unittest.main()
