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
import subprocess
import time
import xmltodict

from obozrenie import N_

import obozrenie.helpers as helpers
import obozrenie.ping as ping
from obozrenie.global_settings import *

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "qstat.toml")


def split_server_list_into_masters(orig_list, split_key, split_value):
    master_server_uri = None
    split_dict = {}

    for server_entry in orig_list['qstat']['server']:
        if server_entry[split_key] == split_value:
            master_server_uri = server_entry['@address'].strip()
            split_dict[master_server_uri] = {'server': []}

        split_dict[master_server_uri]['server'].append(server_entry)

    return split_dict


def stat_master(game, game_table_slice):
    server_table_qstat_xml = []
    server_table_xmltodict = []
    server_table_dict = []

    master_server_uri = None
    stat_start_time = None
    stat_end_time = None

    backend_config_object = helpers.load_table(BACKEND_CONFIG)
    hosts_array = game_table_slice["settings"]["master_uri"].split(',')

    for entry in hosts_array:
        entry = entry.strip()

    hosts_array = list(set(hosts_array))

    qstat_opts = []
    qstat_cmd = ["qstat", "-xml", "-utf8", "-allowserverdups"]

    for entry in hosts_array:
        qstat_cmd.append("-" + backend_config_object['game'][game]['master_key'])
        qstat_cmd.append(entry)

    print(QSTAT_MSG, "Requesting server info")
    stat_start_time = time.time()
    qstat_output, _ = subprocess.Popen(qstat_cmd, stdout=subprocess.PIPE).communicate()
    server_table_qstat_xml = qstat_output.decode()
    stat_end_time = time.time()
    server_table_dict = xmltodict.parse(server_table_qstat_xml)

    stat_total_time = stat_end_time - stat_start_time
    print(QSTAT_MSG, N_("Received server info. Elapsed time: {0}s".format(round(stat_total_time, 2))))
    server_table = []
    color_code_pattern = '[\\^](.)'
    for qstat_entry in server_table_dict['qstat']['server']:  # For every server...
        if qstat_entry['@type'] == backend_config_object['game'][game]['master_type']:
            master_server_uri = qstat_entry['@address']
            master_server_status = qstat_entry['@status']
            if master_server_status == 'UP':
                master_server_entry_count = qstat_entry['@servers']
                print(N_("Queried Master. Address: {0}, status: {1}, server count: {2}".format(master_server_uri, master_server_status, master_server_entry_count)))
            else:
                print(N_("Master query failed. Address: {0}, status: {1}".format(master_server_uri, master_server_status)))

        elif qstat_entry['@type'] == backend_config_object['game'][game]['server_type']:  # If it is not bogus either...
            server_table.append({})
            server_table[-1]['host'] = qstat_entry['hostname']
            server_table[-1]['password'] = False
            server_table[-1]['game_id'] = game
            server_table[-1]['master'] = None

            if qstat_entry['@status'] == 'TIMEOUT' or qstat_entry['@status'] == 'DOWN':
                server_table[-1]['name'] = None
                server_table[-1]['game_type'] = None
                server_table[-1]['terrain'] = None
                server_table[-1]['player_count'] = 0
                server_table[-1]['player_limit'] = 0
                server_table[-1]['ping'] = 9999
            else:
                server_table[-1]['name'] = re.sub(color_code_pattern, '', qstat_entry['name'])
                server_table[-1]['game_type'] = qstat_entry['gametype']
                server_table[-1]['terrain'] = qstat_entry['map']
                server_table[-1]['player_count'] = int(qstat_entry['numplayers'])
                server_table[-1]['player_limit'] = int(qstat_entry['maxplayers'])
                server_table[-1]['ping'] = int(qstat_entry['ping'])

    return server_table
