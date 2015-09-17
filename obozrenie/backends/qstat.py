#!/usr/bin/python
# This source file is part of Obozrenie
# Copyright 2015 Artem Vorotnikov

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
import re

import ast
import json
import subprocess
import time
import xmltodict

from obozrenie.global_settings import *
from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers
import obozrenie.ping as ping

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "qstat.toml")
QSTAT_MSG = BACKENDCAT_MSG + i18n._("QStat:")


def split_server_list_into_masters(orig_list, split_key, split_value):
    master_server_uri = None
    split_dict = {}

    for server_entry in orig_list['qstat']['server']:
        if server_entry[split_key] == split_value:
            master_server_uri = server_entry['@address'].strip()
            split_dict[master_server_uri] = {'server': []}

        split_dict[master_server_uri]['server'].append(server_entry)

    return split_dict


def stat_master(game, game_table_slice, proxy=None):
    hosts_array = []
    server_table = []
    server_table_qstat_xml = []
    server_table_xmltodict = []
    server_table_dict = []

    master_server_uri = None
    stat_start_time = None
    stat_end_time = None

    game_name = game_table_slice["info"]["name"]
    backend_config_object = helpers.load_table(BACKEND_CONFIG)
    hosts_temp_array = list(game_table_slice["settings"]["master_uri"])

    for entry in hosts_temp_array:
        hosts_array.append(entry.strip())

    hosts_array = list(set(hosts_array))

    qstat_opts = []
    qstat_cmd = ["qstat", "-xml", "-utf8", "-R", "-P"]

    for entry in hosts_array:
        qstat_cmd.append("-" + backend_config_object['game'][game]['master_key'])
        qstat_cmd.append(entry)

    print(i18n._(QSTAT_MSG), i18n._("|%(game)s| Requesting server info.") % {'game': game_name})
    stat_start_time = time.time()
    try:
        qstat_output, _ = subprocess.Popen(qstat_cmd, stdout=subprocess.PIPE).communicate()
        server_table_qstat_xml = qstat_output.decode()
        server_table_dict = json.loads(json.dumps(xmltodict.parse(server_table_qstat_xml)))
    except Exception as e:
        print(QSTAT_MSG, e)
        proxy.append(Exception)
        return Exception
    stat_end_time = time.time()

    stat_total_time = stat_end_time - stat_start_time
    print(i18n._(QSTAT_MSG), i18n._("|%(game)s| Received server info. Elapsed time: %(stat_time)s s.") % {'game': game_name, 'stat_time': round(stat_total_time, 2)})
    color_code_pattern = '[\\^](.)'
    for qstat_entry in server_table_dict['qstat']['server']:  # For every server...
        try:
            if server_table_dict['qstat']['server']['@type'] == backend_config_object['game'][game]['master_type']:
                print(QSTAT_MSG, i18n._("|%(game)s| No valid masters specified. Please check your master server settings.") % {'game': game_name})
                break

        except TypeError:
            try:
                if qstat_entry['@type'] == backend_config_object['game'][game]['master_type']:
                    master_server_uri = qstat_entry['@address']
                    master_server_status = qstat_entry['@status']
                    if master_server_status == 'UP':
                        master_server_entry_count = qstat_entry['@servers']
                        print(QSTAT_MSG, i18n._("|%(game)s| Queried Master. Address: %(address)s, status: %(status)s, server count: %(servers)s.") % {'game': game_name, 'address': master_server_uri, 'status': master_server_status, 'servers': master_server_entry_count})
                    else:
                        print(QSTAT_MSG, i18n._("|%(game)s| Master query failed. Address: %(address)s, status: %(status)s.") % {'game': game_name, 'address': master_server_uri, 'status': master_server_status})

                elif qstat_entry['@type'] == backend_config_object['game'][game]['server_type']:  # If it is not bogus either...
                    server_table.append({})
                    server_table[-1]['host'] = qstat_entry['hostname']
                    server_table[-1]['password'] = False
                    server_table[-1]['game_id'] = game
                    server_table[-1]['game_mod'] = None

                    server_status = qstat_entry['@status']

                    if server_status == 'TIMEOUT' or server_status == 'DOWN' or server_status == 'ERROR':
                        server_table[-1]['name'] = None
                        server_table[-1]['game_mod'] = None
                        server_table[-1]['game_type'] = None
                        server_table[-1]['terrain'] = None
                        server_table[-1]['player_count'] = 0
                        server_table[-1]['player_limit'] = 0
                        server_table[-1]['ping'] = 9999
                    else:
                        if qstat_entry['name'] is None:
                            server_table[-1]['name'] = None
                        else:
                            server_table[-1]['name'] = re.sub(color_code_pattern, '', str(qstat_entry['name']))
                        server_table[-1]['game_type'] = re.sub(color_code_pattern, '', str(qstat_entry['gametype']))
                        server_table[-1]['terrain'] = str(qstat_entry['map'])
                        server_table[-1]['player_count'] = int(qstat_entry['numplayers'])
                        server_table[-1]['player_limit'] = int(qstat_entry['maxplayers'])
                        server_table[-1]['ping'] = int(qstat_entry['ping'])
                        server_table[-1]['rules'] = {}
                        server_table[-1]['players'] = []

                        if qstat_entry['rules'] is not None:
                            if isinstance(qstat_entry['rules']['rule'], dict):
                                rule = qstat_entry['rules']['rule']
                                qstat_entry['rules']['rule'] = [rule]

                            for rule in qstat_entry['rules']['rule']:
                                if '#text' in rule.keys():
                                    server_table[-1]['rules'][rule['@name']] = rule['#text']
                                else:
                                    server_table[-1]['rules'][rule['@name']] = None

                                if rule['@name'] == 'game':
                                    server_table[-1]['game_mod'] = str(rule['#text'])
                                elif rule['@name'] == 'g_needpass' or rule['@name'] == 'needpass' or rule['@name'] == 'si_usepass' or rule['@name'] == 'pswrd':
                                    server_table[-1]['password'] = bool(int(rule['#text']))
                        if qstat_entry['players'] is not None:
                            if isinstance(qstat_entry['players']['player'], dict):
                                player = qstat_entry['players']['player']
                                qstat_entry['players']['player'] = [player]

                            for player in qstat_entry['players']['player']:
                                server_table[-1]['players'].append({})
                                server_table[-1]['players'][-1]['name'] = re.sub(color_code_pattern, '', str(player['name']))
                                server_table[-1]['players'][-1]['score'] = int(player['score'])
                                server_table[-1]['players'][-1]['ping'] = int(player['ping'])
                        else:
                            server_table[-1]['players'] = None
            except Exception as e:
                print(QSTAT_MSG, e)
                proxy.append(Exception)
                return Exception

    if proxy is not None:
        for entry in server_table:
            proxy.append(entry)

    return server_table
