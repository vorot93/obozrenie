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

import json
import subprocess
import time
import xmltodict

from obozrenie.global_settings import *
from obozrenie.global_strings import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "qstat.toml")
QSTAT_MSG = BACKENDCAT_MSG + i18n._("QStat:")


def stat_master(game, game_info, game_settings, proxy=None):
    hosts_array = []
    server_table = []
    server_table_qstat_xml = []
    server_table_dict = []

    qstat_stdin_object = ""

    master_server_uri = None
    stat_start_time = None
    stat_end_time = None

    game_name = game_info["name"]
    backend_config_object = helpers.load_table(BACKEND_CONFIG)

    if "server_gamename" not in backend_config_object['game'][game].keys():
        backend_config_object['game'][game]['server_gamename'] = None

    server_game_name = backend_config_object['game'][game]['server_gamename']
    if "server_gamename" in backend_config_object['game'][game].keys():
        server_game_name = backend_config_object['game'][game]['server_gamename']

    server_game_type = None
    if "server_gametype" in backend_config_object['game'][game].keys():
        server_game_type = backend_config_object['game'][game]['server_gametype']

    hosts_temp_array = list(game_settings["master_uri"])

    for entry in hosts_temp_array:
        entry = entry.strip()
        if '://' in entry:
            entry_protocol, entry_host = entry.split('://')
        else:
            entry_protocol = 'master'
            entry_host = entry
        hosts_array.append(entry_host)

    hosts_array = list(set(hosts_array))

    qstat_cmd = ["qstat", "-xml", "-utf8", "-maxsim", "9999", "-sendinterval", "1", "-R", "-P", "-f", "-"]

    qstat_stdin_descriptor = backend_config_object['game'][game]['master_type']
    if server_game_type is not None:
        qstat_stdin_descriptor = qstat_stdin_descriptor + ",game=" + server_game_type

    for entry in hosts_array:
        qstat_stdin_object = qstat_stdin_object + qstat_stdin_descriptor + " " + entry + "\n"

    print(i18n._(QSTAT_MSG), i18n._("|%(game)s| Requesting server info.") % {'game': game_name})
    stat_start_time = time.time()
    try:
        qstat_output, _ = subprocess.Popen(qstat_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate(input=qstat_stdin_object.strip().encode())
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
                    server_status = qstat_entry['@status']
                    if server_status == 'UP':
                        server_table.append({})
                        server_table[-1]['host'] = qstat_entry['hostname']
                        server_table[-1]['password'] = False
                        server_table[-1]['game_id'] = game
                        server_table[-1]['game_mod'] = None

                        if qstat_entry['name'] is None:
                            server_table[-1]['name'] = None
                        else:
                            server_table[-1]['name'] = re.sub(color_code_pattern, '', str(qstat_entry['name']))
                        server_table[-1]['game_type'] = re.sub(color_code_pattern, '', str(qstat_entry['gametype']))
                        server_table[-1]['terrain'] = str(qstat_entry['map'])
                        try:
                            server_table[-1]['player_count'] = int(qstat_entry['numplayers'])
                        except:
                            server_table[-1]['player_count'] = 0
                        try:
                            server_table[-1]['player_limit'] = int(qstat_entry['maxplayers'])
                        except:
                            server_table[-1]['player_limit'] = 0
                        try:
                            server_table[-1]['ping'] = int(qstat_entry['ping'])
                        except KeyError:
                            server_table[-1]['ping'] = 9999
                        server_table[-1]['rules'] = {}
                        server_table[-1]['players'] = []

                        if qstat_entry['rules'] is not None:
                            if isinstance(qstat_entry['rules']['rule'], dict):
                                rule = qstat_entry['rules']['rule']
                                qstat_entry['rules']['rule'] = [rule]

                            for rule in qstat_entry['rules']['rule']:
                                rule_name = rule['@name']
                                if '#text' in rule.keys():
                                    server_table[-1]['rules'][rule_name] = rule['#text']
                                else:
                                    server_table[-1]['rules'][rule_name] = None

                                rule_text = server_table[-1]['rules'][rule_name]

                                if rule_name in ['gamename']:
                                    server_table[-1]['game_name'] = str(rule_text)
                                elif rule_name in ['game']:
                                    server_table[-1]['game_mod'] = str(rule_text)
                                elif rule_name in ['g_needpass', 'needpass', 'si_usepass', 'pswrd', 'password']:
                                    try:
                                        server_table[-1]['password'] = bool(int(rule_text))
                                    except TypeError:
                                        server_table[-1]['password'] = False
                        if qstat_entry['players'] is not None:
                            if isinstance(qstat_entry['players']['player'], dict):
                                player = qstat_entry['players']['player']
                                qstat_entry['players']['player'] = [player]

                            for player in qstat_entry['players']['player']:
                                server_table[-1]['players'].append({})
                                server_table[-1]['players'][-1]['name'] = re.sub(color_code_pattern, '', str(player['name']))
                                try:
                                    server_table[-1]['players'][-1]['score'] = int(player['score'])
                                except:
                                    server_table[-1]['players'][-1]['score'] = None
                                try:
                                    server_table[-1]['players'][-1]['ping'] = int(player['ping'])
                                except:
                                    server_table[-1]['players'][-1]['ping'] = 9999
                        else:
                            server_table[-1]['players'] = None

                        for filter_rule in ((server_game_name, "game_name"), (server_game_type, "game_type")):
                            if filter_rule[0] is not None:
                                if filter_rule[1] not in server_table[-1].keys():
                                    del server_table[-1]
                                    continue
                                else:
                                    if server_table[-1][filter_rule[1]] != filter_rule[0]:
                                        del server_table[-1]
                                        continue


            except Exception as e:
                print(QSTAT_MSG, e)
                if proxy is not None:
                    proxy.append(Exception)
                return Exception

    if proxy is not None:
        for entry in server_table:
            proxy.append(entry)

    return server_table
