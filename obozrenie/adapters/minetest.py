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


import json
import os
import requests

from obozrenie.global_settings import *
from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers
import obozrenie.ping as ping

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "minetest.toml")
MINETEST_MSG = BACKENDCAT_MSG + i18n._("Minetest:")


def get_json(master_page_uri):
    try:
        master_page_object = requests.get(master_page_uri)
        master_page = master_page_object.text
    except Exception as e:
        raise ConnectionError(i18n._("Accessing URI %(uri)s failed with error %(msg)s.") % {'uri': master_page_uri, 'msg': e.args[0]})

    server_table = [];
    try:
        server_table = list(json.loads(master_page)["list"])
    except ValueError as e:
        raise ValueError(i18n._("Error parsing URI %(uri)s.: %(msg)s") % {'uri': master_page_uri, 'msg': e.args[0]})

    return server_table


def parse_json_entry(entry):
    entry_dict = {'rules': {}, 'players': []}
    try:
        entry_dict['password'] = bool(entry['password'])
    except KeyError:
        entry_dict['password'] = False
    entry_dict['host'] = ":".join((str(entry['ip']), str(entry['port'])))
    entry_dict['player_count'] = int(entry['clients'])
    try:
        entry_dict['player_limit'] = int(entry['proto_max'])
    except:
        entry_dict['player_limit'] = 9999
    try:
        entry_dict['name'] = str(entry['name'])
    except AttributeError:
        pass

    try:
        entry_dict['game_type'] = str(entry['gameid'])
    except AttributeError:
        pass

    entry_dict['terrain'] = ""

    try:
        for player in entry['clients_list']:
            entry_dict['players'].append({'name': str(player)})
    except KeyError:
        pass

    entry_dict['secure'] = False

    return entry_dict


def stat_master(game: str, game_info: dict, master_list: list):
  """Stats the master server"""
  try:
    backend_config_object = helpers.load_table(BACKEND_CONFIG)

    server_json_table = []
    server_table = []

    for master_uri in master_list:
        server_json_table += get_json(master_uri)

    for entry in server_json_table:
        entry_dict = parse_json_entry(entry)

        server_table.append(entry_dict)

    ping.add_rtt_info(server_table)

    for i in range(len(server_table)):
        entry = server_table[i]
        entry["game_id"] = game
        server_table[i] = entry

    return server_table
  except Exception as e:
    raise Exception(helpers.debug_msg_str([BACKENDCAT_MSG + MINETEST_MSG, e.args[0]]))
