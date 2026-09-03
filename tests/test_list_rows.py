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

import unittest

from obozrenie import list_rows
from obozrenie.models import Endpoint, Player, Server


class CountryEmojiTests(unittest.TestCase):
    def test_two_letter_code(self):
        self.assertEqual(list_rows.country_code_to_flag_emoji("US"), "\U0001F1FA\U0001F1F8")

    def test_lowercase_is_upper_cased(self):
        self.assertEqual(list_rows.country_code_to_flag_emoji("gb"), "\U0001F1EC\U0001F1E7")

    def test_empty_none_unknown_and_non_alpha_return_empty(self):
        for code in ("", None, "unknown", "U1"):
            with self.subTest(code=code):
                self.assertEqual(list_rows.country_code_to_flag_emoji(code), "")


class GameRowTests(unittest.TestCase):
    def test_row_carries_id_name_icon_and_a_settable_status_icon(self):
        icon = object()
        row = list_rows.GameRow("q3a", "Quake III", icon)

        self.assertEqual((row.game_id, row.name), ("q3a", "Quake III"))
        self.assertIs(row.icon, icon)
        self.assertIsNone(row.status_icon)

        changes = []
        row.connect("notify::status-icon", lambda *_: changes.append(row.status_icon))
        row.status_icon = "emblem-ok-symbolic"
        row.status_icon = None

        self.assertEqual(changes, ["emblem-ok-symbolic", None])


class ServerRowTests(unittest.TestCase):
    def test_row_renders_the_record_and_derives_flag_full_and_empty(self):
        server = Server(
            endpoint=Endpoint("2001:db8::40", 30000),
            game_id="g",
            name="IPv6 server",
            password=True,
            player_count=4,
            player_limit=4,
            ping=21,
            secure=True,
            country="US",
            game_mod="mod",
            game_type="ctf",
            terrain="map",
        )
        icon = object()

        row = list_rows.ServerRow(server, icon)

        self.assertIs(row.server, server)
        self.assertIs(row.game_icon, icon)
        self.assertEqual(row.host, "[2001:db8::40]:30000")
        self.assertEqual(
            (row.name, row.game_id, row.game_mod, row.game_type, row.terrain),
            ("IPv6 server", "g", "mod", "ctf", "map"),
        )
        self.assertEqual((row.country, row.flag), ("US", "\U0001F1FA\U0001F1F8"))
        self.assertEqual((row.ping, row.player_count, row.player_limit), (21, 4, 4))
        self.assertEqual((row.password, row.secure), (True, True))
        self.assertEqual((row.full, row.empty), (True, False))

    def test_defaults_give_no_flag_an_empty_and_full_row(self):
        row = list_rows.ServerRow(Server(endpoint=Endpoint("h.example", 1), game_id="g"))

        self.assertEqual(row.flag, "")
        self.assertIsNone(row.game_icon)
        # Zero players of a zero limit is both empty and full, as it is today.
        self.assertEqual((row.full, row.empty), (True, True))
        self.assertEqual(row.ping, 9999)

    def test_integer_properties_hold_every_value_the_records_admit(self):
        floor, ceiling = -(1 << 31), (1 << 31) - 1
        low = list_rows.ServerRow(Server(endpoint=Endpoint("a.example", 1), game_id="g", ping=floor))
        high = list_rows.ServerRow(Server(
            endpoint=Endpoint("b.example", 2),
            game_id="g",
            player_count=ceiling,
            player_limit=ceiling,
            ping=ceiling,
        ))

        self.assertEqual(low.ping, floor)
        self.assertEqual((high.player_count, high.player_limit, high.ping), (ceiling, ceiling, ceiling))

    def test_row_requires_an_exact_server_record(self):
        with self.assertRaises(TypeError):
            list_rows.ServerRow({"host": "h.example:1"})


class PlayerRowTests(unittest.TestCase):
    def test_row_keeps_a_missing_score_as_none(self):
        scored = list_rows.PlayerRow(Player("first", 7, 20))
        unscored = list_rows.PlayerRow(Player("second"))

        self.assertEqual((scored.name, scored.score, scored.ping), ("first", 7, 20))
        self.assertEqual((unscored.name, unscored.score, unscored.ping), ("second", None, 9999))

    def test_row_requires_an_exact_player_record(self):
        with self.assertRaises(TypeError):
            list_rows.PlayerRow(("name", 1, 2))


if __name__ == "__main__":
    unittest.main()
