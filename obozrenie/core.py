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

from collections import OrderedDict
import os
import multiprocessing
import threading

from gi.repository import GLib

from obozrenie import N_

from obozrenie import helpers
from obozrenie import backends
from obozrenie.option_lists import *
from obozrenie.global_settings import *


class Core:
    """
    Contains core logic and game table of Obozrenie server browser.
    """

    def __init__(self):
        self.gameconfig_object = helpers.load_table(GAME_CONFIG_FILE)

        self.game_table = self.create_game_table()

        try:
            import pygeoip
            try:
                open(GEOIP_DATA_FILE)
                print(CORE_MSG, N_("GeoIP data file {0} opened successfully".format(GEOIP_DATA_FILE)))
                self.geolocation = pygeoip
            except:
                print(CORE_MSG, N_("GeoIP data file not found. Disabling geolocation."))
                self.geolocation = None
        except ImportError:
            print(CORE_MSG, N_("PyGeoIP not found. Disabling geolocation."))
            self.geolocation = None

    def create_game_table(self):
        """
        Loads game list into a table
        """
        game_table = {}
        for game_id in self.gameconfig_object:
            game_table[game_id] = {}

            name = self.gameconfig_object[game_id]["name"]
            backend = self.gameconfig_object[game_id]["backend"]
            launch_pattern = self.gameconfig_object[game_id]["launch_pattern"]

            # Create dict groups
            game_table[game_id]["info"] = {}
            game_table[game_id]["settings"] = OrderedDict()
            game_table[game_id]["servers"] = []

            # Create setting groups
            for j in range(len(self.gameconfig_object[game_id]["settings"])):
                option_name = self.gameconfig_object[game_id]["settings"][j]
                game_table[game_id]["settings"][option_name] = ""

            game_table[game_id]["info"]["name"] = name
            game_table[game_id]["info"]["backend"] = backend
            game_table[game_id]["info"]["launch_pattern"] = launch_pattern
            game_table[game_id]["query-status"] = None

        return game_table

    def update_server_list(self, game, stat_callback=None):
        """Updates server lists"""
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(game, stat_callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, game, callback):
        """Separate update thread. Strictly per-game."""
        table = self.game_table
        backend = table[game]["info"]["backend"]
        state = table[game]["query-status"]

        # Start query if it's not up already
        if state != "working":
            table[game]["query-status"] = "working"
            print(CORE_MSG, N_("Refreshing servers for {0}".format(table[game]["info"]["name"])))
            server_list_temp = None
            stat_master_cmd = backends.backend_table[backend].stat_master
            try:
                mgr = multiprocessing.Manager()
                server_list_proxy = mgr.list()
                backend_process = multiprocessing.Process(target=stat_master_cmd, args=(game, table[game].copy(), server_list_proxy))
                backend_process.daemon=True
                backend_process.start()
                backend_process.join()
            except KeyError:
                print(CORE_MSG + N_("Internal backend error for {0}.".format(table[game]["info"]["name"])), ERROR_MSG)
                table[game]["query-status"] = "error"
                exit(1)

            # ListProxy -> list
            table[game]["servers"] = []
            server_table = table[game]["servers"]
            for entry in server_list_proxy:
                server_table.append(entry)

            for entry in table[game]["servers"]:
                entry['country'] = "unknown"
                if self.geolocation is not None:
                    host = entry["host"].split(':')[0]
                    try:
                        entry['country'] = self.geolocation.GeoIP(GEOIP_DATA_FILE).country_code_by_addr(host)
                    except OSError:
                        entry['country'] = self.geolocation.GeoIP(GEOIP_DATA_FILE).country_code_by_name(host)
                    except:
                        pass

            table[game]["query-status"] = "ready"

        # Workaround: GUI toolkits are not thread safe therefore request callback in the main thread
        if callback is not None:
            GLib.idle_add(callback, game, table.copy())

    def start_game(self, game, server, password):
        """Start game"""
        launch_thread = threading.Thread(target=helpers.launch_game, args=(game, self.game_table[game]["info"]["launch_pattern"], self.game_table[game]["settings"], server, password))
        launch_thread.daemon = True
        launch_thread.start()


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.
    """

    def __init__(self, core, profile_path):
        """Loads base variables into the class."""
        # Default configs
        self.default_common_settings_path = DEFAULT_COMMON_SETTINGS_PATH
        self.default_game_settings_path = DEFAULT_GAME_SETTINGS_PATH

        # User configs
        self.user_common_settings_path = os.path.join(profile_path, COMMON_SETTINGS_FILE)
        self.user_game_settings_path = os.path.join(profile_path, GAME_SETTINGS_FILE)

        self.dynamic_widget_table = get_game_options()
        self.common_settings_table = get_common_options()

        self.settings_table = {}

        self.core = core

    def load(self, callback_postgenload=None):
        """Loads configuration."""
        default_common_settings_table = helpers.load_table(self.default_common_settings_path)
        default_game_settings_table = helpers.load_table(self.default_game_settings_path)

        user_common_settings_table = helpers.load_table(self.user_common_settings_path)

        # Load into common settings table
        for group in self.common_settings_table:
            self.settings_table[group] = {}
            for option in self.common_settings_table[group]:
                # Define variables
                value = default_common_settings_table[group][option]
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
                value = default_game_settings_table[game][option]
                try:
                    value = user_game_settings_table[game][option]
                except ValueError:
                    pass
                except KeyError:
                    pass
                except TypeError:
                    pass

                self.core.game_table[game]["settings"][option] = value

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
    print(CORE_MSG, N_("This is the core module of Obozrenie Game Server Browser.\n"
             "Please run an appropriate UI instead."))
