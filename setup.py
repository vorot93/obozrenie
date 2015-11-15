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
from distutils.core import setup
from babel.messages import frontend as babel

from obozrenie.global_settings import VERSION

data_files = []
data_dir = 'assets'

for directory, _, filenames in os.walk(data_dir):
    dest = directory[len(data_dir) + 1:]
    if filenames:
        files = []
        for filename in filenames:
            filename = os.path.join(directory, filename)
            files.append(filename)
        data_files.append((os.path.join('share', dest), files))

app_info = {'name': 'Obozrenie',
            'version': VERSION,
            'description': 'Simple and easy to use game server browser',
            'long_description': open('README.md').read(),
            'author': 'Artem Vorotnikov',
            'author_email': 'artem@vorotnikov.me',
            'url': 'https://github.com/obozrenie/obozrenie',
            'license': open('COPYING').read(),
            'install_requires': ['xmltodict', 'pyxdg', 'pytoml', 'beautifulsoup4', 'babel'],
            'packages': ['obozrenie', 'obozrenie.backends'],
            'package_dir': {'obozrenie': 'obozrenie', 'obozrenie.backends': 'obozrenie/backends'},
            'data_files': data_files,
            'cmdclass': {'compile_catalog':  babel.compile_catalog,
                         'extract_messages': babel.extract_messages,
                         'init_catalog':     babel.init_catalog,
                         'update_catalog':   babel.update_catalog},
            'classifiers': ['License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
                            'Topic :: Games/Entertainment',
                            'Topic :: Internet :: WWW/HTTP',
                            'Topic :: Utilities',
                            'Programming Language :: Python :: 3']
            }

setup(**app_info)
