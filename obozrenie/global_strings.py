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

import obozrenie.i18n as i18n

PROJECT = i18n._("Obozrenie")
DESCRIPTION = i18n._("Game Server Browser")
WEBSITE = "http://obozrenie.vorotnikov.me"
GIT_WEBSITE = "https://github.com/obozrenie/obozrenie"
COPYRIGHT = i18n._("© 2015 Artem Vorotnikov")
AUTHORS = [i18n._("Artem Vorotnikov")]
ARTISTS = [i18n._("Artem Vorotnikov")]


SEPARATOR_MSG = ("-------------------------------------------")
ERROR_MSG = i18n._("\nThis is an error. Please file a bug report at ") + GIT_WEBSITE
CORECAT_MSG = i18n._("Core/")
UICAT_MSG = i18n._("UI/")
BACKENDCAT_MSG = i18n._("Backend/")

CORE_MSG = CORECAT_MSG + i18n._("Core:")
SETTINGS_MSG = CORECAT_MSG + i18n._("Settings:")
GTK_MSG = UICAT_MSG + i18n._("GTK+:")
HELPER_MSG = i18n._("Helpers:")
