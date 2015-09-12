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


def get_common_options():
    option_list = {}

    option_list['common'] = {}

    option_list['common']['selected-game'] = {}
    option_list['common']['selected-game']['gtk_widget_name'] = "Game_TreeView"

    option_list['common']['server-host'] = {}
    option_list['common']['server-host']['gtk_widget_name'] = "ServerHost_Entry"

    option_list['common']['server-pass'] = {}
    option_list['common']['server-pass']['gtk_widget_name'] = "ServerPass_Entry"

    return option_list


def get_game_options():
    def _(msg): return msg
    option_list = {}

    option_list['path'] = {}
    option_list['path']['name'] = _("Game path")
    option_list['path']['description'] = _("Path to the game")
    option_list['path']['gtk_type'] = "Entry with Label"

    option_list['workdir'] = {}
    option_list['workdir']['name'] = _("Working directory")
    option_list['workdir']['description'] = _("Working directory of the game")
    option_list['workdir']['gtk_type'] = "Entry with Label"

    option_list['master_uri'] = {}
    option_list['master_uri']['name'] = _("Master URI list")
    option_list['master_uri']['description'] = _("List of master servers to query")
    option_list['master_uri']['gtk_type'] = "Entry with Label"

    del _
    return option_list
