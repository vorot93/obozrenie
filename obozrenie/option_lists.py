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


def get_common_options():
    option_list = {
                   'common': {
                              'selected-game-connect': {'gtk_widget_name': "server-connect-game", 'gtk_widget_colnum': 0},
                              'selected-game-browser': {'gtk_widget_name': "Game_TreeView", 'gtk_widget_colnum': 0},
                              'server-host':   {'gtk_widget_name': "server-connect-host"},
                              'server-pass':   {'gtk_widget_name': "server-connect-pass"},
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
