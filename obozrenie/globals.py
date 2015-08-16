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

import inspect
import os
from xdg import BaseDirectory


def is_run_from_git_workdir():
    return os.path.exists('assets')

APPLICATION_ID = "io.obozrenie"
PROJECT = "Obozrenie"
DESCRIPTION = "Game Server Browser"
WEBSITE = "http://obozrenie.vorotnikov.me"
VERSION = "0.1"
COPYRIGHT = "© 2015 Artem Vorotnikov"
AUTHORS = ["Artem Vorotnikov"]
ARTISTS = ["Artem Vorotnikov"]

SYSTEM_DATA_DIR = '/usr/share'
SYSTEM_PROJECT_DIR = os.path.join(SYSTEM_DATA_DIR, 'obozrenie')

if is_run_from_git_workdir():
    ASSETS_DIR = 'assets'
    LOCALE_DIR = 'locale'
else:
    ASSETS_DIR = os.path.join(SYSTEM_PROJECT_DIR, 'assets')
    LOCALE_DIR = os.path.join(SYSTEM_DATA_DIR, 'locale')

ICON_DIR = os.path.join(ASSETS_DIR, 'icons')
ICON_FLAGS_DIR = os.path.join(ICON_DIR, 'flags')
ICON_GAMES_DIR = os.path.join(ICON_DIR, 'games')

SETTINGS_DIR = os.path.join(ASSETS_DIR, 'settings')
SETTINGS_INTERNAL_DIR = os.path.join(SETTINGS_DIR, 'internal')
SETTINGS_INTERNAL_BACKENDS_DIR = os.path.join(SETTINGS_INTERNAL_DIR, 'backends')
SETTINGS_DEFAULTS_DIR = os.path.join(SETTINGS_DIR, 'defaults')

PROFILE_PATH = os.path.join(BaseDirectory.xdg_config_home, 'obozrenie')

GAME_CONFIG_FILE = os.path.join(SETTINGS_INTERNAL_DIR, "game_lists.toml")

GEOIP_DATA_FILE = os.path.join(SYSTEM_DATA_DIR, 'GeoIP', 'GeoIP.dat')

GTK_UI_FILE = os.path.join(ASSETS_DIR, "obozrenie_gtk.ui")
GTK_APPMENU_FILE = os.path.join(ASSETS_DIR, "obozrenie_gtk_appmenu.ui")
