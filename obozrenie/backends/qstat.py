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

import ast
import xmltodict

import obozrenie.helpers as helpers
import obozrenie.ping as ping
from obozrenie.globals import *

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
    for master_server in server_table_dict:  # For every master...
        if server_table_dict[master_server] is not None:  # If master is not bogus...
            for server in server_table_dict[master_server]:  # For every server...
                if server_table_dict[master_server]['server']['@type'] == backend_config_object['game'][game]['server_type']:  # If it is not bogus either...
                    server_table.append({})
                    pass

    return server_table_dict
