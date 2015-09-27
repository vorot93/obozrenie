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
LAUNCHER_MSG = i18n._("Launcher:")


GTK_STRING_TABLE = {"main-window":                      {"title": PROJECT},
                    "game-combobox":                    {"tooltip-text": i18n._("Select a game.")},
                    "game-treeview-column":             {"title": i18n._("Available games")},
                    "game-view-togglebutton":           {"tooltip-text": i18n._("Toggle game list view mode.")},
                    "game-preferences-button":          {"tooltip-text": i18n._("Configure the selected game.")},
                    "action-update-button":             {"tooltip-text": i18n._("Update server list.")},
                    "action-info-button":               {"tooltip-text": i18n._("Show server information.")},
                    "action-connect-button":            {"tooltip-text": i18n._("Connect to the selected server.")},
                    "filters-button":                   {"tooltip-text": i18n._("Set up server filters.")},
                    "filter-mod-label":                 {"label": i18n._("Game Mod:")},
                    "filter-type-label":                {"label": i18n._("Game Type:")},
                    "filter-terrain-label" :            {"label": i18n._("Terrain:")},
                    "filter-ping-label" :               {"label": i18n._("Max ping:")},
                    "filter-secure-label":              {"label": i18n._("Secure:")},
                    "filter-notfull":                   {"label": i18n._("Not full")},
                    "filter-notempty":                  {"label": i18n._("Has players")},
                    "filter-nopassword":                {"label": i18n._("No password")},
                    "serverlist-view-name-column":      {"title": i18n._("Name")},
                    "serverlist-view-host-column":      {"title": i18n._("Host")},
                    "serverlist-view-ping-column":      {"title": i18n._("Ping")},
                    "serverlist-view-players-column":   {"title": i18n._("Players")},
                    "serverlist-view-game_mod-column":  {"title": i18n._("Game Mod")},
                    "serverlist-view-game_type-column": {"title": i18n._("Game Type")},
                    "serverlist-view-terrain-column":   {"title": i18n._("Terrain")},
                    "serverlist-welcome-label":         {"label": i18n._("Welcome to Obozrenie!\nSelect a game or press refresh button below.")},
                    "error-message-label":              {"label": i18n._("Oops. Something went wrong.")},
                    "server-connect-host":              {"placeholder-text": i18n._("Host address"),
                                                         "tooltip-text":     i18n._("Server address to connect to. Automatically filled on changing server list selection.")},
                    "server-connect-pass":              {"placeholder-text": i18n._("Password"),
                                                         "tooltip-text":     i18n._("The server password, if required.")},
                    "serverinfo-dialog":                {"title": i18n._("Server information")},
                    "serverinfo-name-label":            {"label": i18n._("Name:")},
                    "serverinfo-host-label":            {"label": i18n._("Host:")},
                    "serverinfo-game-label":            {"label": i18n._("Game:")},
                    "serverinfo-terrain-label":         {"label": i18n._("Terrain:")},
                    "serverinfo-players-label":         {"label": i18n._("Players:")},
                    "serverinfo-ping-label":            {"label": i18n._("Ping:")},
                    "serverinfo-connect-button":        {"label": i18n._("Connect")},
                    "serverinfo-close-button":          {"label": i18n._("Close")},
                    "serverinfo-players-name-column":   {"title": i18n._("Name")},
                    "serverinfo-players-score-column":  {"title": i18n._("Score")},
                    "serverinfo-players-ping-column":   {"title": i18n._("Ping")}
                    }
