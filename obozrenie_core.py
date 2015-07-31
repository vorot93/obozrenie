#!/usr/bin/python
# This source file is part of Obozrenie
# Copyright 2015 Artem Vorotnikov

# For more information, see https://github.com/skybon/obozrenie

# Obozrenie is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.

# Obozrenie is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Obozrenie.  If not, see <http://www.gnu.org/licenses/>.

"""Core functions for Obozrenie Game Server Browser."""

import os
import threading

from gi.repository import GLib

import backends

GAME_CONFIG = os.path.join(os.path.dirname(__file__), "games.ini")


class Core:
    """Contains core logic of Obozrenie."""
    def __init__(self):
        self.game_config = GLib.KeyFile.new()
        self.game_config.load_from_file(GAME_CONFIG, GLib.KeyFileFlags.NONE)

        self.game_table_format = ("game",
                                  "player_count",
                                  "player_limit",
                                  "password",
                                  "host",
                                  "name",
                                  "terrain",
                                  "ping")

    def create_game_table(self):
        """
        Loads game list into a table
        """
        self.game_table = []
        for i in range(self.game_config.get_groups()[1]):
            self.game_table.append([])

            game_id = self.game_config.get_groups()[0][i]
            name = self.game_config.get_value(game_id, "name")
            backend = self.game_config.get_value(game_id, "backend")

            self.game_table[i].append([])

            self.game_table[i][0].append(game_id)
            self.game_table[i][0].append(name)
            self.game_table[i][0].append(backend)

            self.game_table[i].append([])

        return self.game_table

    def search_table(self, table, level, value):
        if level == 0:
            for i in range(len(table)):
                if table[i] == value:
                    return i
        elif level == 1:
            for i in range(len(table)):
                for j in range(len(table[i])):
                    if table[i][j] == value:
                        return i, j
        elif level == 2:
            for i in range(len(table)):
                for j in range(len(table[i])):
                    for k in range(len(table[i][j])):
                        if table[i][j][k] == value:
                            return i, j, k
        elif level is (3 or -1):
            for i in range(len(table)):
                for j in range(len(table[i])):
                    for k in range(len(table[i][j])):
                        for l in range(len(table[i][j][k])):
                            if table[i][j][k][l] == value:
                                return i, j, k, l
        else:
            print("Please specify correct search level: 0, 1, 2, 3, or -1 for deepest possible.")

    def dict_to_list(self, dict_table, key_list):
        list_table = []

        for i in range(len(dict_table)):
            list_table.append([])

            for j in range(len(key_list)):
                list_table[i].append(dict_table[i][key_list[j]])

        return list_table

    def update_server_list(self, game, bool_ping, callback):
        """Updates server lists"""
        game_index = self.search_table(self.game_table, 2, game)[0]
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(game_index, bool_ping, self.game_table_format, callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, game_index, bool_ping, game_table_format, callback):
        """Separate update thread"""
        backend = self.game_table[game_index][0][2]
        if backend == "rigsofrods":
            self.game_table[game_index][1] = backends.rigsofrods.core.stat_master(bool_ping, game_table_format)
        elif backend == "qstat":
            print("----------\n"
                  "QStat backend has not been implemented yet. Stay tuned!\n"
                  "----------")

        self.game_table[game_index][1] = self.dict_to_list(self.game_table[game_index][1], self.game_table_format)

        # Calls GTK+ code in the main thread
        if callback is not None:
            GLib.idle_add(callback, self.game_table[game_index][1])

    def get_server_info():
        pass

    def start_game(path):
        """Start game"""
        from subprocess import call

        try:
            call(path)
            return 0
        except OSError:
            print("Error launching the game.")

if __name__ == "__main__":
    print("This is a core module of Obozrenie Game Server Browser.\n"
          "Please run an appropriate UI instead.")
