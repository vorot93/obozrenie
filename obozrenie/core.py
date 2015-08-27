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

import pytoml

from gi.repository import GLib

from . import helpers
from . import backends
from .globals import *


class Core:
    """
    Contains core logic and game table of Obozrenie server browser.
    """

    def __init__(self):
        self.gameconfig_object = pytoml.load(open(GAME_CONFIG_FILE, 'r'))

        self.game_table = self.create_game_table()

    def create_game_table(self):
        """
        Loads game list into a table
        """
        game_table = {}
        for game_id in self.gameconfig_object:
            game_table[game_id] = {}

            name = self.gameconfig_object[game_id]["name"]
            backend = self.gameconfig_object[game_id]["backend"]

            # Create dict groups
            game_table[game_id]["info"] = {}
            game_table[game_id]["settings"] = {}
            game_table[game_id]["servers"] = []

            # Create setting groups
            for j in range(len(self.gameconfig_object[game_id]["settings"])):
                option_name = self.gameconfig_object[game_id]["settings"][j]
                game_table[game_id]["settings"][option_name] = ""

            game_table[game_id]["info"]["name"] = name
            game_table[game_id]["info"]["backend"] = backend

        return game_table

    def clear_server_list(self, game):
        """Clears server list"""
        game_index = helpers.search_dict_table(self.game_table, "id", game)
        self.game_table[game_index]["servers"].clear()

    def update_server_list(self, game, stat_callback=None):
        """Updates server lists"""
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(game, stat_callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, game, callback):
        """Separate update thread"""
        backend = self.game_table[game]["info"]["backend"]
        try:
            print("Refreshing servers for", game)
            self.game_table[game]["servers"] = backends.backend_table[backend].stat_master(game, self.game_table[game].copy())
        except KeyError:
            print("Specified backend for", self.game_table[game]["info"]["name"], "does not exist.", ERROR_MSG)

        # Workaround: GUI toolkits are not thread safe therefore request callback in the main thread
        if callback is not None:
            GLib.idle_add(callback, self.game_table[game])

    def get_server_info():
        pass

    def start_game(self, game, server, password):
        """Start game"""
        helpers.launch_game(game, self.game_table[game]["settings"], server, password)


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.
    """

    def __init__(self, core, profile_path):
        """Loads base variables into the class."""
        # Internal configs
        self.common_settings_config_path = os.path.join(SETTINGS_INTERNAL_DIR, "options_common.toml")
        self.dynamic_widget_config_path = os.path.join(SETTINGS_INTERNAL_DIR, "options_game.toml")
        self.defaults_path = os.path.join(SETTINGS_DEFAULTS_DIR, "defaults.toml")

        # User configs
        self.user_common_settings_path = os.path.join(profile_path, "settings.toml")
        self.user_game_settings_path = os.path.join(profile_path, "games.toml")

        self.dynamic_widget_table = helpers.load_table(self.dynamic_widget_config_path)
        self.common_settings_table = helpers.load_table(self.common_settings_config_path)

        self.settings_table = {}

        self.core = core

    def load(self, callback_postgenload=None):
        """Loads configuration."""
        defaults_table = helpers.load_table(self.defaults_path)
        user_common_settings_table = helpers.load_table(self.user_common_settings_path)

        # Load into common settings table
        for group in self.common_settings_table:
            self.settings_table[group] = {}
            for option in self.common_settings_table[group]:
                # Define variables
                value = defaults_table[group][option]
                try:
                    value = user_common_settings_table[group][option]
                except ValueError:
                    pass
                except KeyError:
                    pass
                except TypeError:
                    pass

                self.settings_table[group][option] = value

                if callback_postgenload is not None:
                    callback_postgenload(self.common_settings_table, group, option, value)

        # Load game settings table
        user_game_settings_table = helpers.load_table(self.user_game_settings_path)

        # Set game settings
        for game in self.core.game_table:
            for option in self.core.game_table[game]["settings"]:
                try:
                    self.core.game_table[game]["settings"][option] = user_game_settings_table[game][option]
                except ValueError:
                    pass
                except KeyError:
                    pass
                except TypeError:
                    pass

    def save(self):
        """Saves configuration."""
        # Save common settings table
        helpers.save_table(self.user_common_settings_path, self.settings_table)

        # Compile game settings table
        user_game_settings_table = {}
        for game in self.core.game_table:
            user_game_settings_table[game] = self.core.game_table[game]["settings"]

        # Save game settings
        helpers.save_table(self.user_game_settings_path, user_game_settings_table)


if __name__ == "__main__":
    print("This is a core module of Obozrenie Game Server Browser.\n"
          "Please run an appropriate UI instead.")
