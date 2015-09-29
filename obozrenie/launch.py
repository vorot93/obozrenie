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
import subprocess

from obozrenie.global_settings import *
from obozrenie.global_strings import *


def launch_game(game, launch_pattern, game_settings, host, port, password, steam_app_id=None):
    """Launches the game based on specified launch pattern"""
    try:
        launch_cmd = []
        env_dict = dict(os.environ)

        path = os.path.expanduser(game_settings["path"])

        # Pre-launch
        if launch_pattern == "steam":
            steam_path = game_settings["steam_path"]
            launch_cmd = [steam_path, "-applaunch", steam_app_id, "+connect", host + ":" + port]
            if password != '':
                launch_cmd.append("+password", password)

        elif launch_pattern == "rigsofrods":
            config_file = os.path.expanduser("~/.rigsofrods/config/RoR.cfg")
            launch_cmd = [path]

            if os.path.exists(config_file):
                subprocess.call(["sed", "-i", "s/Network enable.*/Network enable=Yes/", config_file])
                subprocess.call(["sed", "-i", "s/Server name.*/Server name=" + host + "/", config_file])
                subprocess.call(["sed", "-i", "s/Server port.*/Server port=" + port + "/", config_file])
                subprocess.call(["sed", "-i", "s/Server password.*/Server password=" + password + "/", config_file])
            else:
                if os.path.exists(os.path.dirname(config_file)) is False:
                    os.makedirs(os.path.dirname(config_file))
                with open(config_file, "x") as f:
                    f.write("Network enable=Yes" + "\n")
                    f.write("Server name=" + host + "\n")
                    f.write("Server port=" + port + "\n")
                    f.write("Server password=" + password + "\n")
                    f.close()

        elif launch_pattern == "quake":
            launch_cmd = [path, "+connect", host + ":" + port]
            if password != '':
                launch_cmd.append("+password", password)

        elif launch_pattern == "hl2":
            workdir = os.path.dirname(path)
            try:
                if game_settings["workdir"]:
                    workdir = os.path.expanduser(game_settings["workdir"])
            except KeyError:
                pass
            env_dict['PWD'] = workdir
            ld_dir = "./bin"
            try:
                env_dict['LD_LIBRARY_PATH'] = ":".join([ld_dir, env_dict['LD_LIBRARY_PATH']])
            except KeyError:
                env_dict['LD_LIBRARY_PATH'] = ld_dir

            launch_cmd = [path, "-game", game, "+connect", host + ":" + port]
            if password != '':
                launch_cmd.append("+password", password)

        elif launch_pattern == "openttd":
            launch_cmd = [path, "-n", host + ":" + port]

        elif launch_pattern == "minetest":
            launch_cmd = [path, "--address", host, "--port", port]
            if password != '':
                launch_cmd.append("--password", password)

        # Launch
        print(LAUNCHER_MSG, "Launching '%(launch_cmd)s'" % {'launch_cmd': " ".join(launch_cmd)})
        call_exit_code = subprocess.call(launch_cmd, cwd=env_dict['PWD'], env=env_dict)

        # Post-launch
        if launch_pattern == "rigsofrods":
            subprocess.call(["sed", "-i", "s/Network enable.*/Network enable=No/", config_file])
        return call_exit_code
    except OSError as e:
        print(e)
        return e
