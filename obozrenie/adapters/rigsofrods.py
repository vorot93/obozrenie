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

from collections.abc import Mapping
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from obozrenie import helpers, i18n
from obozrenie.adapters.http_masters import collect_http_masters
from obozrenie.global_strings import BACKENDCAT_MSG
from obozrenie.models import Endpoint, Player, Server

RIGSOFRODS_MSG = BACKENDCAT_MSG + i18n._("Rigs of Rods:")


def master_request_url(master_uri: str) -> str:
    parts = urlsplit(master_uri)
    query_parts = [
        part
        for part in (parts.query.split("&") if parts.query else [])
        if unquote_plus(part.partition("=")[0]) != "json"
    ]
    query_parts.append("json=true")
    return urlunsplit(parts._replace(query="&".join(query_parts)))


def parse_server_entry(game: str, entry: Mapping) -> Server:
    """Map one Rigs of Rods JSON server object to a server record."""
    if not isinstance(entry, Mapping):
        raise TypeError("Rigs of Rods server entry must be a mapping")

    password = entry["has-password"]
    if type(password) is not bool:
        raise TypeError("Rigs of Rods has-password must be a boolean")

    numeric_values = {
        field: entry[field]
        for field in ("current-users", "max-clients", "port")
    }
    numeric_values.update({
        field: entry.get(field, 0)
        for field in ("verified", "is-official")
    })
    for field, value in numeric_values.items():
        if type(value) is not int:
            raise TypeError(f"Rigs of Rods {field} must be an integer")

    string_values = {
        field: entry[field]
        for field in ("ip", "name", "terrain-name")
    }
    for field, value in string_values.items():
        if not isinstance(value, str):
            raise TypeError(f"Rigs of Rods {field} must be a string")

    user_list = entry.get("json-userlist", [])
    if not isinstance(user_list, list):
        raise TypeError("Rigs of Rods json-userlist must be a list")

    players = []
    for player in user_list:
        if not isinstance(player, Mapping):
            raise TypeError("Rigs of Rods player entry must be a mapping")
        username = player["username"]
        if not isinstance(username, str):
            raise TypeError("Rigs of Rods username must be a string")
        players.append(Player(username))

    return Server(
        endpoint=Endpoint(string_values["ip"], numeric_values["port"]),
        game_id=game,
        name=string_values["name"],
        password=password,
        player_count=numeric_values["current-users"],
        player_limit=numeric_values["max-clients"],
        game_type=game,
        terrain=string_values["terrain-name"],
        players=tuple(players),
        rules={
            "verified": numeric_values["verified"],
            "is_official": numeric_values["is-official"],
        },
    )


def adapt_server_list(game: str, entries: object) -> list[Server]:
    """Map a decoded Rigs of Rods server list to validated records."""
    if not isinstance(entries, list):
        raise ValueError("Rigs of Rods response must be a list of servers")

    server_list = []
    malformed_count = 0

    for entry in entries:
        try:
            server_list.append(parse_server_entry(game, entry))
        except (KeyError, TypeError, ValueError):
            malformed_count += 1

    if entries and not server_list:
        raise ValueError("Rigs of Rods response contained no valid servers")

    if malformed_count:
        helpers.debug_msg([
            RIGSOFRODS_MSG,
            i18n._("Skipped malformed Rigs of Rods server entries: %(count)i.") % {
                "count": malformed_count,
            },
        ])

    return server_list


def stat_master(game: str, game_info: dict, master_list: list) -> list[Server]:
    """Stats the master server"""
    return collect_http_masters(
        game,
        master_list,
        request_url=master_request_url,
        adapt_payload=adapt_server_list,
        diagnostic_prefix=RIGSOFRODS_MSG,
    )
