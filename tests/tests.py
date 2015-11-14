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


import json
import unittest
import xmltodict

from obozrenie import helpers, backends, i18n


class HelpersTests(unittest.TestCase):
    """Tests for helpers"""
    @staticmethod
    def unit_dict_to_list():
        """Does helper convert dict table into a list table based on specified format correctly?"""
        spec_table = [{'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'d': 'D2', 'e': 'E2', 'a': 'A2'}]
        spec_format = ('a',
                   'e',
                   'd')
        spec_result = [['A1', 'E1', 'D1'], ['A2', 'E2', 'D2']]

        result = helpers.dict_to_list(spec_table, spec_format)

        return {'expectation': spec_result, 'result': result}

    def test_dict_to_list(self):
        unit = self.unit_dict_to_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    @staticmethod
    def unit_flatten_dict_table():
        """Does helper flatten dict based on specified format correctly?"""
        spec_table = {'lead1': {'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, 'lead2': {'a': 'A2', 'b': 'B2'}}
        leading_key_spec = 'leading key'
        spec_result = [{'leading key': 'lead1', 'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'leading key': 'lead2', 'a': 'A2', 'b': 'B2'}]

        result = helpers.flatten_dict_table(spec_table, leading_key_spec=leading_key_spec)

        return {'expectation': spec_result, 'result': result}

    def test_flatten_dict_table(self):
        unit = self.unit_flatten_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])

    @staticmethod
    def unit_flatten_list():
        """Does helper flatten list of iterable objects based on specified format correctly?"""
        spec_list = ['a', ['b', ['c', 'd'], 'e'], 'f']
        spec_result = ['a', 'b', 'c', 'd', 'e', 'f']

        result = helpers.flatten_list(spec_list)

        return {'expectation': spec_result, 'result': result}

    def test_flatten_list(self):
        unit = self.unit_flatten_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    @staticmethod
    def unit_sort_dict_table():
        """Checks how helper function sorts dictionary tables"""
        spec_table = [{'name': 'Olga', 'age': 32, 'gender': 'F'}, {'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}]
        spec_key = 'name'
        spec_result = [{'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}, {'name': 'Olga', 'age': 32, 'gender': 'F'}]

        result = helpers.sort_dict_table(spec_table, spec_key)

        return {'expectation': spec_result, 'result': result}

    def test_sort_dict_table(self):
        unit = self.unit_sort_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])


class MinetestTests(unittest.TestCase):
    """Tests for minetest"""
    @staticmethod
    def unit_minetest_parse():
        """Check Minetest backend parsing"""
        json_string = '{"ping": 0.09202814102172852, "clients_list": ["PlayerA", "PlayerB", "PlayerC"], "version": "0.4.13", "creative": false, "proto_max": 26, "total_clients": 807, "proto_min": 13, "pvp": true, "damage": true, "mapgen": "v7", "privs": "interact, shout, home", "address": "game.minetest-france.fr", "update_time": 1447351523.4476626, "uptime": 21600, "mods": ["potions", "item_drop", "irc", "irc_commands", "hudbars", "mana", "essentials_mmo", "efori", "denaid", "areas_gui", "areas", "worldedit", "worldedit_infinity", "worldedit_commands", "worldedit_shortcommands", "sethome", "screwdriver", "fire", "dye", "default", "mobs", "essentials", "encyclopedia", "economy", "xpanes", "wool", "farming", "stairs", "beds", "vessels", "tnt", "give_initial_stuff", "flowers", "doors", "creative", "unified_inventory", "u_skins", "worldedit_gui", "3d_armor", "hbarmor", "wieldview", "shields", "bucket", "hbhunger", "bones", "boats"], "rollback": false, "password": false, "game_time": 13680891, "lag": 0.1009900271892548, "description": "Serveur Survie / PvP", "can_see_far_names": false, "port": 30001, "start": 1447333508.1709588, "pop_v": 13.229508196721312, "clients_max": 50, "updates": 61, "name": "My Little Server", "url": "http://my-minetest-server.com", "clients_top": 23, "gameid": "minetest", "clients": 3, "dedicated": true, "ip": "12.34.56.789"}'
        json_result = {'password': False, 'host': '12.34.56.789:30001', 'player_count': 3, 'player_limit': 26, 'name': 'My Little Server', 'game_id': 'minetest', 'game_type': 'minetest', 'terrain': '', 'secure': False, 'rules': {}, 'players': [{'name': 'PlayerA'}, {'name': 'PlayerB'}, {'name': 'PlayerC'}]}

        spec_table = json_string
        spec_args = {"entry": json.loads(json_string)}
        spec_result = json_result

        result = backends.minetest.parse_json_entry(**spec_args)

        return {'expectation': spec_result, 'result': result}

    def test_minetest_parse(self):
        unit = self.unit_minetest_parse()
        self.assertTrue(unit['expectation'] == unit['result'])


class QStatTests(unittest.TestCase):
    """Tests for QStat backend"""
    @staticmethod
    def unit_qstat_parse_master_entry():
        """Check QStat output parsing"""
        xml_string = '<qstat><server type="Q2M" address="localhost:12345" status="UP" servers="44"></server></qstat>'

        spec_args = {"qstat_entry": xmltodict.parse(xml_string)['qstat']['server'], "game": "q2", "master_type": "Q2M", "server_type": "Q2S"}
        spec_result = (None, i18n._('Queried Master. Address: localhost:12345, status: UP, server count: 44.'))

        result = backends.qstat.parse_server_entry(**spec_args)

        return {'expectation': spec_result, 'result': result}

    def test_qstat_parse_master_entry(self):
        unit = self.unit_qstat_parse_master_entry()
        self.assertTrue(unit['expectation'] == unit['result'])


if __name__ == "__main__":
    unittest.main()
