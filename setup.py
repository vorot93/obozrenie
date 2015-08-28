#!/usr/bin/env python

import os
from distutils.core import setup

data_files = []

for directory, _, filenames in os.walk('assets'):
    dest = directory[7:]
    if filenames:
        files = []
        for filename in filenames:
            filename = os.path.join(directory, filename)
            files.append(filename)
        data_files.append((os.path.join('share', dest), files))

setup(name='Obozrenie',
      version='0.1',
      description='Simple and easy to use game server browser',
      author='Artem Vorotnikov',
      author_email='artem@vorotnikov.me',
      url='https://github.com/obozrenie',
      packages=['obozrenie'],
      package_dir={'obozrenie': 'obozrenie'},
      data_files=data_files
      )
