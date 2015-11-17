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

import obozrenie.helpers as helpers

from obozrenie.global_settings import *
from obozrenie.global_strings import *

from . import rigsofrods
from . import qstat
from . import minetest

adapter_table = {}

adapter_list = ('rigsofrods', 'qstat', 'minetest')

for adapter in adapter_list:
    adapter_table[adapter] = globals()[adapter]

helpers.debug_msg([CORE_MSG, i18n._("%(adapter_num)i adapters loaded successfully") % {'adapter_num': len(adapter_list)}])
