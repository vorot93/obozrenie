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

import os

import json
import requests
import xmltodict

from obozrenie.global_settings import *
from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers
import obozrenie.ping as ping

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "rigsofrods.toml")
RIGSOFRODS_MSG = BACKENDCAT_MSG + i18n._("Rigs of Rods:")


def parse_server_entry(entry: list) -> dict:
    players = entry[0]['#text'].split('/')

    try:
        password = bool(entry[1]['#text'].strip())
    except KeyError:
        password = False

    server_dict = {'player_count': int(players[0]),
                   'player_limit': int(players[1]),
                   'password': password,
                   'name': str(entry[2]['a']['#text']),
                   'host': str(entry[2]['a']['@href']).replace('rorserver://', '').replace('user:pass@', '').strip('/'),
                   'terrain': str(entry[3]['#text'])}

    return server_dict


def adapt_server_list(game: str, html_string: str) -> list:
    html_dict = json.loads(json.dumps(xmltodict.parse(html_string)))

    server_list = []

    for html_entry in html_dict['table']['tr'][1:]:
        try:
            entry = parse_server_entry(html_entry['td'])
            entry['game_id'] = game
            entry['game_type'] = game
            entry['secure'] = False

            server_list.append(entry)
        except:
            continue

    return server_list


def stat_master(game: str, game_info: dict, master_list: str, proxy=None) -> list:
    """Stats the master server"""

    backend_config_object = helpers.load_table(BACKEND_CONFIG)

    protocol = backend_config_object["protocol"]["version"]

    server_table = []

    for master_uri in master_list:
        master_page_uri = master_uri.strip('/') + '/?version=' + protocol
        try:
            master_page_object = requests.get(master_page_uri)
            master_page = master_page_object.text
        except:
            print(i18n._(RIGSOFRODS_MSG), i18n._("Accessing URI %(uri)s failed with error code %(code)s.") % {'uri': master_page_uri, 'code': "unknown"})
            continue

        try:
            temp_table = adapt_server_list(game, master_page)
        except:
            print(i18n._(RIGSOFRODS_MSG), i18n._("Error parsing URI %(uri)s.") % {'uri': master_page_uri})
            continue

        server_table += temp_table

    ping.add_rtt_info(server_table)

    if proxy is not None:
        for entry in server_table:
            proxy.append(entry)

    return server_table
