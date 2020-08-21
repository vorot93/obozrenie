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

from obozrenie.global_settings import *

from obozrenie.gtk import *

try:
    core_instance = core.Core()
    settings_instance = core.Settings(
        core_instance, os.path.expanduser(PROFILE_PATH))
    app_instance = App(core_instance, settings_instance)
    app_instance.run(None)
except Exception as e:
    print(e)
