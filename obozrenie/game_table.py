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

"""The per-game store that adapters fill and the GTK UI reads."""

import copy
import threading
from enum import Enum, auto

from obozrenie import i18n, resources
from obozrenie.models import Server


class QueryStatus(Enum):
    EMPTY = auto()
    WORKING = auto()
    READY = auto()
    ERROR = auto()


def validated_server_batch(servers) -> list[Server]:
    """Materialize a server batch, rejecting anything but exact records."""
    server_batch = list(servers)
    for index, server in enumerate(server_batch):
        if type(server) is not Server:
            raise TypeError(
                f"GameTable servers[{index}] must be an exact Server record."
            )
    return server_batch


class GameTable:
    """
    The Game Table is the persistent game information storage that is used through out Obozrenie.

    Game Table internal structure:
    ------------------------------

        game_table - gameA - info     - name
                                      - backend
                                      ....

                           - settings - path
                                      - master_server_uri
                                      ....

                           - servers  - server_entry1     - host
                                                          - name
                                                          - ping
                                                          ....

                                      - server_entry2     - host
                                                          - name
                                                          - ping
                                                          ....
                     gameB - info     - name
                                      - backend
                                      ....

                           - settings - path
                                      - master_server_uri
                                      ....

                           - servers  - server_entry1     - host
                                                          - name
                                                          - ping
                                                          ....

                                      - server_entry2     - host
                                                          - name
                                                          - ping
                                                          ....
                     gameC - ....
                     ....

    ------------------------------

    Please mind, however, that the storage itself is not available for direct access. This helps us keep interface stable and game table internal structure agile.

    Instead this class holds the Game Table and provides read and write accessor methods.

    Read accessors never expose mutable internal state; mutable results are deep copies.
    """

    def __init__(self, gameconfig_object):
        self._lock = threading.RLock()
        with self._lock:
            self._game_table = self.create_game_table(gameconfig_object)

    @classmethod
    def from_shipped_list(cls) -> "GameTable":
        """Load the game list shipped inside the package."""
        return cls(resources.load_toml("settings", "internal", "game_lists.toml"))

    def __repr__(self):
        with self._lock:
            game_num = len(self._game_table)
        return i18n._("<Game Table - games: %(game_num)i, id: %(gt_id)i>") % {
            'game_num': game_num,
            'gt_id': id(self),
        }

    def create_game_table(self, gameconfig_object) -> dict:
        """
        Loads game list into a table.
        """
        game_table = {}
        for game_id, game_config in gameconfig_object.items():
            info = {
                "name": str(game_config["name"]),
                "adapter": str(game_config["adapter"]),
                "launch_pattern": str(game_config["launch_pattern"]),
            }
            if "steam_app_id" in game_config:
                info["steam_app_id"] = str(game_config["steam_app_id"])

            settings = {
                option_name: "" for option_name in game_config.get("settings", [])
            }
            game_table[game_id] = {
                "info": info,
                "settings": settings,
                "servers": [],
                "query-status": QueryStatus.EMPTY,
                "query-error": None,
                "query-token": None,
            }
        return game_table

    def _get_game_entry(self, game: str) -> dict:
        if game in ('', None):
            raise ValueError(i18n._('Invalid game specified.'))
        try:
            return self._game_table[game]
        except KeyError:
            raise ValueError(
                i18n._('Game not found: %(game)s') % {'game': game})

    def get_game_table_copy(self):
        """
        Returns a deep copy of the Game Table.
        It is the slowest accessor method. Please try to query for specific information.
        """
        with self._lock:
            return copy.deepcopy(self._game_table)

    def get_game_set(self):
        """
        Returns the set of games held by Game Table.
        """
        with self._lock:
            return set(self._game_table)

    def get_game_info(self, game):
        """
        Returns information about the specified game.
        """
        with self._lock:
            return copy.deepcopy(self._get_game_entry(game)["info"])

    def get_game_settings(self, game):
        """
        Returns settings for the specified game.
        """
        with self._lock:
            return copy.deepcopy(self._get_game_entry(game)["settings"])

    def set_game_setting(self, game, option, value):
        faulty_param = None
        if game in ('', None):
            faulty_param = i18n._('game id')
        elif option in ('', None):
            faulty_param = i18n._('option')
        if faulty_param is not None:
            raise ValueError(
                i18n._('Invalid %(param)s specified.') % {'param': faulty_param})

        with self._lock:
            self._get_game_entry(game)["settings"][option] = copy.deepcopy(value)

    def get_query_status(self, game: str) -> QueryStatus:
        with self._lock:
            # Enum members are immutable singletons, so returning one cannot leak state.
            return self._get_game_entry(game)["query-status"]

    def get_query_error(self, game: str) -> str | None:
        with self._lock:
            return copy.deepcopy(self._get_game_entry(game)["query-error"])

    def begin_query(self, game: str) -> object | None:
        """Admit one query and return its token, or None if one is running."""
        with self._lock:
            game_entry = self._get_game_entry(game)
            if game_entry["query-status"] is QueryStatus.WORKING:
                return None
            token = object()
            game_entry["query-token"] = token
            game_entry["query-status"] = QueryStatus.WORKING
            game_entry["query-error"] = None
            return token

    def _owns_query(self, game_entry: dict, attempt_token: object) -> bool:
        """Report whether this exact attempt still owns the running query."""
        return (
            game_entry["query-status"] is QueryStatus.WORKING
            and game_entry["query-token"] is attempt_token
        )

    def finish_query(
        self,
        game: str,
        attempt_token: object,
        servers,
    ) -> bool:
        """Publish one attempt's servers; report whether it still owned them."""
        server_batch = validated_server_batch(servers)
        with self._lock:
            game_entry = self._get_game_entry(game)
            if not self._owns_query(game_entry, attempt_token):
                return False
            game_entry["servers"] = server_batch
            game_entry["query-error"] = None
            game_entry["query-status"] = QueryStatus.READY
            game_entry["query-token"] = None
            return True

    def fail_query(self, game: str, attempt_token: object, error: str) -> bool:
        """Publish one attempt's failure; report whether it still owned it."""
        if type(error) is not str:
            raise TypeError("GameTable error must be an exact str.")
        with self._lock:
            game_entry = self._get_game_entry(game)
            if not self._owns_query(game_entry, attempt_token):
                return False
            game_entry["query-error"] = error
            game_entry["query-status"] = QueryStatus.ERROR
            game_entry["query-token"] = None
            return True

    def get_server_info(self, game: str, host: str) -> Server:
        faulty_param = None
        if game in ('', None):
            faulty_param = i18n._('game id')
        elif host in ('', None):
            faulty_param = i18n._('hostname')
        if faulty_param is not None:
            raise ValueError(
                i18n._('Invalid %(param)s specified.') % {'param': faulty_param})

        with self._lock:
            server_table = self._get_game_entry(game)["servers"]
            for server in server_table:
                if str(server.endpoint) == host:
                    return copy.deepcopy(server)
        raise ValueError(i18n._('Server not found: %(host)s') % {'host': host})

    def get_servers_data(self, game: str) -> list[Server]:
        with self._lock:
            return copy.deepcopy(self._get_game_entry(game)["servers"])
