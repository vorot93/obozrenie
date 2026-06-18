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


import json
import os
import tempfile
import threading
import types
import unittest
import xmltodict
from unittest import mock

from obozrenie import helpers, adapters, i18n, launch


class HelpersTests(unittest.TestCase):
    """Tests for helpers"""
    module = helpers
    @classmethod
    def unit_dict_to_list(cls):
        """Does helper convert dict table into a list table based on specified format correctly?"""
        spec_dict_table = [{'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'d': 'D2', 'e': 'E2', 'a': 'A2'}]
        spec_key_list = ('a',
                         'e',
                         'd')

        func = cls.module.dict_to_list
        spec_args = {'dict_table': spec_dict_table, 'key_list': spec_key_list}
        spec_result = [['A1', 'E1', 'D1'], ['A2', 'E2', 'D2']]

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_dict_to_list(self):
        unit = self.unit_dict_to_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_flatten_dict_table(cls):
        """Does helper flatten dict based on specified format correctly?"""
        spec_dict_table = {'lead1': {'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, 'lead2': {'a': 'A2', 'b': 'B2'}}
        spec_leading_key_spec = 'leading key'

        func = cls.module.flatten_dict_table
        spec_args = {'dict_table': spec_dict_table, 'leading_key_spec': spec_leading_key_spec}
        spec_result = [{'leading key': 'lead1', 'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'leading key': 'lead2', 'a': 'A2', 'b': 'B2'}]

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_flatten_dict_table(self):
        unit = self.unit_flatten_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_flatten_list(cls):
        """Does helper flatten list of iterable objects based on specified format correctly?"""
        spec_nested_list = ['a', ['b', ['c', 'd'], 'e'], 'f']

        func = cls.module.flatten_list
        spec_args = {'nested_list': spec_nested_list}
        spec_result = ['a', 'b', 'c', 'd', 'e', 'f']

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_flatten_list(self):
        unit = self.unit_flatten_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_sort_dict_table(cls):
        """Checks how helper function sorts dictionary tables"""
        spec_dict_table = [{'name': 'Olga', 'age': 32, 'gender': 'F'}, {'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}]
        spec_sort_key = 'name'

        func = cls.module.sort_dict_table
        spec_args = {'dict_table': spec_dict_table, 'sort_key': spec_sort_key}
        spec_result = [{'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}, {'name': 'Olga', 'age': 32, 'gender': 'F'}]

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_sort_dict_table(self):
        unit = self.unit_sort_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])


class LauncherTests(unittest.TestCase):
    module = launch
    """Tests for launcher"""
    @classmethod
    def unit_steam_launch_pattern(cls):
        """Check Steam launch pattern"""
        func = cls.module.steam_launch_pattern
        spec_args = {'game_settings': {'steam_path': 'steam'}, 'steam_app_id': '12345', 'host': 'localhost', 'port': '27960', 'password': 'abracadabra'}
        spec_result = ['steam', '-applaunch', '12345', '+connect', 'localhost:27960', '+password', 'abracadabra']

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_steam_launch_pattern(self):
        unit = self.unit_steam_launch_pattern()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_quake_launch_pattern(cls):
        """Check Quake launch pattern"""
        func = cls.module.quake_launch_pattern
        spec_args = {'path': 'quake', 'host': 'localhost', 'port': '27960', 'password': 'abracadabra'}
        spec_result = ['quake', '+connect', 'localhost:27960', '+password', 'abracadabra']

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_quake_launch_pattern(self):
        unit = self.unit_quake_launch_pattern()
        self.assertTrue(unit['expectation'] == unit['result'])


class MinetestTests(unittest.TestCase):
    """Tests for minetest"""
    module = adapters.minetest
    @classmethod
    def unit_minetest_parse(cls):
        """Check Minetest backend parsing"""
        json_string = '{"ping": 0.09202814102172852, "clients_list": ["PlayerA", "PlayerB", "PlayerC"], "version": "0.4.13", "creative": false, "proto_max": 26, "total_clients": 807, "proto_min": 13, "pvp": true, "damage": true, "mapgen": "v7", "privs": "interact, shout, home", "address": "game.minetest-france.fr", "update_time": 1447351523.4476626, "uptime": 21600, "mods": ["potions", "item_drop", "irc", "irc_commands", "hudbars", "mana", "essentials_mmo", "efori", "denaid", "areas_gui", "areas", "worldedit", "worldedit_infinity", "worldedit_commands", "worldedit_shortcommands", "sethome", "screwdriver", "fire", "dye", "default", "mobs", "essentials", "encyclopedia", "economy", "xpanes", "wool", "farming", "stairs", "beds", "vessels", "tnt", "give_initial_stuff", "flowers", "doors", "creative", "unified_inventory", "u_skins", "worldedit_gui", "3d_armor", "hbarmor", "wieldview", "shields", "bucket", "hbhunger", "bones", "boats"], "rollback": false, "password": false, "game_time": 13680891, "lag": 0.1009900271892548, "description": "Serveur Survie / PvP", "can_see_far_names": false, "port": 30001, "start": 1447333508.1709588, "pop_v": 13.229508196721312, "clients_max": 50, "updates": 61, "name": "My Little Server", "url": "http://my-minetest-server.com", "clients_top": 23, "gameid": "minetest", "clients": 3, "dedicated": true, "ip": "12.34.56.789"}'
        json_result = {'password': False, 'host': '12.34.56.789:30001', 'player_count': 3, 'player_limit': 26, 'name': 'My Little Server', 'game_type': 'minetest', 'terrain': '', 'secure': False, 'rules': {}, 'players': [{'name': 'PlayerA'}, {'name': 'PlayerB'}, {'name': 'PlayerC'}]}

        func = cls.module.parse_json_entry
        spec_args = {"entry": json.loads(json_string)}
        spec_result = json_result

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_minetest_parse(self):
        unit = self.unit_minetest_parse()
        self.assertTrue(unit['expectation'] == unit['result'])


class QStatTests(unittest.TestCase):
    """Tests for QStat backend"""
    module = adapters.qstat
    @classmethod
    def unit_parse_master_entry(cls):
        """Check QStat output parsing - masters"""
        xml_string = '<server type="Q2M" address="localhost:12345" status="UP" servers="44"></server>'

        func = cls.module.adapt_qstat_entry
        spec_args = {"qstat_entry": xmltodict.parse(xml_string)['server'], "game": "q2", "master_type": "Q2M", "server_type": "Q2S"}
        spec_result = {'server_dict': None, 'debug_msg': i18n._('Queried Master. Address: localhost:12345, status: UP, server count: 44.')}

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_parse_master_entry(self):
        unit = self.unit_parse_master_entry()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_parse_server_entry(cls):
        """Check QStat output parsing - masters"""
        xml_string = '<server type="Q2S" address="localhost:12345" status="UP"><hostname>localhost</hostname><name>Gandalfehtgreen&apos;s Casino (R.I.P.)</name><gametype>action</gametype><map>locknload</map><numplayers>0</numplayers><maxplayers>15</maxplayers><numspectators>0</numspectators><maxspectators>0</maxspectators><ping>1126</ping><retries>1</retries><rules><rule name="*Q2Admin">2.0~3a63381</rule><rule name="actionversion">TNG 2.81~d504f0d</rule><rule name="allitem">0</rule><rule name="allweapon">0</rule><rule name="capturelimit">0</rule><rule name="cheats">0</rule><rule name="ctf">0</rule><rule name="deathmatch">1</rule><rule name="dmflags">8</rule><rule name="fraglimit">0</rule><rule name="game">action</rule><rule name="gamedate">Sep 15 2013</rule><rule name="gamedir">action</rule><rule name="gamename">action</rule><rule name="items">1</rule><rule name="matchmode">0</rule><rule name="needpass">0</rule><rule name="port">12345</rule><rule name="protocol">34</rule><rule name="q2a_mvd">1.6hau</rule><rule name="roundlimit">15</rule><rule name="roundtimelimit">5</rule><rule name="t1">0</rule><rule name="t2">0</rule><rule name="t3">0</rule><rule name="teamplay">1</rule><rule name="tgren">1</rule><rule name="timelimit">60</rule><rule name="use_3teams">0</rule><rule name="use_classic">0</rule><rule name="use_tourney">0</rule><rule name="uptime">297+0:09.46</rule></rules><players><player><name>PlayerA</name><score>0</score><ping>3</ping></player><player><name>PlayerB</name><score>0</score><ping>4</ping></player><player><name>PlayerC</name><score>0</score><ping>5</ping></player></players></server>'

        spec_server_dict = {'game_name': 'action', 'password': False, 'game_mod': '', 'player_count': 0, 'secure': False, 'ping': 1126, 'rules': {'gamedir': 'action', 'needpass': '0', 'uptime': '297+0:09.46', 'use_classic': '0', 'timelimit': '60', 'capturelimit': '0', 'game': 'action', 'tgren': '1', 'actionversion': 'TNG 2.81~d504f0d', 'allitem': '0', 'use_tourney': '0', 't1': '0', 'protocol': '34', 'port': '12345', 'cheats': '0', 'ctf': '0', 'deathmatch': '1', 'q2a_mvd': '1.6hau', '*Q2Admin': '2.0~3a63381', 'teamplay': '1', 'use_3teams': '0', 'gamedate': 'Sep 15 2013', 't2': '0', 'fraglimit': '0', 'matchmode': '0', 'dmflags': '8', 'allweapon': '0', 'roundlimit': '15', 'gamename': 'action', 'roundtimelimit': '5', 'items': '1', 't3': '0'}, 'players': [{'name': 'PlayerA', 'ping': 3, 'score': 0}, {'name': 'PlayerB', 'ping': 4, 'score': 0}, {'name': 'PlayerC', 'ping': 5, 'score': 0}], 'name': "Gandalfehtgreen's Casino (R.I.P.)", 'player_limit': 15, 'host': 'localhost', 'game_type': 'action', 'game_id': 'q2', 'terrain': 'locknload'}
        spec_debug_msg = None

        func = adapters.qstat.adapt_qstat_entry
        spec_args = {"qstat_entry": xmltodict.parse(xml_string)['server'], "game": "q2", "master_type": "Q2M", "server_type": "Q2S"}
        spec_result = {'server_dict': spec_server_dict, 'debug_msg': spec_debug_msg}

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_parse_server_entry(self):
        unit = self.unit_parse_server_entry()
        self.assertTrue(unit['expectation'] == unit['result'])

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
    module = adapters.rigsofrods

    @classmethod
    def unit_parse_server_entry(cls):
        """Check Rigs of Rods JSON server entry parsing (incl. player list)."""
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
        spec_server_dict = {'player_count': 1,
                            'player_limit': 16,
                            'password': True,
                            'name': 'My Little Server',
                            'host': '1.2.3.4:12000',
                            'terrain': 'Terrain A',
                            'players': [{'name': 'PlayerA'}, {'name': 'RoRBot'}]}

        func = cls.module.parse_server_entry
        spec_args = {'entry': spec_entry}
        spec_result = spec_server_dict

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_parse_server_entry(self):
        unit = self.unit_parse_server_entry()
        self.assertTrue(unit['expectation'] == unit['result'])

    @classmethod
    def unit_adapt_server_list(cls):
        """Check Rigs of Rods JSON list parsing and game tagging."""
        spec_json_string = json.dumps([
            {"has-password": False, "current-users": 3, "max-clients": 16,
             "ip": "1.2.3.4", "port": 12000, "terrain-name": "Terrain A",
             "name": "Server 1", "json-userlist": []},
            {"has-password": True, "current-users": 1, "max-clients": 9,
             "ip": "5.6.7.8", "port": 12001, "terrain-name": "Terrain B",
             "name": "Server 2", "json-userlist": [{"username": "PlayerA"}]}])
        spec_server_list = [
            {'player_count': 3, 'player_limit': 16, 'password': False,
             'name': 'Server 1', 'host': '1.2.3.4:12000', 'terrain': 'Terrain A',
             'players': [], 'game_id': 'rigsofrods', 'game_type': 'rigsofrods',
             'secure': False},
            {'player_count': 1, 'player_limit': 9, 'password': True,
             'name': 'Server 2', 'host': '5.6.7.8:12001', 'terrain': 'Terrain B',
             'players': [{'name': 'PlayerA'}], 'game_id': 'rigsofrods',
             'game_type': 'rigsofrods', 'secure': False}]
        spec_game = 'rigsofrods'

        func = cls.module.adapt_server_list
        spec_args = {'game': spec_game, 'json_string': spec_json_string}
        spec_result = spec_server_list

        result = func(**spec_args)
        return {'expectation': spec_result, 'result': result}

    def test_adapt_server_list(self):
        unit = self.unit_adapt_server_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    def test_stat_master_returns_list_not_tuple(self):
        """Adapter contract: stat_master returns a flat list of server dicts, not a (list, None) tuple.

        Regression: the tuple return caused TypeError in Core.stat_master_target's
        geolocation loop. Empty master_list => no network access.
        """
        from obozrenie.adapters import rigsofrods
        result = rigsofrods.stat_master('rigsofrods', {}, [])
        self.assertIsInstance(result, list)


class TomlTableTests(unittest.TestCase):
    """Tests for TOML load/save round-trip"""

    def test_save_then_load_round_trip(self):
        data = {"section": {"key": "value", "number": 42, "flag": True}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "table.toml")
            helpers.save_table(path, data)
            self.assertEqual(helpers.load_table(path), data)

    def test_load_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "does-not-exist.toml")
            self.assertIsNone(helpers.load_table(path))


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

    def test_download_cancel_cleans_up_and_returns_none(self):
        from obozrenie import geoip

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            headers = {"Content-Length": "100"}
            def iter_content(self, chunk_size): yield b"x" * 10

        with tempfile.TemporaryDirectory() as d:
            cache_db = os.path.join(d, "cache.mmdb")
            cancel = threading.Event()
            cancel.set()
            with mock.patch.object(geoip, "CACHE_DB_PATH", cache_db), \
                 mock.patch.object(geoip.requests, "get",
                                   return_value=_FakeResp()):
                result = geoip.download_database(cancel)
            self.assertIsNone(result)
            self.assertFalse(os.path.exists(cache_db + ".part"))
            self.assertFalse(os.path.exists(cache_db))


class CoreGeoIPTests(unittest.TestCase):
    """Tests for Core geolocation lookups."""

    def _make_core(self):
        from obozrenie import core
        c = core.Core()
        c.geolocation = None
        return c

    def test_lookup_country_disabled_returns_unknown(self):
        c = self._make_core()
        self.assertEqual(c._lookup_country("example.com"), "unknown")

    def test_lookup_country_success_returns_iso_code(self):
        import socket
        from obozrenie import core

        class _FakeReader:
            def country(self, ip):
                return types.SimpleNamespace(
                    country=types.SimpleNamespace(iso_code="US"))

        c = self._make_core()
        c.geolocation = _FakeReader()
        with mock.patch.object(socket, "gethostbyname", return_value="1.2.3.4"):
            self.assertEqual(c._lookup_country("example.com"), "US")

    def test_lookup_country_unresolvable_returns_empty(self):
        import socket
        c = self._make_core()
        c.geolocation = object()  # never called; resolution fails first
        with mock.patch.object(socket, "gethostbyname",
                               side_effect=socket.gaierror):
            self.assertEqual(c._lookup_country("nope.invalid"), "")


if __name__ == "__main__":
    unittest.main()
