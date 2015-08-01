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
        for i in range(len(table)):
            if table[i][key] == value:
                return i
        return None


def dict_to_list(dict_table, key_list):
        list_table = []

        for i in range(len(dict_table)):
            list_table.append([])

            for j in range(len(key_list)):
                list_table[i].append(dict_table[i][key_list[j]])

        return list_table
