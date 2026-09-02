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

import importlib
import threading


_BUILTIN_ADAPTERS = frozenset({"minetest", "qstat", "rigsofrods"})
_REGISTRATION_LOCK = threading.Lock()
adapter_table = {}


class UnknownAdapterError(LookupError):
    def __init__(self, name):
        self.name = name
        super().__init__(f"unknown adapter: {name!r}")


def resolve_adapter(name):
    with _REGISTRATION_LOCK:
        try:
            return adapter_table[name]
        except KeyError:
            pass

    if name not in _BUILTIN_ADAPTERS:
        raise UnknownAdapterError(name)

    adapter = importlib.import_module(f"{__name__}.{name}")
    # A loader's same-thread recursive import returns its in-progress module.
    if getattr(getattr(adapter, "__spec__", None), "_initializing", False):
        return adapter

    with _REGISTRATION_LOCK:
        return adapter_table.setdefault(name, adapter)


def __getattr__(name):
    try:
        return adapter_table[name]
    except KeyError:
        pass
    if name not in _BUILTIN_ADAPTERS:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    return resolve_adapter(name)
