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
QSTAT_MSG = BACKENDCAT_MSG + i18n._("QStat")


def debug_msg(game_name, msg=None):
    if msg is not None:
        helpers.debug_msg([QSTAT_MSG, game_name, msg])


def parse_master_entry(qstat_entry, game):
    master_server_uri = qstat_entry['@address']
    master_server_status = qstat_entry['@status']
    server_dict = None
    debug_message = None
    if master_server_status == 'UP':
        master_server_entry_count = qstat_entry['@servers']
        debug_message = i18n._("Queried Master. Address: %(address)s, status: %(status)s, server count: %(servers)s.") % {'address': master_server_uri, 'status': master_server_status, 'servers': master_server_entry_count}
    else:
        debug_message = i18n._("Master query failed. Address: %(address)s, status: %(status)s.") % {'address': master_server_uri, 'status': master_server_status}

    return {'server_dict': server_dict, 'debug_msg': debug_message}


def parse_player_entry(player, color_code_pattern):
    """Parses player entry returned by QStat"""
    player_entry = {}

    player_entry['name'] = re.sub(color_code_pattern, '', str(player['name']))
    try:
        player_entry['score'] = int(player['score'])
    except:
        player_entry['score'] = None
    try:
        player_entry['ping'] = int(player['ping'])
    except:
        player_entry['ping'] = 9999

    return player_entry


def parse_server_entry(qstat_entry, game):
    """Parses server entry returned by QStat"""
    color_code_pattern = '[\\^](.)'

    debug_message = None
    server_dict = None

    server_status = qstat_entry['@status']
    if server_status == 'UP':
        server_dict = {}
        server_dict['host'] = qstat_entry['hostname']
        server_dict['password'] = False
        server_dict['secure'] = False
        server_dict['game_id'] = game
        server_dict['game_mod'] = ""

        if qstat_entry['name'] is None:
            server_dict['name'] = ""
        else:
            server_dict['name'] = re.sub(color_code_pattern, '', str(qstat_entry['name']))
        server_dict['game_type'] = re.sub(color_code_pattern, '', str(qstat_entry['gametype']))
        server_dict['terrain'] = str(qstat_entry['map'])
        try:
            server_dict['player_count'] = int(qstat_entry['numplayers'])
        except (KeyError, TypeError):
            server_dict['player_count'] = 0
        try:
            server_dict['player_limit'] = int(qstat_entry['maxplayers'])
        except (KeyError, TypeError):
            server_dict['player_limit'] = 0
        try:
            server_dict['ping'] = int(qstat_entry['ping'])
        except (KeyError, TypeError):
            server_dict['ping'] = 9999
        server_dict['rules'] = {}
        server_dict['players'] = []

        if qstat_entry['rules'] is not None:
            rules_list = helpers.enforce_array(qstat_entry['rules']['rule'])

            for rule in rules_list:
                rule_name = rule['@name']
                if '#text' in rule.keys():
                    server_dict['rules'][rule_name] = rule['#text']
                else:
                    server_dict['rules'][rule_name] = None

                rule_text = server_dict['rules'][rule_name]

                if rule_name in ('gamename'):
                    server_dict['game_name'] = str(rule_text)
                elif rule_name in ('punkbuster', 'sv_punkbuster', 'secure'):
                    server_dict['secure'] = bool(int(rule_text))
                elif rule_name in ('game'):
                    server_dict['game_mod'] = str(rule_text)
                elif rule_name in ('g_needpass', 'needpass', 'si_usepass', 'pswrd', 'password'):
                    try:
                        server_dict['password'] = bool(int(rule_text))
                    except TypeError:
                        server_dict['password'] = False

        if qstat_entry['players'] is not None:
            player_list = helpers.enforce_array(qstat_entry['players']['player'])

            for player_entry in player_list:
                server_dict['players'].append(parse_player_entry(player_entry, color_code_pattern))

    return {'server_dict': server_dict, 'debug_msg': debug_message}


def parse_qstat_entry(qstat_entry, game, master_type, server_type):
    response = None
    if qstat_entry['@type'] == master_type:
        response = parse_master_entry(qstat_entry, game)

    elif qstat_entry['@type'] == server_type:
        response = parse_server_entry(qstat_entry, game)

    debug_message = response['debug_msg']
    server_dict = response['server_dict']

    return {'server_dict': server_dict, 'debug_msg': debug_message}


def stat_master(game, game_info, master_list, proxy=None):
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

    for entry in master_list:
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

    debug_msg(game_name, i18n._("Requesting server info."))
    stat_start_time = time.time()
    try:
        qstat_output, _ = subprocess.Popen(qstat_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate(input=qstat_stdin_object.strip().encode())
        server_table_qstat_xml = qstat_output.decode()
        server_table_dict = json.loads(json.dumps(xmltodict.parse(server_table_qstat_xml)))
    except Exception as e:
        debug_msg(game_name, e.args[0])
        proxy.append(Exception)
        return Exception
    stat_end_time = time.time()

    stat_total_time = stat_end_time - stat_start_time
    debug_msg(game_name, i18n._("Received server info. Elapsed time: %(stat_time)s s.") % {'stat_time': round(stat_total_time, 2)})

    server_table_dict['qstat']['server'] = helpers.enforce_array(server_table_dict['qstat']['server'])  # Enforce server array even if n=1

    parse_start_time = time.time()
    for qstat_entry in server_table_dict['qstat']['server']:  # For every server...
        try:
            if server_table_dict['qstat']['server']['@type'] == backend_config_object['game'][game]['master_type']:
                debug_msg(game_name, i18n._("No valid masters specified. Please check your master server settings."))
                break

        except TypeError:
            try:
                response = parse_qstat_entry(qstat_entry, game, backend_config_object['game'][game]['master_type'], backend_config_object['game'][game]['server_type'])

                server_dict = response['server_dict']
                msg = response['debug_msg']
                debug_msg(game_name, msg)

                if server_dict is not None:
                    appendable = True
                    for filter_rule in ((server_game_name, "game_name"), (server_game_type, "game_type")):
                        if filter_rule[0] is not None:
                            if filter_rule[1] not in server_dict.keys():
                                appendable = False
                                break
                            else:
                                if server_dict[filter_rule[1]] != filter_rule[0]:
                                    appendable = False
                                    break
                    if appendable:
                        server_table.append(server_dict)

            except Exception as e:
                debug_msg(game_name, str(e.args[0]))
    parse_end_time = time.time()

    debug_msg(game_name, i18n._("Parsed QStat response. Elapsed time: %(parse_time)s ms") % {'parse_time': round((parse_end_time - parse_start_time) * 1000, 2)})

    if proxy is not None:
        for entry in server_table:
            proxy.append(entry)

    return server_table
