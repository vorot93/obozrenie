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


import math
from collections.abc import Mapping

from obozrenie import helpers, i18n
from obozrenie.adapters.http_masters import collect_http_masters
from obozrenie.global_strings import BACKENDCAT_MSG
from obozrenie.models import Endpoint, Player, Server

MINETEST_MSG = BACKENDCAT_MSG + i18n._("Minetest:")


def _parse_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"Minetest {field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if not value.isdecimal():
            raise ValueError(f"Minetest {field} must be an integer")
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"Minetest {field} must be an integer")
        return int(value)
    raise TypeError(f"Minetest {field} must be an integer")


def parse_json_entry(game: str, entry: Mapping) -> Server:
    if not isinstance(entry, Mapping):
        raise TypeError("Minetest server entry must be a mapping")

    address = entry.get("address") or entry.get("ip")
    if not isinstance(address, str):
        raise TypeError("Minetest server address must be a string")
    if not address:
        raise ValueError("Minetest server address must be nonempty")

    password = entry.get("password", False)
    if type(password) is not bool:
        raise TypeError("Minetest password must be a boolean")

    name = entry.get("name", "")
    if not isinstance(name, str):
        raise TypeError("Minetest name must be a string")

    game_type = entry.get("gameid", "")
    if not isinstance(game_type, str):
        raise TypeError("Minetest gameid must be a string")

    clients_list = entry.get("clients_list", [])
    if not isinstance(clients_list, list) or any(
        not isinstance(player, str) for player in clients_list
    ):
        raise TypeError("Minetest clients_list must be a list of strings")

    return Server(
        endpoint=Endpoint(address, _parse_integer(entry["port"], "port")),
        game_id=game,
        name=name,
        password=password,
        player_count=_parse_integer(entry["clients"], "clients"),
        player_limit=_parse_integer(entry["clients_max"], "clients_max"),
        game_type=game_type,
        players=tuple(Player(player) for player in clients_list),
        rules={
            key: _parse_integer(entry[key], key)
            for key in ("proto_min", "proto_max")
            if key in entry
        },
    )


def adapt_server_list(game: str, payload: object) -> list[Server]:
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("list"), list
    ):
        raise ValueError("Minetest response must contain a list of servers")

    server_json_table = payload["list"]
    server_table = []

    invalid_count = 0
    for entry in server_json_table:
        try:
            server_table.append(parse_json_entry(game, entry))
        except (KeyError, OverflowError, TypeError, ValueError):
            invalid_count += 1

    if server_json_table and not server_table:
        raise ValueError("Minetest response contained no valid servers")

    if invalid_count:
        helpers.debug_msg([
            MINETEST_MSG,
            i18n._("Skipped malformed Minetest server entries: %(count)i.") % {
                "count": invalid_count,
            },
        ])

    return server_table


def stat_master(game: str, game_info: dict, master_list: list) -> list[Server]:
    """Stats the master server"""
    return collect_http_masters(
        game,
        master_list,
        request_url=lambda source: source,
        adapt_payload=adapt_server_list,
        diagnostic_prefix=MINETEST_MSG,
    )
