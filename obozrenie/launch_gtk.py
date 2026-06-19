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
import sys

from obozrenie.global_settings import *


def _macos_library_dir():
    """Return the Homebrew lib dir holding the glib/gtk dylibs that the GObject-
    Introspection typelibs load by bare name, or None when not applicable.

    uv's standalone Python does not search Homebrew's prefix, so on macOS this
    directory must be on the dynamic loader path before any gi import."""
    if sys.platform != "darwin":
        return None
    for libdir in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.exists(os.path.join(libdir, "libgobject-2.0.0.dylib")):
            return libdir
    return None


def _ensure_macos_library_path():
    """On macOS, add Homebrew's lib dir to DYLD_FALLBACK_LIBRARY_PATH and
    re-exec once so dyld picks it up before gi loads the GTK typelibs."""
    libdir = _macos_library_dir()
    if libdir is None:
        return
    key = "DYLD_FALLBACK_LIBRARY_PATH"
    current = os.environ.get(key, "")
    if libdir in current.split(os.pathsep):
        return
    os.environ[key] = os.pathsep.join(filter(None, [current, libdir]))
    os.execv(sys.executable, [sys.executable, "-m", "obozrenie.launch_gtk", *sys.argv[1:]])


def main():
    _ensure_macos_library_path()

    from obozrenie.gtk import App, core

    try:
        core_instance = core.Core()
        settings_instance = core.Settings(
            core_instance, os.path.expanduser(PROFILE_PATH))
        app_instance = App(core_instance, settings_instance)
        app_instance.run(None)
    except Exception as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
