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


import unittest
from obozrenie import helpers


def unit_dict_to_list():
    """Does helper convert dict table into a list table based on specified format correctly?"""
    spec_table = [{'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'d': 'D2', 'e': 'E2', 'a': 'A2'}]
    spec_format = ('a',
                   'e',
                   'd')
    spec_result = [['A1', 'E1', 'D1'], ['A2', 'E2', 'D2']]

    result = helpers.dict_to_list(spec_table, spec_format)

    return {'expectation': spec_result, 'result': result}


def unit_flatten_dict_table():
    """Does helper flatten dict based on specified format correctly?"""
    spec_table = {'lead1': {'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, 'lead2': {'a': 'A2', 'b': 'B2'}}
    leading_key_spec = 'leading key'
    spec_result = [{'leading key': 'lead1', 'a': 'A1', 'b': 'B1', 'c': 'C1', 'd': 'D1', 'e': 'E1'}, {'leading key': 'lead2', 'a': 'A2', 'b': 'B2'}]

    result = helpers.flatten_dict_table(spec_table, leading_key_spec=leading_key_spec)

    return {'expectation': spec_result, 'result': result}


def unit_sort_dict_table():
    """Checks how helper function sorts dictionary tables"""
    spec_table = [{'name': 'Olga', 'age': 32, 'gender': 'F'}, {'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}]
    spec_key = 'name'
    spec_result = [{'name': 'Marina', 'age': 25, 'gender': 'F'}, {'name': 'Nikolai', 'age': 40, 'gender': 'M'}, {'name': 'Olga', 'age': 32, 'gender': 'F'}]

    result = helpers.sort_dict_table(spec_table, spec_key)

    return {'expectation': spec_result, 'result': result}


class HelpersTests(unittest.TestCase):
    """Tests for helpers"""
    def test_dict_to_list(self):
        unit = unit_dict_to_list()
        self.assertTrue(unit['expectation'] == unit['result'])

    def test_flatten_dict_table(self):
        unit = unit_flatten_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])

    def test_sort_dict_table(self):
        unit = unit_sort_dict_table()
        self.assertTrue(unit['expectation'] == unit['result'])

if __name__ == "__main__":
    unittest.main()
