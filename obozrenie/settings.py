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

"""User settings: the persisted common options and per-game values."""

import contextlib
import os

import filelock

from obozrenie import helpers, i18n, resources
from obozrenie.diagnostics import SafeDiagnostic, SafeDiagnosticError
from obozrenie.global_settings import COMMON_SETTINGS_FILE, GAME_SETTINGS_FILE


_PROFILE_LOCK_TIMEOUT_SECONDS = 5.0


class SettingsLockError(SafeDiagnosticError):
    def __init__(self) -> None:
        super().__init__(SafeDiagnostic(
            "Another process holds the settings profile lock."
        ))


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.

    One bounded cross-process lock per profile serializes the user-table reads
    in `load` against the atomic writes in `save`, and is always released
    before a caller's callbacks run.
    """

    def __init__(
        self,
        core,
        profile_path: str,
        resource_loader=resources.load_toml,
    ):
        """Loads base variables into the class."""
        self._resource_loader = resource_loader

        # User configs
        self.user_common_settings_path = os.path.join(
            profile_path, COMMON_SETTINGS_FILE)
        self.user_game_settings_path = os.path.join(
            profile_path, GAME_SETTINGS_FILE)
        self._profile_lock = filelock.FileLock(
            f"{profile_path}.lock",
            timeout=_PROFILE_LOCK_TIMEOUT_SECONDS,
            mode=0o600,
        )

        self.dynamic_widget_table = get_game_options()
        self.common_settings_table = get_common_options()

        self.settings_table = {}

        self.core = core

    @contextlib.contextmanager
    def _hold_profile_lock(self):
        """Hold the profile lock for the duration of one settings operation.

        Acquisition is bounded, so contention becomes the payload-free
        `SettingsLockError` instead of an unbounded wait. The `filelock`
        timeout is suppressed rather than chained: its own message names the
        lock file, which an uncaught traceback would otherwise print.
        """
        try:
            self._profile_lock.acquire()
        except filelock.Timeout:
            raise SettingsLockError from None
        try:
            yield
        finally:
            self._profile_lock.release()

    def load(self, callback_postgenload=None) -> list[str]:
        """Loads configuration."""
        default_common_settings_table = self._resource_loader(
            "settings", "defaults", "settings.toml"
        )
        default_game_settings_table = self._resource_loader(
            "settings", "defaults", "games.toml"
        )

        # One acquisition covers both reads, so a concurrent save cannot land
        # between them; it is released well before the callbacks below run.
        with self._hold_profile_lock():
            user_common_settings_table, common_warning = helpers.load_user_table(
                self.user_common_settings_path)
            user_game_settings_table, game_warning = helpers.load_user_table(
                self.user_game_settings_path)

        warnings = [
            warning
            for warning in (common_warning, game_warning)
            if warning is not None
        ]

        for group in self.common_settings_table:
            self.settings_table[group] = {}  # type: dict
            for option in self.common_settings_table[group]:
                value = default_common_settings_table[group][option]
                try:
                    value = user_common_settings_table[group][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.settings_table[group][option] = value

        for game in self.core.game_table.get_game_set():
            for option in self.core.game_table.get_game_settings(game):
                value = default_game_settings_table[game][option]
                try:
                    value = user_game_settings_table[game][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.core.game_table.set_game_setting(game, option, value)

        self.normalize_game_selection()

        if callback_postgenload is not None:
            for group in self.common_settings_table:
                for option in self.common_settings_table[group]:
                    callback_postgenload(
                        self.common_settings_table,
                        group,
                        option,
                        self.settings_table[group][option],
                    )

        return warnings

    def normalize_game_selection(self) -> None:
        """Replace invalid selectors with the first configured game."""
        games = self.core.game_table.get_game_set()
        first_game = ""
        if games:
            first_game = min(
                games,
                key=lambda game_id: (
                    self.core.game_table.get_game_info(game_id)["name"].casefold(),
                    game_id,
                ),
            )

        common_settings = self.settings_table["common"]
        for option in ("selected-game-browser", "selected-game-connect"):
            if common_settings.get(option) not in games:
                common_settings[option] = first_game

    def save(self) -> None:
        """Saves configuration."""
        persistent_common_settings = {
            group: {
                option: self.settings_table[group][option]
                for option in options
            }
            for group, options in self.common_settings_table.items()
        }
        # Compile game settings table
        user_game_settings_table = {}
        for game in self.core.game_table.get_game_set():
            user_game_settings_table[game] = self.core.game_table.get_game_settings(
                game)

        # Both atomic replacements happen under one acquisition, so a reader
        # never observes a half-updated profile.
        with self._hold_profile_lock():
            helpers.save_table(
                self.user_common_settings_path,
                persistent_common_settings,
            )
            helpers.save_table(self.user_game_settings_path,
                               user_game_settings_table)


def get_common_options():
    option_list = {
        'common': {
            'selected-game-connect': {'gtk_widget_name': "server-connect-game", 'gtk_widget_colnum': 0},
            'selected-game-browser': {'gtk_widget_name': "Game_TreeView", 'gtk_widget_colnum': 0},
            'server-host':   {'gtk_widget_name': "server-connect-host"},
            'game-listview': {'gtk_widget_name': "Game_View_ToggleButton"}
        }
    }

    return option_list


def get_game_options():
    option_list = {
        'path':         {
            'name':        i18n._("Game path"),
            'description': i18n._("Path to the game."),
            'gtk_type':    "Entry with Label"
        },

        'workdir':      {
            'name':        i18n._("Working directory"),
            'description': i18n._("Working directory of the game."),
            'gtk_type':    "Entry with Label"
        },

        'master_uri':   {
            'name':        i18n._("Master URI list"),
            'description': i18n._("List of master servers to query."),
            'gtk_type':    "Multiline Entry with Label"
        },

        'steam_launch': {
            'name':        i18n._("Launch via Steam"),
            'description': i18n._("Enables launching via Steam client. Required for VAC-enabled servers."),
            'gtk_type':    "CheckButton"
        },

        'steam_path':   {
            'name':        i18n._("Steam path"),
            'description': i18n._("Steam client path."),
            'gtk_type':    "Entry with Label"
        },
        'nickname':     {
            'name':        i18n._("Nickname"),
            'description': i18n._("Your nickname."),
            'gtk_type':    "Entry with Label"
        }
    }

    return option_list
