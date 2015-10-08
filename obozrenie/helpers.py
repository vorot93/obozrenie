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

"""Helper functions for processing data."""

import collections
import copy
import json
import os
import pytoml
import threading

from obozrenie.global_settings import *
from obozrenie.global_strings import *


class Bunch:
    def __init__(self, **kwds):
        self.__dict__.update(kwds)


class ThreadSafeBunch(Bunch):
    def __init__(self, * p_arg, ** n_arg):
        super().__init__(* p_arg, ** n_arg)
        self.__lock = threading.RLock()

    def __enter__(self):
        self.__lock.acquire()
        return self

    def __exit__(self, type, value, traceback):
        self.__lock.release()


class ThreadSafeDict(dict):
    def __init__(self, * p_arg, ** n_arg):
        super().__init__(* p_arg, ** n_arg)
        self.__lock = threading.RLock()

    def __enter__(self):
        self.__lock.acquire()
        return self

    def __exit__(self, type, value, traceback):
        self.__lock.release()

    def __deepcopy__(self, *args):
        return ThreadSafeDict(json.loads(json.dumps(dict(self))))


class ThreadSafeList(list):
    def __init__(self, * p_arg, ** n_arg):
        super().__init__(* p_arg, ** n_arg)
        self.__lock = threading.RLock()

    def __enter__(self):
        self.__lock.acquire()
        return self

    def __exit__(self, type, value, traceback):
        self.__lock.release()

    def __deepcopy__(self, *args):
        return ThreadSafeList(json.loads(json.dumps(list(self))))


def enum(*args):
    return collections.namedtuple('Enum', args)._make(range(len(args)))


def deepcopy(foo):
    try:
        return foo.__deepcopy__()
    except AttributeError:
        return copy.deepcopy(foo)


def search_table(table, level, value):
        if level == 0:
            for i in range(len(table)):
                if table[i] == value:
                    return i
            return None
        elif level == 1:
            for i in range(len(table)):
                for j in range(len(table[i])):
                    if table[i][j] == value:
                        return i, j
            return None
        elif level == 2:
            for i in range(len(table)):
                for j in range(len(table[i])):
                    for k in range(len(table[i][j])):
                        if table[i][j][k] == value:
                            return i, j, k
            return None
        elif level is (3 or -1):
            for i in range(len(table)):
                for j in range(len(table[i])):
                    for k in range(len(table[i][j])):
                        for l in range(len(table[i][j][k])):
                            if table[i][j][k][l] == value:
                                return i, j, k, l
            return None
        else:
            print("Please specify correct search level: 0, 1, 2, 3, or -1 for deepest possible.")


def search_dict_table(table, key, value):
    result = None
    for i in range(len(table)):
        if table[i][key] == value:
            result = i
            break
    return result


def flatten_dict_table(dict_table, leading_key_spec):
    flattened_dict_table = []
    for leading_key in sorted(dict_table):
        flattened_dict_table.append({})

        flattened_dict_table[-1][leading_key_spec] = leading_key
        for key in dict_table[leading_key]:
            flattened_dict_table[-1][key] = dict_table[leading_key][key]

    return flattened_dict_table

def sort_dict_table(dict_table, sort_key):
    sorted_dict_list = sorted(dict_table, key=lambda k: k[sort_key])

    return sorted_dict_list

def dict_to_list(dict_table, key_list):
    list_table = []

    if dict_table is not None:
        for entry in dict_table:
            list_table.append([])

            for key in key_list:
                try:
                    list_table[-1].append(entry[key])
                except KeyError:
                    list_table[-1].append(None)

    return list_table


def flatten_list(nested_list):
    flattened_list = [item for sublist in nested_list for item in sublist]
    return flattened_list

def remove_all_occurences_from_list(target_list, value):
    return [y for y in target_list if y != value]


def load_table(path):
        """Loads settings table into dict"""
        try:
            table_open_object = open(path, 'r')
        except FileNotFoundError:
            return None
        table = pytoml.load(table_open_object)
        return table


def save_table(path, data):
    """Saves settings to a file"""
    try:
        table_open_object = open(path, 'w')
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(path))
        except OSError:
            pass
        table_open_object = open(path, 'x')
    pytoml.dump(table_open_object, data)
