#!/usr/bin/env python

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

setup(name='Obozrenie',
      version=VERSION,
      description='Simple and easy to use game server browser',
      author='Artem Vorotnikov',
      author_email='artem@vorotnikov.me',
      url='https://github.com/obozrenie',
      packages=['obozrenie', 'obozrenie.backends'],
      package_dir={'obozrenie': 'obozrenie', 'obozrenie.backends': 'obozrenie/backends'},
      data_files=data_files,
      cmdclass={'compile_catalog':  babel.compile_catalog,
                'extract_messages': babel.extract_messages,
                'init_catalog':     babel.init_catalog,
                'update_catalog':   babel.update_catalog}
      )
