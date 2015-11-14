![Obozrenie](https://cdn.rawgit.com/obozrenie/obozrenie/master/assets/icons/hicolor/scalable/apps/obozrenie.svg)

[![Build Status](https://travis-ci.org/obozrenie/obozrenie.svg)](https://travis-ci.org/obozrenie/obozrenie)
[![Climate](https://codeclimate.com/github/obozrenie/obozrenie/badges/gpa.svg)](https://codeclimate.com/github/obozrenie/obozrenie)
[![Coverage](https://codeclimate.com/github/obozrenie/obozrenie/badges/coverage.svg)](https://codeclimate.com/github/obozrenie/obozrenie)
[![codecov.io](https://codecov.io/github/obozrenie/obozrenie/coverage.svg?branch=master)](https://codecov.io/github/obozrenie/obozrenie?branch=master)
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

## Dependencies
### Required:
- [Python 3.2+](https://python.org)
- [PythonGI with GTK+ 3.10+ and higher](https://wiki.gnome.org/Projects/PyGObject)
- [BeautifulSoup4](http://crummy.com/software/BeautifulSoup)
- [PyXDG](http://freedesktop.org/Software/pyxdg)
- [python-pytoml](https://github.com/avakar/pytoml)
- [python-xmltodict](https://github.com/martinblech/xmltodict)
- [python-setuptools](http://pypi.python.org/pypi/setuptools)

### Optional:
- [QStat](https://github.com/multiplay/qstat) - for QStat backend support
- [Babel](http://babel.pocoo.org) - for translation support
- [PyGeoIP](https://github.com/appliedsec/pygeoip) with GeoIP database - for geolocation support

## Get it
    git clone https://github.com/obozrenie/obozrenie.git
    cd obozrenie
    ./obozrenie-gtk

## Technical details
Obozrenie is written in Python, with use of PyGObject libraries. It consists of several backends for master server interaction, core module and different GUIs.

* Backends query master servers and receive the server list.
* Core library forms multi-layered tables with game settings and server information (Game Table) and Obozrenie-wide configuration (Settings Table).
* GTK+ GUI shows this information in a user-friendly form.

## Licenses
### Obozrenie
This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License version 3, as published by the Free Software Foundation.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

### Obozrenie logo
This logo uses Neometric typeface by [Andres Sanchez](http://andresl.tumblr.com) distributed under CC-BY-SA-NC.
