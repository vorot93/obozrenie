# Obozrenie

[![Gitter](https://badges.gitter.im/Join%20Chat.svg)](https://gitter.im/obozrenie/obozrenie?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge&utm_content=badge)

Simple and easy to use game server browser.

## Overview
Obozrenie is a game server browser that can be used to access information about and connect to multiplayer servers.

## Features
- Fast, fully threaded stat engine
- Easy to use, elegant, uncluttered GUI
- Supports various backends, designed for modularity.

## Screenshot
![](screenshot.png)

## Get it
    git clone https://github.com/obozrenie/obozrenie.git
    cd obozrenie
    ./obozrenie-gtk

## Technical details
Obozrenie is written in Python, with use of PyGObject libraries. It consists of several backends for master server interaction, core module and different GUIs.

* Backends query master servers and receive the server list.
* Core library forms multi-layered tables with game settings and server information (Game Table) and Obozrenie-wide configuration (Settings Table).
* GTK+ GUI shows this information in a user-friendly form.

## License
This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 3, as published by the Free Software Foundation.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
