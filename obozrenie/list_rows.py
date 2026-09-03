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

"""GObject rows the list views render, built from the domain records.

Rows carry data only: the views decide which icon a boolean turns into.
`GObject.Property(type=int)` is a 32-bit `gint`, which is why `models`
bounds every rendered integer to that range.
"""

from gi.repository import GObject

from obozrenie.models import Player, Server


def country_code_to_flag_emoji(code):
    """Convert an ISO 3166-1 alpha-2 country code to a flag emoji.

    Returns "" for empty, None, "unknown", or otherwise invalid input, so
    the result can be used directly as cell text (an empty string shows no
    flag).
    """
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


class GameRow(GObject.Object):

    """One configured game: id, display name, icon, and query status icon."""

    __gtype_name__ = "ObozrenieGameRow"

    game_id = GObject.Property(type=str, default="")
    name = GObject.Property(type=str, default="")
    icon = GObject.Property(type=object)
    # type=object allows None default; type=str silently becomes "" even with default=None
    status_icon = GObject.Property(type=object)

    def __init__(self, game_id: str, name: str, icon=None) -> None:
        super().__init__(game_id=game_id, name=name, icon=icon)


class ServerRow(GObject.Object):

    """One listed server: the record and the values the views render."""

    __gtype_name__ = "ObozrenieServerRow"

    server = GObject.Property(type=object)
    host = GObject.Property(type=str, default="")
    name = GObject.Property(type=str, default="")
    game_id = GObject.Property(type=str, default="")
    game_mod = GObject.Property(type=str, default="")
    game_type = GObject.Property(type=str, default="")
    terrain = GObject.Property(type=str, default="")
    country = GObject.Property(type=str, default="")
    flag = GObject.Property(type=str, default="")
    ping = GObject.Property(type=int, default=0)
    player_count = GObject.Property(type=int, default=0)
    player_limit = GObject.Property(type=int, default=0)
    password = GObject.Property(type=bool, default=False)
    secure = GObject.Property(type=bool, default=False)
    full = GObject.Property(type=bool, default=False)
    empty = GObject.Property(type=bool, default=False)
    game_icon = GObject.Property(type=object)

    def __init__(self, server: Server, game_icon=None) -> None:
        if type(server) is not Server:
            raise TypeError("ServerRow needs an exact Server record.")
        super().__init__(
            server=server,
            host=str(server.endpoint),
            name=server.name,
            game_id=server.game_id,
            game_mod=server.game_mod,
            game_type=server.game_type,
            terrain=server.terrain,
            country=server.country,
            flag=country_code_to_flag_emoji(server.country),
            ping=server.ping,
            player_count=server.player_count,
            player_limit=server.player_limit,
            password=server.password,
            secure=server.secure,
            full=server.player_count >= server.player_limit,
            empty=server.player_count == 0,
            game_icon=game_icon,
        )


class PlayerRow(GObject.Object):

    """One player of a listed server; a missing score stays `None`."""

    __gtype_name__ = "ObozreniePlayerRow"

    name = GObject.Property(type=str, default="")
    score = GObject.Property(type=object)
    ping = GObject.Property(type=int, default=0)

    def __init__(self, player: Player) -> None:
        if type(player) is not Player:
            raise TypeError("PlayerRow needs an exact Player record.")
        super().__init__(name=player.name, score=player.score, ping=player.ping)
