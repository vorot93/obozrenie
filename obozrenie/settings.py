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
from types import MappingProxyType

import filelock

from obozrenie import helpers, resources
from obozrenie.diagnostics import SafeDiagnostic, SafeDiagnosticError
from obozrenie.global_settings import COMMON_SETTINGS_FILE, GAME_SETTINGS_FILE


_PROFILE_LOCK_TIMEOUT_SECONDS = 5.0

# The persisted common options, per settings group. The presenter's accessor
# table (`presenter.COMMON_OPTION_ACCESSORS`) maps each one to the view's
# reader and writer.
COMMON_OPTIONS = MappingProxyType({
    "common": (
        "selected-game-connect",
        "selected-game-browser",
        "server-host",
        "game-listview",
    ),
})


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
    before `load` returns.
    """

    def __init__(
        self,
        game_table,
        profile_path: str,
        resource_loader=resources.load_toml,
    ):
        """Loads base variables into the class."""
        self._resource_loader = resource_loader
        self.game_table = game_table

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

        self.settings_table = {}

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

    def load(self) -> list[str]:
        """Loads configuration."""
        default_common_settings_table = self._resource_loader(
            "settings", "defaults", "settings.toml"
        )
        default_game_settings_table = self._resource_loader(
            "settings", "defaults", "games.toml"
        )

        # One acquisition covers both reads, so a concurrent save cannot land
        # between them; it is released well before `load` returns.
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

        for group, options in COMMON_OPTIONS.items():
            self.settings_table[group] = {}  # type: dict
            for option in options:
                value = default_common_settings_table[group][option]
                try:
                    value = user_common_settings_table[group][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.settings_table[group][option] = value

        for game in self.game_table.get_game_set():
            for option in self.game_table.get_game_settings(game):
                value = default_game_settings_table[game][option]
                try:
                    value = user_game_settings_table[game][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.game_table.set_game_setting(game, option, value)

        self.normalize_game_selection()

        return warnings

    def normalize_game_selection(self) -> None:
        """Replace invalid selectors with the first configured game."""
        games = self.game_table.get_game_set()
        first_game = ""
        if games:
            first_game = min(
                games,
                key=lambda game_id: (
                    self.game_table.get_game_info(game_id)["name"].casefold(),
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
            for group, options in COMMON_OPTIONS.items()
        }
        # Compile game settings table
        user_game_settings_table = {}
        for game in self.game_table.get_game_set():
            user_game_settings_table[game] = self.game_table.get_game_settings(
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
