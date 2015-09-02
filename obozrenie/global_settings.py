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

import os
from xdg import BaseDirectory


def N_(msg): return msg


def is_run_from_git_workdir():
    return os.path.exists('assets')

APPLICATION_ID = "io.obozrenie"
PROJECT = N_("Obozrenie")
DESCRIPTION = N_("Game Server Browser")
WEBSITE = "http://obozrenie.vorotnikov.me"
GIT_WEBSITE = "https://github.com/obozrenie/obozrenie"
VERSION = "0.1"
COPYRIGHT = N_("© 2015 Artem Vorotnikov")
AUTHORS = [N_("Artem Vorotnikov")]
ARTISTS = [N_("Artem Vorotnikov")]

SYSTEM_DATA_DIR = '/usr/share'

if is_run_from_git_workdir():
    ASSETS_DIR = 'assets'
else:
    ASSETS_DIR = SYSTEM_DATA_DIR

PROJECT_DIR = os.path.join(ASSETS_DIR, 'obozrenie')
LOCALE_DIR = os.path.join(ASSETS_DIR, 'locale')

ICON_DIR = os.path.join(ASSETS_DIR, 'pixmaps', 'obozrenie')
ICON_FLAGS_DIR = os.path.join(ICON_DIR, 'flags')
ICON_GAMES_DIR = os.path.join(ICON_DIR, 'games')

SETTINGS_DIR = os.path.join(PROJECT_DIR, 'settings')
SETTINGS_INTERNAL_DIR = os.path.join(SETTINGS_DIR, 'internal')
SETTINGS_INTERNAL_BACKENDS_DIR = os.path.join(SETTINGS_INTERNAL_DIR, 'backends')
SETTINGS_DEFAULTS_DIR = os.path.join(SETTINGS_DIR, 'defaults')

COMMON_SETTINGS_FILE = "settings.toml"
GAME_SETTINGS_FILE = "games.toml"

DEFAULT_COMMON_SETTINGS_PATH = os.path.join(SETTINGS_DEFAULTS_DIR, COMMON_SETTINGS_FILE)
DEFAULT_GAME_SETTINGS_PATH = os.path.join(SETTINGS_DEFAULTS_DIR, GAME_SETTINGS_FILE)

PROFILE_PATH = os.path.join(BaseDirectory.xdg_config_home, 'obozrenie')

GAME_CONFIG_FILE = os.path.join(SETTINGS_INTERNAL_DIR, "game_lists.toml")

GEOIP_DATA_FILE = os.path.join(SYSTEM_DATA_DIR, 'GeoIP', 'GeoIP.dat')

UI_DIR = os.path.join(PROJECT_DIR, "ui")
GTK_UI_FILE = os.path.join(UI_DIR, "obozrenie_gtk.ui")
GTK_APPMENU_FILE = os.path.join(UI_DIR, "obozrenie_gtk_appmenu.ui")

ERROR_MSG = N_("\nThis is an error. Please file a bug report at ") + GIT_WEBSITE

del N_
