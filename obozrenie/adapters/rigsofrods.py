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

import requests

from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.ping as ping

RIGSOFRODS_MSG = BACKENDCAT_MSG + i18n._("Rigs of Rods:")

HTTP_TIMEOUT = 10


def parse_server_entry(entry: dict) -> dict:
    """Map one Rigs of Rods JSON server object to a server dict."""
    return {'player_count': int(entry['current-users']),
            'player_limit': int(entry['max-clients']),
            'password': bool(entry['has-password']),
            'name': str(entry['name']),
            'host': '%s:%s' % (entry['ip'], entry['port']),
            'terrain': str(entry['terrain-name']),
            'players': [{'name': str(player['username'])}
                        for player in (entry.get('json-userlist') or [])]}


def adapt_server_list(game: str, json_string: str) -> list:
    """Parse a Rigs of Rods JSON server-list response into a list of server dicts."""
    server_list = []

    for json_entry in json.loads(json_string):
        try:
            entry = parse_server_entry(json_entry)
            entry['game_id'] = game
            entry['game_type'] = game
            entry['secure'] = False

            server_list.append(entry)
        except (KeyError, TypeError, ValueError):
            continue

    return server_list


def stat_master(game: str, game_info: dict, master_list: list) -> list:
    """Stats the master server"""
    server_table = []

    for master_uri in master_list:
        try:
            response = requests.get(master_uri, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException:
            print(i18n._(RIGSOFRODS_MSG), i18n._("Accessing URI %(uri)s failed with error code %(code)s.") % {
                  'uri': master_uri, 'code': "unknown"})
            continue

        try:
            server_table += adapt_server_list(game, response.text)
        except ValueError:
            print(i18n._(RIGSOFRODS_MSG), i18n._(
                "Error parsing URI %(uri)s.") % {'uri': master_uri})
            continue

    ping.add_rtt_info(server_table)

    return server_table
