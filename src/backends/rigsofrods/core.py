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

from gi.repository import GLib

import os

import ast
import html.parser
import requests

import ping

BACKEND_CONFIG = os.path.split(__file__)[0] + "/settings.ini"


class ServerListParser(html.parser.HTMLParser):
    """The HTML Parser for Master server list retrieved from master_uri"""
    def __init__(self):
        super().__init__()

        self.list1 = []
        self.list2 = []
        self.parser_mode = 0

        self.replacements = (["<tr><td><b>Players</b></td><td><b>Type</b></td><td><b>Name</b></td><td><b>Terrain</b></td></tr>", ""],
                             ["rorserver://", ""],
                             ["user:pass@", ""],
                             ["<td valign='middle'>password</td>", "<td valign='middle'>True</td>"],
                             ["<td valign='middle'></td>", "<td valign='middle'>False</td>"])
        self.format = (("players", int),
                       ("password", bool),
                       ("name", str),
                       ("terrain", str))

        self.col_num = 0

    def handle_data(self, data):
        """Normal data handler"""
        if data == 'False':
            data = False
        if self.parser_mode == 0:
            if data == "Full server":
                self.parser_mode = 1
            else:
                if self.col_num >= len(self.format):
                    self.col_num = 0

                if self.col_num == 0:
                    self.list1.append([])

                    self.list1[-1] = dict()

                if self.format[self.col_num][0] == "players":
                    self.list1[-1]["player_count"] = int(data.split('/')[0])
                    self.list1[-1]["player_limit"] = int(data.split('/')[1])
                else:
                    self.list1[-1][self.format[self.col_num][0]] = self.format[self.col_num][1](data)

                self.col_num += 1

        elif self.parser_mode == 1:
            self.list2.append(data)

    def handle_starttag(self, tag, value):
        """Extracts host link from cell"""
        if tag == "a":
            self.list1[-1]["host"] = value[0][1].replace("/", "")


def add_game_name(array):
    for i in range(len(array)):
        array[i]["game"] = "rigsofrods"

    return


def add_rtt_info(array, bool_ping):
    """Appends server response time to the table. If bool_ping is set to false, print 9999 instead."""
    hosts_array = []
    rtt_temp_array = []
    rtt_temp_array.append([])
    rtt_array = []
    rtt_array.append([])

    for i in range(len(array)):
        hosts_array.append(array[i]["host"].split(':')[0])

    pinger = ping.Pinger()
    pinger.thread_count = 16
    pinger.hosts = hosts_array

    if bool_ping is True:
        pinger.status.clear()
        rtt_array = pinger.start()

        # Match ping in host list.
        for i in range(len(array)):
            ip = array[i]["host"].split(':')[0]
            array[i]["ping"] = rtt_array[ip]

    else:
        for i in range(len(hosts_array)):
            array[i]["ping"] = 9999

    return


def stat_master(bool_rtt):
    """Stats the master server"""

    backend_keyfile = GLib.KeyFile.new()
    backend_keyfile.load_from_file(BACKEND_CONFIG, GLib.KeyFileFlags.NONE)

    protocol = backend_keyfile.get_value("protocol", "version")
    master_uri = backend_keyfile.get_value("master", "uri") + '?version=' + protocol

    stream = requests.get(master_uri).text
    parser = ServerListParser()

    for i in range(len(parser.replacements)):
        stream = stream.replace(parser.replacements[i][0], parser.replacements[i][1])

    parser.feed(stream)
    server_table = parser.list1.copy()

    add_rtt_info(server_table, bool_rtt)
    add_game_name(server_table)

    return server_table
