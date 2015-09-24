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
import copy
import os
import multiprocessing
import threading

from gi.repository import GLib

from obozrenie.global_settings import *
from obozrenie.global_strings import *
from obozrenie.option_lists import *

import obozrenie.i18n as i18n
import obozrenie.helpers as helpers
import obozrenie.backends as backends
import obozrenie.launch as launch


class Core:
    """
    Contains core logic and game table of Obozrenie server browser.
    """

    def __init__(self):
        self.__game_table = self.create_game_table(helpers.load_table(GAME_CONFIG_FILE))

        try:
            import pygeoip
            try:
                open(GEOIP_DATA_FILE)
                print(CORE_MSG, i18n._("GeoIP data file %(geoip_data_file)s opened successfully.") % {'geoip_data_file': GEOIP_DATA_FILE})
                self.geolocation = pygeoip
            except FileNotFoundError:
                print(CORE_MSG, i18n._("GeoIP data file not found. Disabling geolocation."))
                self.geolocation = None
        except ImportError:
            print(CORE_MSG, i18n._("PyGeoIP not found. Disabling geolocation."))
            self.geolocation = None

    def create_game_table(self, gameconfig_object):
        """
        Loads game list into a table
        """
        game_table = {}
        for game_id in gameconfig_object:
            game_table[game_id] = {}

            name = gameconfig_object[game_id]["name"]
            backend = gameconfig_object[game_id]["backend"]
            launch_pattern = gameconfig_object[game_id]["launch_pattern"]
            try:
                steam_app_id = gameconfig_object[game_id]["steam_app_id"]
            except KeyError:
                pass

            # Create dict groups
            game_table[game_id]["info"] = {}
            game_table[game_id]["settings"] = OrderedDict()
            game_table[game_id]["servers"] = []

            # Create setting groups
            for j in range(len(gameconfig_object[game_id]["settings"])):
                option_name = gameconfig_object[game_id]["settings"][j]
                game_table[game_id]["settings"][option_name] = ""

            game_table[game_id]["info"]["name"] = name
            game_table[game_id]["info"]["backend"] = backend
            game_table[game_id]["info"]["launch_pattern"] = launch_pattern
            try:
                game_table[game_id]["info"]["steam_app_id"] = steam_app_id
            except NameError:
                pass
            game_table[game_id]["query-status"] = None

        return game_table

    def get_game_table_copy(self):
        game_table_copy = copy.deepcopy(self.__game_table)
        return game_table_copy

    def get_game_set(self):
        game_set = set(self.__game_table.keys())
        return game_set

    def get_game_info(self, game):
        game_info = copy.deepcopy(self.__game_table[game]["info"])
        return game_info

    def get_game_settings(self, game):
        game_settings = copy.deepcopy(self.__game_table[game]["settings"])
        return game_settings

    def set_game_setting(self, game, option, value):
        self.__game_table[game]["settings"][option] = value

    def get_query_status(self, game):
        query_status = copy.deepcopy(self.__game_table[game]["query-status"])
        return query_status

    def set_query_status(self, game, status):
        self.__game_table[game]["query-status"] = status

    def get_servers_data(self, game):
        servers_data = copy.deepcopy(self.__game_table[game]["servers"])
        return servers_data

    def set_servers_data(self, game, servers_data):
        self.clear_servers_data(game)
        for entry in servers_data:
            self.__game_table[game]["servers"].append(entry)

    def clear_servers_data(self, game):
        self.__game_table[game]["servers"] = []

    def update_server_list(self, game, stat_callback=None):
        """Updates server lists."""
        stat_master_thread = threading.Thread(target=self.stat_master_target, args=(game, stat_callback))
        stat_master_thread.daemon = True
        stat_master_thread.start()

    def stat_master_target(self, game, callback):
        """Separate update thread. Strictly per-game."""
        game_info = self.get_game_info(game)
        game_settings = self.get_game_settings(game)
        game_name = game_info["name"]
        backend = game_info["backend"]

        # Start query if it's not up already
        if self.get_query_status(game) != "working":
            self.set_query_status(game, "working")
            print(CORE_MSG, i18n._("Refreshing server list for %(game)s.") % {'game': game_name})
            server_list_proxy = None
            stat_master_cmd = backends.backend_table[backend].stat_master
            try:
                mgr = multiprocessing.Manager()
                server_list_proxy = mgr.list()
                backend_process = multiprocessing.Process(target=stat_master_cmd, args=(game, game_info, game_settings, server_list_proxy))
                backend_process.daemon=True
                backend_process.start()
                backend_process.join()
                if len(server_list_proxy) > 0 and server_list_proxy[0] is Exception:
                    raise Exception
            except Exception as e:
                print(CORE_MSG, e)
                print(CORE_MSG, i18n._("Internal backend error for %(game)s.") % {'game': game_name}, ERROR_MSG)
                self.set_query_status(game, "error")

            # ListProxy -> list
            if self.get_query_status(game) != "error":
                self.set_servers_data(game, server_list_proxy)
                temp_list = []
                for entry in server_list_proxy:
                    temp_list.append(entry)

                for entry in temp_list:
                    entry['country'] = "unknown"
                    if self.geolocation is not None:
                        host = entry["host"].split(':')[0]
                        try:
                            entry['country'] = self.geolocation.GeoIP(GEOIP_DATA_FILE).country_code_by_addr(host)
                        except OSError:
                            entry['country'] = self.geolocation.GeoIP(GEOIP_DATA_FILE).country_code_by_name(host)
                        except:
                            pass

                self.set_servers_data(game, temp_list)

                self.set_query_status(game, "ready")

        # Workaround: GUI toolkits are not thread safe therefore request callback in the main thread
        if callback is not None:
            GLib.idle_add(callback, game)

    def start_game(self, game, server, password):
        """Start game"""
        game_info = self.get_game_info(game)
        game_settings = self.get_game_settings(game)
        launch_pattern = game_info["launch_pattern"]
        steam_app_id = None
        try:
            if game_settings["steam_launch"] is True:
                try:
                    steam_app_id = game_info["steam_app_id"]
                    launch_pattern = "steam"
                except KeyError:
                    pass
        except:
            pass

        launch_process = multiprocessing.Process(target=launch.launch_game, args=(game, launch_pattern, game_settings, server, password, steam_app_id))
        launch_process.daemon = True
        launch_process.start()


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
        for game in self.core.get_game_set():
            for option in self.core.get_game_settings(game):
                value = default_game_settings_table[game][option]
                try:
                    value = user_game_settings_table[game][option]
                except ValueError:
                    pass
                except KeyError:
                    pass
                except TypeError:
                    pass

                self.core.set_game_setting(game, option, value)

    def save(self):
        """Saves configuration."""
        # Save common settings table
        helpers.save_table(self.user_common_settings_path, self.settings_table)

        # Compile game settings table
        user_game_settings_table = {}
        for game in self.core.get_game_set():
            user_game_settings_table[game] = self.core.get_game_settings(game)

        # Save game settings
        helpers.save_table(self.user_game_settings_path, user_game_settings_table)


if __name__ == "__main__":
    print(CORE_MSG, i18n._("This is the core module of Obozrenie Game Server Browser.\n"
                           "Please run an appropriate UI instead."))
