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

import os
import sys
from xdg import BaseDirectory


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_SOURCE_ROOT = os.path.dirname(_PACKAGE_DIR)


def _running_from_source():
    return os.path.isdir(os.path.join(_SOURCE_ROOT, "assets"))


APPLICATION_ID = "me.vorotnikov.Obozrenie"
VERSION = "1"

if _running_from_source():
    ASSETS_DIR = os.path.join(_SOURCE_ROOT, "assets")
    LOCALE_DIR = os.path.join(_SOURCE_ROOT, "locale")
else:
    ASSETS_DIR = os.path.join(sys.prefix, "share")
    LOCALE_DIR = os.path.join(sys.prefix, "share", "locale")

PROJECT_DIR = os.path.join(ASSETS_DIR, "obozrenie")

ICON_NAME = "obozrenie-short"

ICON_DIR = os.path.join(ASSETS_DIR, "pixmaps", "obozrenie")
ICON_GAMES_DIR = os.path.join(ICON_DIR, "games")

if _running_from_source():
    ICON_PATH = os.path.join(
        ASSETS_DIR, "icons", "hicolor", "scalable", "apps", ICON_NAME + ".svg")
    ICON_NAME = None

SETTINGS_DIR = os.path.join(PROJECT_DIR, 'settings')
SETTINGS_INTERNAL_DIR = os.path.join(SETTINGS_DIR, 'internal')
SETTINGS_INTERNAL_BACKENDS_DIR = os.path.join(
    SETTINGS_INTERNAL_DIR, 'backends')
SETTINGS_DEFAULTS_DIR = os.path.join(SETTINGS_DIR, 'defaults')

COMMON_SETTINGS_FILE = "settings.toml"
GAME_SETTINGS_FILE = "games.toml"

DEFAULT_COMMON_SETTINGS_PATH = os.path.join(
    SETTINGS_DEFAULTS_DIR, COMMON_SETTINGS_FILE)
DEFAULT_GAME_SETTINGS_PATH = os.path.join(
    SETTINGS_DEFAULTS_DIR, GAME_SETTINGS_FILE)

PROFILE_PATH = os.path.join(BaseDirectory.xdg_config_home, 'obozrenie')

GAME_CONFIG_FILE = os.path.join(SETTINGS_INTERNAL_DIR, "game_lists.toml")

UI_DIR = os.path.join(PROJECT_DIR, "ui")
GTK_UI_FILE = os.path.join(UI_DIR, "obozrenie_gtk.ui")
GTK_APPMENU_FILE = os.path.join(UI_DIR, "obozrenie_gtk_appmenu.ui")
