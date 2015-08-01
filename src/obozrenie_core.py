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
import helpers


class Core:
    """
    Contains core logic and game table of Obozrenie server browser.
    """
    GAME_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "games.ini")

    def __init__(self):
        self.game_config = GLib.KeyFile.new()
        self.game_config.load_from_file(self.GAME_CONFIG_FILE, GLib.KeyFileFlags.NONE)

    def create_game_table(self):
        """
        Loads game list into a table
        """
        self.game_table = []
        for i in range(len(self.game_config.get_groups()[0])):
            self.game_table.append({})

            game_id = self.game_config.get_groups()[0][i]
            name = self.game_config.get_value(game_id, "name")
            backend = self.game_config.get_value(game_id, "backend")

            self.game_table[i]["id"] = game_id

            # Create setting groups
            self.game_table[i]["info"] = {}
            self.game_table[i]["settings"] = {}
            self.game_table[i]["servers"] = []

            self.game_table[i]["info"]["name"] = name
            self.game_table[i]["info"]["backend"] = backend

        return self.game_table

    def clear_server_list(self, game):
        """Clears server list"""
        game_index = helpers.search_dict_table(self.game_table, "id", game)
        self.game_table[game_index]["servers"].clear()

    def update_server_list(self, game, bool_ping, callback):
        """Updates server lists"""
        game_index = helpers.search_dict_table(self.game_table, "id", game)
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(game_index, bool_ping, callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, game_index, bool_ping, callback):
        """Separate update thread"""
        backend = self.game_table[game_index]["info"]["backend"]
        if backend == "rigsofrods":
            self.game_table[game_index]["servers"] = backends.rigsofrods.core.stat_master(bool_ping)
        elif backend == "qstat":
            print("----------\n"
                  "QStat backend has not been implemented yet. Stay tuned!\n"
                  "----------")

        # Workaround for GTK+: GTK+ is not thread safe, call in the main thread
        if callback is not None:
            GLib.idle_add(callback, self.game_table)

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
