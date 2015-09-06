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
import xmltodict

import obozrenie.helpers as helpers
import obozrenie.ping as ping
from obozrenie.global_settings import *

BACKEND_CONFIG = os.path.join(SETTINGS_INTERNAL_BACKENDS_DIR, "qstat.toml")


def stat_master(game, game_table_slice):
    backend_config_object = helpers.load_table(BACKEND_CONFIG)
    hosts_array = game_table_slice["settings"]["master_uri"].split(',')

    pinger = ping.Pinger()
    pinger.hosts = list(set(hosts_array))
    pinger.action = "qstat"
    pinger.options = []
    pinger.options.append("-" + backend_config_object['game'][game]['master_key'])

    pinger.status.clear()
    server_table_qstat_xml = pinger.start()
    server_table_dict = {}
    for master_server in server_table_qstat_xml:
        server_table_dict[master_server] = xmltodict.parse(server_table_qstat_xml[master_server])['qstat']

    server_table = []
    color_code_pattern = '[\\^](.)'
    for master_server in server_table_dict:  # For every master...
        if server_table_dict[master_server] is not None:  # If master is not bogus...
            master_server_uri = None
            for qstat_entry in server_table_dict[master_server]['server']:
                if qstat_entry['@type'] == backend_config_object['game'][game]['master_type']:
                    master_server_uri = qstat_entry['@address']
                    break

            for qstat_entry in server_table_dict[master_server]['server']:  # For every server...
                if qstat_entry['@type'] == backend_config_object['game'][game]['server_type']:  # If it is not bogus either...
                    server_table.append({})
                    server_table[-1]['host'] = qstat_entry['hostname']
                    server_table[-1]['password'] = False
                    server_table[-1]['master'] = master_server_uri

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
