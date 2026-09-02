import contextlib
import copy
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

import requests
import xmltodict

from obozrenie import adapters, helpers, http_client, network_calls, ping, qstat_processes
from obozrenie.adapters import minetest, qstat, rigsofrods
from obozrenie.adapters.http_masters import AllMastersFailedError
from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    diagnostic_for_exception,
)
from obozrenie.models import Endpoint, Player, Server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _capture_value_or_error(callback):
    try:
        return True, callback()
    except BaseException as error:
        return False, error


@contextlib.contextmanager
def installed_network_pools():
    fresh = network_calls.NetworkCallPools(
        dns=network_calls.BoundedDaemonPool(4, "obozrenie-dns-resolver"),
        http=network_calls.BoundedDaemonPool(4, "obozrenie-http"),
        icmp=network_calls.BoundedDaemonPool(32, "obozrenie-icmp"),
    )
    patcher = mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", fresh)
    patcher.start()
    try:
        yield fresh
    finally:
        try:
            network_calls.close_network_call_pools(timeout=0.2)
        finally:
            patcher.stop()


def _streamed_response(body, *, encoding=None):
    """Build one terminal response the HTTP boundary can stream and close."""
    response = requests.Response()
    response.status_code = 200
    response.reason = "OK"
    response.url = "https://example.invalid/list"
    response.headers["Content-Type"] = "application/json"
    if encoding is not None:
        response.headers["Content-Type"] += f"; charset={encoding}"
    response.encoding = encoding
    response.raw = io.BytesIO(body)
    return response


# Every body a single master can send that makes the decode or parse phase,
# rather than the transport, refuse the response. Sizes stay far under every
# configured limit, so no size guard can isolate them instead.
HOSTILE_DECODE_BODIES = (
    # `punycode` raises the bare `UnicodeError` before CPython 3.14 and the
    # `UnicodeDecodeError` subclass from 3.14 on; both belong to the phase.
    ("undecodable charset", b"$", "punycode"),
    ("digit-limited integer", b"[" + b"9" * 5000 + b"]", None),
    ("deeply nested array", b"[" * 200000, None),
)


class _ScriptedSession:
    """Serve one scripted response per URL and record what was requested."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **_kwargs):
        self.requested.append(url)
        return self.responses[url]


CURRENT_MINETEST_ENTRY = {
    "clients": 0,
    "clients_list": [],
    "clients_max": 50,
    "creative": False,
    "damage": True,
    "description": (
        "Welcome to kattttt's Kingdom ; Build anything you can imagine, "
        "from massive projects to detailed creations. Become true engineers "
        "with Techage, which will animate your factories ! Every block matters, "
        "join us and bring your ideas to life (the admin speak french, and "
        "english too ;) enjoy and have fun ^^ discord : "
        "https://discord.gg/RKAPMpC6nd - website : "
        "https://kattttt.ddns.net/dokuwiki/doku.php/start"
    ),
    "game_time": 23427379,
    "gameid": "minetest",
    "lag": 0.09034024924039841,
    "name": "kattttt's kingdom",
    "password": False,
    "proto_max": 52,
    "proto_min": 37,
    "pvp": False,
    "uptime": 39605,
    "url": "https://kattttt.ddns.net/dokuwiki/doku.php/start",
    "version": "5.17.0-dev",
    "clients_top": 0,
    "geo_continent": "EU",
    "ping": 0.02442,
    "address": "kattttt.ddns.net",
    "port": 31337,
    "pop_v": 0,
}

CURRENT_FREEMINER_ENTRY = {
    "mapgen": "indev",
    "url": "https://forum.minetest.net/viewtopic.php?t=7068",
    "creative": False,
    "liquid_real": True,
    "clients": "1",
    "clients_top": "4",
    "map_size": "32",
    "uptime": "250186",
    "mods": [
        "volumetric_lighting",
        "tbm",
        "spring",
        "sfinv",
        "mtg_craftguide",
        "creative",
        "sethome",
        "screwdriver",
        "player_api",
        "default",
        "weather",
        "walls",
        "unified_inventory",
        "stairs",
        "sponge",
        "spawn",
        "pbj_pup",
        "keys",
        "give_initial_stuff",
        "freeminer",
        "flowers",
        "butterflies",
        "fire",
        "tnt",
        "env_sounds",
        "dungeon_loot",
        "vessels",
        "fireflies",
        "carts",
        "bucket",
        "doors",
        "xpanes",
        "bones",
        "heads",
        "boats",
        "binoculars",
        "mesecons",
        "mobs",
        "mobs_npc",
        "mobs_monster",
        "mob_horse",
        "mobs_turtles",
        "mobs_sharks",
        "mobs_jellyfish",
        "mobs_fish",
        "mobs_crocs",
        "mobs_butterfly",
        "mobs_birds",
        "mobs_bat",
        "boost_cart",
        "mesecons_wires",
        "mesecons_torch",
        "mesecons_switch",
        "mesecons_receiver",
        "mesecons_walllever",
        "mesecons_button",
        "mesecons_random",
        "mesecons_pressureplates",
        "mesecons_powerplant",
        "mesecons_noteblock",
        "mesecons_mvps",
        "protector",
        "mesecons_stickyblocks",
        "mesecons_pistons",
        "mesecons_microcontroller",
        "mesecons_materials",
        "mesecons_solarpanel",
        "mesecons_movestones",
        "mesecons_detector",
        "mesecons_luacontroller",
        "mesecons_lamp",
        "mesecons_insulated",
        "mesecons_hydroturbine",
        "mesecons_fpga",
        "mesecons_extrawires",
        "mesecons_doors",
        "mesecons_delayer",
        "mesecons_gates",
        "mesecons_commandblock",
        "mesecons_blinkyplant",
        "mesecons_alias",
        "intllib",
        "3d_armor",
        "wieldview",
        "shields",
        "3d_armor_ui",
        "3d_armor_stand",
        "3d_armor_sfinv",
        "3d_armor_ip",
        "game_commands",
        "fakelib",
        "dye",
        "wool",
        "farming",
        "xcompat",
        "travelnet",
        "basic_materials",
        "pipeworks",
        "digtron",
        "elevator",
        "mobs_animal",
        "beds",
        "handle_schematics",
        "ufos",
        "map",
        "mesecons_lightstone",
        "areas",
        "hangglider",
    ],
    "1": "",
    "clients_list": ["growbot"],
    "address": "fm.setun.net",
    "ip": "2a02:a466:8baa:0:216:3eff:fe00:c211",
    "clients_max": "199",
    "proto_max": "148",
    "lag": 0.397509217262268,
    "pop_n": 15844,
    "version": "5.17.0.0",
    "privs": "interact, shout, fast",
    "pvp": True,
    "password": False,
    "gameid": "sky",
    "pop_c": 15357,
    "key": "2a02:a466:8baa:0:216:3eff:fe00:c211:30000",
    "game_time": "420634750",
    "proto": "mt",
    "name": "fm: Sky: Yggdrasil tree growing",
    "port": "30000",
    "first": 1783165107,
    "proto_multi": {
        "mt": 30000,
        "enet": 30200,
        "sctp": 30100,
        "wss": 30000,
    },
    "time": 1788111623,
    "start": 1787861444,
    "description": "3D. Very hard. Weather, real liquid.",
    "dedicated": True,
    "damage": True,
    "ping": 0,
    "pop_v": 0.969262812421106,
    "proto_min": "37",
}

CURRENT_RIGSOFRODS_IPV4_ENTRY = {
    "has-password": True,
    "current-users": 1,
    "max-clients": 8,
    "verified": 1,
    "is-official": 0,
    "ip": "71.239.149.231",
    "port": 12001,
    "version": "RoRnet_2.45",
    "terrain-name": "any",
    "name": "Kipp County | CDL+",
    "json-userlist": [
        {
            "client_id": 1,
            "is_admin": 1,
            "is_bot": 8,
            "is_mod": 0,
            "is_ranked": 0,
            "username": "Kipp Bot",
        }
    ],
}

CURRENT_RIGSOFRODS_IPV6_ENTRY = {
    "has-password": False,
    "current-users": 2,
    "max-clients": 24,
    "verified": 0,
    "is-official": 1,
    "ip": "2001:db8::20",
    "port": 12000,
    "version": "RoRnet_2.45",
    "terrain-name": "Desert",
    "name": "RoR IPv6",
    "json-userlist": [
        {
            "client_id": 41,
            "is_admin": 0,
            "is_bot": 0,
            "is_mod": 1,
            "is_ranked": 2,
            "username": "Ada",
        },
        {
            "client_id": 42,
            "is_admin": 0,
            "is_bot": 0,
            "is_mod": 0,
            "is_ranked": 0,
            "username": "Lin",
        },
    ],
}


DIRECT_IMPORT_RACE_SCRIPT = r'''
import importlib
import importlib.abc
import importlib.util
import sys
import threading

import obozrenie.adapters as adapters

qstat_name = "obozrenie.adapters.qstat"
minetest_name = "obozrenie.adapters.minetest"
real_import_module = importlib.import_module
loader_entered = threading.Event()
resolver_import_entered = threading.Event()
resolver_thread_ids = []
nested_results = []

class QStatLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        loader_entered.set()
        assert resolver_import_entered.wait(2)
        if mode == "same-name":
            nested = getattr(adapters, "qstat")
            assert nested is module
            assert "qstat" not in adapters.adapter_table
        else:
            nested_results.append(adapters.resolve_adapter("minetest"))
        module.ready = True

class MinetestLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        module.ready = True

class AdapterFinder(importlib.abc.MetaPathFinder):
    def __init__(self, qstat_loader, minetest_loader):
        self.qstat_loader = qstat_loader
        self.minetest_loader = minetest_loader

    def find_spec(self, fullname, path=None, target=None):
        if fullname == qstat_name:
            return importlib.util.spec_from_loader(fullname, self.qstat_loader)
        if fullname == minetest_name:
            return importlib.util.spec_from_loader(fullname, self.minetest_loader)
        return None

qstat_loader = QStatLoader()
minetest_loader = MinetestLoader()
sys.meta_path.insert(0, AdapterFinder(qstat_loader, minetest_loader))
direct_results = []
resolver_results = []
errors = []

def direct_import():
    try:
        direct_results.append(real_import_module(qstat_name))
    except BaseException as error:
        errors.append(error)

direct_thread = threading.Thread(target=direct_import, daemon=True)
direct_thread.start()
assert loader_entered.wait(2)

def observed_import_module(name, package=None):
    if (
        name == qstat_name
        and threading.get_ident() == resolver_thread_ids[0]
    ):
        resolver_import_entered.set()
    return real_import_module(name, package)

adapters.importlib.import_module = observed_import_module

def resolve_qstat():
    resolver_thread_ids.append(threading.get_ident())
    try:
        resolver_results.append(adapters.resolve_adapter("qstat"))
    except BaseException as error:
        errors.append(error)

resolver_thread = threading.Thread(target=resolve_qstat, daemon=True)
resolver_thread.start()
assert resolver_import_entered.wait(2)
direct_thread.join(1)
resolver_thread.join(1)
assert not direct_thread.is_alive(), "direct importer remained blocked"
assert not resolver_thread.is_alive(), "resolver remained blocked"
assert errors == []
assert qstat_loader.calls == 1
assert len(direct_results) == 1
assert resolver_results == direct_results
qstat = direct_results[0]
assert qstat.ready is True
assert adapters.adapter_table["qstat"] is qstat
assert sys.modules[qstat_name] is qstat
assert vars(adapters)["qstat"] is qstat

if mode == "same-name":
    assert nested_results == []
    assert minetest_loader.calls == 0
    assert "minetest" not in adapters.adapter_table
else:
    assert len(nested_results) == 1
    minetest = nested_results[0]
    assert minetest.ready is True
    assert minetest_loader.calls == 1
    assert adapters.adapter_table["minetest"] is minetest
    assert sys.modules[minetest_name] is minetest
    assert vars(adapters)["minetest"] is minetest

print("DIRECT_IMPORT_RACE_OK:" + mode)
'''


class AdapterResolverTests(unittest.TestCase):
    def run_clean_script(self, script, *, timeout=30):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def assert_clean_script_succeeds(self, script, *, timeout=30):
        try:
            result = self.run_clean_script(script, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.fail(
                f"clean subprocess timed out after {timeout} seconds\n"
                f"stdout:\n{error.stdout or ''}\nstderr:\n{error.stderr or ''}"
            )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def preserve_table_entry(self, name):
        missing = object()
        previous = adapters.adapter_table.get(name, missing)
        if previous is missing:
            self.addCleanup(adapters.adapter_table.pop, name, None)
        else:
            self.addCleanup(
                adapters.adapter_table.__setitem__,
                name,
                previous,
            )
        return previous, missing

    def test_clean_builtin_resolution_registers_once_without_eager_qstat(self):
        result = self.run_clean_script(r'''
import json
import sys

import obozrenie.adapters as adapters

assert "obozrenie.adapters.minetest" not in sys.modules
assert "obozrenie.adapters.qstat" not in sys.modules
resolved = adapters.resolve_adapter("minetest")
assert resolved is adapters.adapter_table["minetest"]
assert adapters.resolve_adapter("minetest") is resolved
assert "obozrenie.adapters.qstat" not in sys.modules
print(json.dumps(sorted(adapters.adapter_table)))
''')
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertEqual(result.stdout.splitlines(), ['["minetest"]'])

    def test_direct_submodule_import_is_registered_by_resolution(self):
        result = self.run_clean_script(r'''
import importlib

import obozrenie.adapters as adapters

direct = importlib.import_module("obozrenie.adapters.rigsofrods")
assert "rigsofrods" not in adapters.adapter_table
resolved = adapters.resolve_adapter("rigsofrods")
assert resolved is direct
assert adapters.adapter_table["rigsofrods"] is direct
''')
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_injected_adapter_precedes_builtin_import(self):
        self.preserve_table_entry("qstat")
        injected = SimpleNamespace(stat_master=lambda *_args: [])
        adapters.adapter_table["qstat"] = injected

        with mock.patch.object(
            adapters.importlib,
            "import_module",
            side_effect=AssertionError("injected adapter triggered import"),
        ):
            resolved = adapters.resolve_adapter("qstat")

        self.assertIs(resolved, injected)

    def test_unknown_adapter_has_explicit_and_attribute_error_contracts(self):
        name = "not-a-registered-adapter"
        self.preserve_table_entry(name)
        adapters.adapter_table.pop(name, None)

        with self.assertRaises(adapters.UnknownAdapterError) as raised:
            adapters.resolve_adapter(name)
        self.assertEqual(raised.exception.name, name)
        with self.assertRaises(AttributeError):
            getattr(adapters, name)

    def test_builtin_import_failure_propagates_without_registration(self):
        self.preserve_table_entry("qstat")
        adapters.adapter_table.pop("qstat", None)
        failure = ImportError("broken qstat import")

        with (
            mock.patch.object(
                adapters.importlib,
                "import_module",
                side_effect=failure,
            ),
            self.assertRaises(ImportError) as raised,
        ):
            adapters.resolve_adapter("qstat")

        self.assertIs(raised.exception, failure)
        self.assertNotIn("qstat", adapters.adapter_table)

    def test_concurrent_first_resolution_executes_real_loader_once(self):
        result = self.assert_clean_script_succeeds(r'''
import importlib.abc
import importlib.util
import sys
import threading

import obozrenie.adapters as adapters

entered = threading.Event()
release = threading.Event()
gate = threading.Barrier(9)

class BlockingLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        entered.set()
        assert release.wait(2)
        module.ready = True

class QStatFinder(importlib.abc.MetaPathFinder):
    def __init__(self, loader):
        self.loader = loader

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "obozrenie.adapters.qstat":
            return importlib.util.spec_from_loader(fullname, self.loader)
        return None

loader = BlockingLoader()
sys.meta_path.insert(0, QStatFinder(loader))
results = []
errors = []

def resolve():
    try:
        gate.wait(timeout=2)
        results.append(adapters.resolve_adapter("qstat"))
    except BaseException as error:
        errors.append(error)

threads = [threading.Thread(target=resolve) for _ in range(8)]
for thread in threads:
    thread.start()
try:
    gate.wait(timeout=2)
    assert entered.wait(1)
finally:
    release.set()
for thread in threads:
    thread.join(2)

assert all(not thread.is_alive() for thread in threads)
assert errors == []
assert loader.calls == 1
assert len(results) == 8
assert all(result is results[0] for result in results)
assert results[0].ready is True
assert adapters.adapter_table["qstat"] is results[0]
assert sys.modules["obozrenie.adapters.qstat"] is results[0]
print("REAL_CONCURRENT_IMPORT_OK")
''')
        self.assertEqual(
            result.stdout.splitlines(),
            ["REAL_CONCURRENT_IMPORT_OK"],
        )

    def test_direct_import_racing_resolution_allows_same_name_reentry(self):
        result = self.assert_clean_script_succeeds(
            'mode = "same-name"\n' + DIRECT_IMPORT_RACE_SCRIPT,
            timeout=5,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["DIRECT_IMPORT_RACE_OK:same-name"],
        )

    def test_direct_import_racing_resolution_allows_other_adapter_resolution(self):
        result = self.assert_clean_script_succeeds(
            'mode = "other-adapter"\n' + DIRECT_IMPORT_RACE_SCRIPT,
            timeout=5,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            ["DIRECT_IMPORT_RACE_OK:other-adapter"],
        )

    def test_real_loader_same_thread_reentry_avoids_partial_registration(self):
        result = self.assert_clean_script_succeeds(r'''
import importlib.abc
import importlib.util
import sys

import obozrenie.adapters as adapters

class ReentrantLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        reentered = getattr(adapters, "qstat")
        assert reentered is module
        assert "qstat" not in adapters.adapter_table
        module.ready = True

class QStatFinder(importlib.abc.MetaPathFinder):
    def __init__(self, loader):
        self.loader = loader

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "obozrenie.adapters.qstat":
            return importlib.util.spec_from_loader(fullname, self.loader)
        return None

loader = ReentrantLoader()
sys.meta_path.insert(0, QStatFinder(loader))
resolved = adapters.resolve_adapter("qstat")
assert resolved.ready is True
assert loader.calls == 1
assert adapters.adapter_table["qstat"] is resolved
assert sys.modules["obozrenie.adapters.qstat"] is resolved
assert vars(adapters)["qstat"] is resolved
print("REAL_REENTRY_OK")
''', timeout=5)
        self.assertEqual(result.stdout.splitlines(), ["REAL_REENTRY_OK"])

    def test_package_attribute_preserves_known_loader_unknown_error(self):
        result = self.assert_clean_script_succeeds(r'''
import importlib.abc
import importlib.util
import sys

import obozrenie.adapters as adapters

sentinel = adapters.UnknownAdapterError("loader-sentinel")

class FailingLoader(importlib.abc.Loader):
    def exec_module(self, module):
        raise sentinel

class QStatFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "obozrenie.adapters.qstat":
            return importlib.util.spec_from_loader(fullname, FailingLoader())
        return None

sys.meta_path.insert(0, QStatFinder())
try:
    getattr(adapters, "qstat")
except BaseException as error:
    assert error is sentinel, (type(error), error)
else:
    raise AssertionError("known loader failure did not propagate")
assert "qstat" not in adapters.adapter_table
assert "obozrenie.adapters.qstat" not in sys.modules
assert "qstat" not in vars(adapters)
print("KNOWN_LOADER_ERROR_OK")
''')
        self.assertEqual(result.stdout.splitlines(), ["KNOWN_LOADER_ERROR_OK"])

    def test_real_loader_in_flight_injection_wins_registration_and_return(self):
        result = self.assert_clean_script_succeeds(r'''
import importlib.abc
import importlib.util
import sys
import threading

import obozrenie.adapters as adapters

entered = threading.Event()
release = threading.Event()

class BlockingLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        entered.set()
        assert release.wait(2)
        module.ready = True

class QStatFinder(importlib.abc.MetaPathFinder):
    def __init__(self, loader):
        self.loader = loader

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "obozrenie.adapters.qstat":
            return importlib.util.spec_from_loader(fullname, self.loader)
        return None

loader = BlockingLoader()
sys.meta_path.insert(0, QStatFinder(loader))
results = []
errors = []

def resolve():
    try:
        results.append(adapters.resolve_adapter("qstat"))
    except BaseException as error:
        errors.append(error)

thread = threading.Thread(target=resolve)
thread.start()
assert entered.wait(2)
injected = object()
adapters.adapter_table["qstat"] = injected
release.set()
thread.join(2)
assert not thread.is_alive()
assert errors == []
assert results == [injected]
assert adapters.adapter_table["qstat"] is injected
loaded = sys.modules["obozrenie.adapters.qstat"]
assert loaded.ready is True
assert vars(adapters)["qstat"] is loaded
assert loader.calls == 1
print("IN_FLIGHT_INJECTION_OK")
''')
        self.assertEqual(result.stdout.splitlines(), ["IN_FLIGHT_INJECTION_OK"])

    def test_real_loader_failure_cleans_state_and_retries_successfully(self):
        result = self.assert_clean_script_succeeds(r'''
import importlib.abc
import importlib.util
import sys

import obozrenie.adapters as adapters

sentinel = ImportError("first real loader failure")

class RetryLoader(importlib.abc.Loader):
    def __init__(self):
        self.calls = 0

    def exec_module(self, module):
        self.calls += 1
        if self.calls == 1:
            raise sentinel
        module.ready = True

class QStatFinder(importlib.abc.MetaPathFinder):
    def __init__(self, loader):
        self.loader = loader

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "obozrenie.adapters.qstat":
            return importlib.util.spec_from_loader(fullname, self.loader)
        return None

loader = RetryLoader()
sys.meta_path.insert(0, QStatFinder(loader))
try:
    adapters.resolve_adapter("qstat")
except BaseException as error:
    assert error is sentinel
else:
    raise AssertionError("first real loader failure did not propagate")
assert "qstat" not in adapters.adapter_table
assert "obozrenie.adapters.qstat" not in sys.modules
assert "qstat" not in vars(adapters)

resolved = adapters.resolve_adapter("qstat")
assert resolved.ready is True
assert loader.calls == 2
assert adapters.adapter_table["qstat"] is resolved
assert sys.modules["obozrenie.adapters.qstat"] is resolved
assert vars(adapters)["qstat"] is resolved
print("REAL_IMPORT_RETRY_OK")
''')
        self.assertEqual(result.stdout.splitlines(), ["REAL_IMPORT_RETRY_OK"])


class ServerModelTests(unittest.TestCase):
    def test_complete_mapping_has_stable_defaults(self):
        server = Server(
            endpoint=Endpoint("example.org", 30000),
            game_id="minetest",
            name="Test",
            player_count=1,
            player_limit=20,
        )

        self.assertEqual(
            server.to_mapping(),
            {
                "host": "example.org:30000",
                "game_id": "minetest",
                "name": "Test",
                "password": False,
                "player_count": 1,
                "player_limit": 20,
                "ping": 9999,
                "secure": False,
                "country": "unknown",
                "game_mod": "",
                "game_type": "",
                "terrain": "",
                "players": [],
                "rules": {},
            },
        )

    def test_network_enrichment_returns_a_new_record(self):
        server = Server(endpoint=Endpoint("example.org", 1), game_id="g")

        enriched = server.with_network(42, "DE")

        self.assertEqual((enriched.ping, enriched.country), (42, "DE"))
        self.assertEqual((server.ping, server.country), (9999, "unknown"))

    def test_players_and_rules_are_immutable_copies(self):
        player = Player("Ada", score=7, ping=12)
        players = [player]
        rules = {"mode": "survival", "round": 3, "ranked": True, "motd": None}
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=players,
            rules=rules,
        )

        players.clear()
        rules["mode"] = "creative"

        self.assertEqual(server.players, (player,))
        self.assertIsInstance(server.rules, MappingProxyType)
        self.assertEqual(dict(server.rules), {
            "mode": "survival",
            "round": 3,
            "ranked": True,
            "motd": None,
        })
        with self.assertRaises(TypeError):
            server.rules["mode"] = "creative"
        with self.assertRaises(FrozenInstanceError):
            player.name = "Grace"
        with self.assertRaises(FrozenInstanceError):
            server.name = "Changed"

    def test_rejects_mutable_or_composite_rule_values(self):
        for value in ([], {}, set(), (), object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                Server(
                    endpoint=Endpoint("example.org", 1),
                    game_id="g",
                    rules={"invalid": value},
                )

    def test_rejects_negative_player_counts(self):
        for counts in ((-1, 0), (0, -1)):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                Server(
                    endpoint=Endpoint("example.org", 1),
                    game_id="g",
                    player_count=counts[0],
                    player_limit=counts[1],
                )

    def test_deepcopy_returns_the_same_immutable_record(self):
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=(Player("Ada"),),
            rules={"mode": "survival"},
        )

        self.assertIs(copy.deepcopy(server), server)

    def test_mapping_is_a_mutable_boundary_copy(self):
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=(Player("Ada"),),
            rules={"mode": "survival"},
        )

        mapping = server.to_mapping()
        mapping["players"][0]["name"] = "Grace"
        mapping["rules"]["mode"] = "creative"

        self.assertEqual(server.players, (Player("Ada"),))
        self.assertEqual(dict(server.rules), {"mode": "survival"})


class AdapterRttBoundaryTests(unittest.TestCase):
    def test_rtt_enrichment_returns_replaced_server_records(self):
        server = Server(
            endpoint=Endpoint("2001:db8::1", 30000),
            game_id="g",
        )
        received_endpoints = []

        def fake_measure_rtt(endpoints, *, deadline=None):
            endpoints = list(endpoints)
            received_endpoints.append(endpoints)
            return {endpoint: 17 for endpoint in endpoints}

        # The pool/worker machinery behind a real ICMP measurement is
        # RttTests' and test_network_calls.py's responsibility; this
        # boundary test only proves add_rtt_info consumes and maps the
        # endpoint iterable through measure_rtt, without ever starting the
        # process-global ICMP registry (which 32 real workers would leak
        # across every other test in the process).
        with (
            mock.patch.object(
                ping,
                "measure_rtt",
                side_effect=fake_measure_rtt,
            ) as measure_rtt,
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "adapter RTT boundary reached the process-global registry"
                ),
            ) as get_pools,
            mock.patch.object(
                ping,
                "icmp_ping",
                side_effect=AssertionError(
                    "adapter RTT boundary started a real ICMP probe"
                ),
            ) as ping_call,
        ):
            enriched = ping.add_rtt_info([server])

        self.assertEqual(
            enriched,
            [
                Server(
                    endpoint=Endpoint("2001:db8::1", 30000),
                    game_id="g",
                    ping=17,
                )
            ],
        )
        self.assertEqual(server.ping, 9999)
        self.assertEqual(received_endpoints, [[Endpoint("2001:db8::1", 30000)]])
        measure_rtt.assert_called_once()
        get_pools.assert_not_called()
        ping_call.assert_not_called()


class _FailClosedHttpAdapterTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch(
            "requests.Session",
            side_effect=AssertionError(
                "adapter bypassed http_client.get_json via requests.Session"
            ),
        ))
        self.enterContext(mock.patch(
            "requests.get",
            side_effect=AssertionError(
                "adapter bypassed http_client.get_json via requests.get"
            ),
        ))

    def assert_hostile_first_master_is_isolated(
        self,
        stat_master,
        request_url,
        healthy_body,
        expected_endpoints,
    ):
        """Assert no first-master body can suppress a later master's records.

        The request runs through the real HTTP boundary against a scripted
        session, so the decode and parse phases are the production ones.
        """
        masters = [
            "https://hostile.example/list",
            "https://healthy.example/list",
        ]
        for label, hostile_body, encoding in HOSTILE_DECODE_BODIES:
            with self.subTest(body=label):
                session = _ScriptedSession({
                    request_url(masters[0]): _streamed_response(
                        hostile_body,
                        encoding=encoding,
                    ),
                    request_url(masters[1]): _streamed_response(healthy_body),
                })
                messages = []
                with (
                    installed_network_pools(),
                    mock.patch(
                        "obozrenie.http_client.requests.Session",
                        return_value=session,
                    ),
                    mock.patch.object(helpers, "debug_msg", messages.append),
                ):
                    succeeded, outcome = _capture_value_or_error(
                        lambda: stat_master(masters)
                    )

                self.assertTrue(succeeded, outcome)
                self.assertEqual(
                    [server.endpoint for server in outcome],
                    expected_endpoints,
                )
                self.assertEqual(
                    session.requested,
                    [request_url(master) for master in masters],
                )
                [message] = messages
                self.assertEqual(
                    [str(part) for part in message[1:]],
                    [
                        "Failed to read master https://hostile.example/"
                        "<redacted>.",
                        "HTTP response decoding failed.",
                    ],
                )


class MinetestAdapterTests(_FailClosedHttpAdapterTestCase):
    def test_transport_guard_rejects_session_construction(self):
        with self.assertRaisesRegex(
            AssertionError,
            "adapter bypassed http_client.get_json via requests.Session",
        ):
            requests.Session()

    def test_transport_guard_rejects_module_level_get(self):
        with (
            mock.patch(
                "requests.sessions.Session.request",
                return_value=mock.sentinel.response,
            ),
            self.assertRaisesRegex(
                AssertionError,
                "adapter bypassed http_client.get_json via requests.get",
            ),
        ):
            requests.get("https://transport-bypass.invalid/")

    def test_current_minetest_schema_uses_address_capacity_and_protocol_rules(self):
        server = minetest.parse_json_entry("minetest", CURRENT_MINETEST_ENTRY)

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("kattttt.ddns.net", 31337),
                game_id="minetest",
                name="kattttt's kingdom",
                player_count=0,
                player_limit=50,
                game_type="minetest",
                rules={"proto_min": 37, "proto_max": 52},
            ),
        )
        self.assertEqual(str(server.endpoint), "kattttt.ddns.net:31337")

    def test_current_freeminer_schema_prefers_client_address_over_ipv6_ip(self):
        server = minetest.parse_json_entry("freeminer", CURRENT_FREEMINER_ENTRY)

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("fm.setun.net", 30000),
                game_id="freeminer",
                name="fm: Sky: Yggdrasil tree growing",
                player_count=1,
                player_limit=199,
                game_type="sky",
                players=(Player("growbot"),),
                rules={"proto_min": 37, "proto_max": 148},
            ),
        )
        self.assertEqual(str(server.endpoint), "fm.setun.net:30000")

    def test_ip_is_an_ipv6_fallback_when_address_is_missing(self):
        entry = {
            "ip": "2001:db8::10",
            "port": 30000,
            "clients": 1,
            "clients_max": 20,
            "proto_min": 37,
            "proto_max": 42,
            "name": "IPv6 Minetest",
            "gameid": "minetest",
            "clients_list": ["Ada"],
            "password": True,
        }

        server = minetest.parse_json_entry("minetest", entry)

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("2001:db8::10", 30000),
                game_id="minetest",
                name="IPv6 Minetest",
                password=True,
                player_count=1,
                player_limit=20,
                game_type="minetest",
                players=(Player("Ada"),),
                rules={"proto_min": 37, "proto_max": 42},
            ),
        )
        self.assertEqual(str(server.endpoint), "[2001:db8::10]:30000")

    def test_missing_optional_fields_use_server_defaults(self):
        server = minetest.parse_json_entry("minetest", {
            "address": "minimal.example",
            "port": "30001",
            "clients": "0",
            "clients_max": "12",
        })

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("minimal.example", 30001),
                game_id="minetest",
                player_count=0,
                player_limit=12,
            ),
        )

    def test_integral_float_numeric_fields_are_normalized_to_integers(self):
        entry = {
            **CURRENT_MINETEST_ENTRY,
            "port": 31337.0,
            "clients": 0.0,
            "clients_max": 50.0,
            "proto_min": 37.0,
            "proto_max": 52.0,
        }

        server = minetest.parse_json_entry("minetest", entry)

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("kattttt.ddns.net", 31337),
                game_id="minetest",
                name="kattttt's kingdom",
                player_count=0,
                player_limit=50,
                game_type="minetest",
                rules={"proto_min": 37, "proto_max": 52},
            ),
        )
        self.assertIs(type(server.endpoint.port), int)
        self.assertIs(type(server.player_count), int)
        self.assertIs(type(server.player_limit), int)
        self.assertIs(type(server.rules["proto_min"]), int)
        self.assertIs(type(server.rules["proto_max"]), int)

    def test_invalid_numeric_forms_are_malformed(self):
        for field in ("port", "clients", "clients_max", "proto_min", "proto_max"):
            for value in (True, 1.5, " 1", "+1"):
                malformed_entry = {**CURRENT_MINETEST_ENTRY, field: value}
                with self.subTest(field=field, value=value):
                    with (
                        mock.patch(
                            "obozrenie.http_client.get_json",
                            return_value={
                                "list": [CURRENT_MINETEST_ENTRY, malformed_entry]
                            },
                        ),
                        mock.patch.object(helpers, "debug_msg") as debug_msg,
                    ):
                        servers = minetest.stat_master(
                            "minetest",
                            {"name": "Minetest"},
                            ["https://servers.minetest.net/list"],
                        )

                    self.assertEqual(
                        servers,
                        [
                            Server(
                                endpoint=Endpoint("kattttt.ddns.net", 31337),
                                game_id="minetest",
                                name="kattttt's kingdom",
                                player_count=0,
                                player_limit=50,
                                game_type="minetest",
                                rules={"proto_min": 37, "proto_max": 52},
                            )
                        ],
                    )
                    debug_msg.assert_called_once_with([
                        "Adapter/Minetest:",
                        "Skipped malformed Minetest server entries: 1.",
                    ])

    def test_non_mapping_entry_is_malformed(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value={"list": [CURRENT_MINETEST_ENTRY, "not-a-mapping"]},
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                ["https://servers.minetest.net/list"],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("kattttt.ddns.net", 31337),
                    game_id="minetest",
                    name="kattttt's kingdom",
                    player_count=0,
                    player_limit=50,
                    game_type="minetest",
                    rules={"proto_min": 37, "proto_max": 52},
                )
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Minetest:",
            "Skipped malformed Minetest server entries: 1.",
        ])

    def test_present_optional_fields_require_declared_json_types(self):
        malformed_values = (
            ("password", "false"),
            ("password", 0),
            ("password", None),
            ("name", 17),
            ("name", None),
            ("gameid", 17),
            ("gameid", None),
            ("clients_list", None),
            ("clients_list", "Ada"),
            ("clients_list", {"name": "Ada"}),
            ("clients_list", [17]),
            ("clients_list", ["Ada", None]),
        )
        for field, value in malformed_values:
            malformed_entry = {**CURRENT_MINETEST_ENTRY, field: value}
            with self.subTest(field=field, value=value):
                with (
                    mock.patch(
                        "obozrenie.http_client.get_json",
                        return_value={
                            "list": [CURRENT_MINETEST_ENTRY, malformed_entry]
                        },
                    ),
                    mock.patch.object(helpers, "debug_msg") as debug_msg,
                ):
                    servers = minetest.stat_master(
                        "minetest",
                        {"name": "Minetest"},
                        ["https://servers.minetest.net/list"],
                    )

                self.assertEqual(
                    servers,
                    [
                        Server(
                            endpoint=Endpoint("kattttt.ddns.net", 31337),
                            game_id="minetest",
                            name="kattttt's kingdom",
                            player_count=0,
                            player_limit=50,
                            game_type="minetest",
                            rules={"proto_min": 37, "proto_max": 52},
                        )
                    ],
                )
                debug_msg.assert_called_once_with([
                    "Adapter/Minetest:",
                    "Skipped malformed Minetest server entries: 1.",
                ])

    def test_all_entries_with_invalid_field_types_raise_contract_error(self):
        malformed_entries = [
            "not-a-mapping",
            {**CURRENT_MINETEST_ENTRY, "address": 17},
            {**CURRENT_MINETEST_ENTRY, "port": True},
            {**CURRENT_MINETEST_ENTRY, "clients": 0.5},
            {**CURRENT_MINETEST_ENTRY, "clients_max": True},
            {**CURRENT_MINETEST_ENTRY, "proto_min": 37.5},
            {**CURRENT_MINETEST_ENTRY, "proto_max": False},
            {**CURRENT_MINETEST_ENTRY, "password": "false"},
            {**CURRENT_MINETEST_ENTRY, "name": 17},
            {**CURRENT_MINETEST_ENTRY, "gameid": 17},
            {**CURRENT_MINETEST_ENTRY, "clients_list": [17]},
        ]
        with self.assertRaises(ValueError) as raised:
            minetest.adapt_server_list(
                "minetest",
                {"list": malformed_entries},
            )

        self.assertEqual(
            str(raised.exception),
            "Minetest response contained no valid servers",
        )

    def test_stat_master_passes_one_literal_deadline_to_every_source(self):
        clock = SimpleNamespace(now=100.0, calls=[])

        def monotonic():
            clock.calls.append(clock.now)
            return clock.now

        def read_master(uri, **_kwargs):
            if uri == "https://first.example/list":
                clock.now = 900.0
                return {"list": []}
            if uri == "https://second.example/list":
                return {"list": [CURRENT_MINETEST_ENTRY]}
            raise AssertionError(f"unexpected Minetest master: {uri}")

        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                side_effect=monotonic,
            ),
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=read_master,
            ) as get_json,
        ):
            servers = minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                [
                    "https://first.example/list",
                    "https://second.example/list",
                ],
            )

        self.assertEqual(clock.calls, [100.0])
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call("https://first.example/list", deadline=130.0),
                mock.call("https://second.example/list", deadline=130.0),
            ],
        )
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].endpoint, Endpoint("kattttt.ddns.net", 31337))

    def test_failed_first_master_is_isolated_under_one_deadline(self):
        masters = [
            "https://first.example/private?token=first%2Fsecret#fragment",
            "https://second.example/list",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError("first source secret"),
                    {"list": [CURRENT_MINETEST_ENTRY]},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(
            [server.endpoint for server in outcome],
            [Endpoint("kattttt.ddns.net", 31337)],
        )
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(masters[0], deadline=130.0),
                mock.call(masters[1], deadline=130.0),
            ],
        )

    def test_hostile_first_master_body_cannot_suppress_a_later_master(self):
        self.assert_hostile_first_master_is_isolated(
            lambda masters: minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                masters,
            ),
            lambda source: source,
            json.dumps({"list": [CURRENT_MINETEST_ENTRY]}).encode("utf-8"),
            [Endpoint("kattttt.ddns.net", 31337)],
        )

    def test_unclassified_http_value_error_propagates_by_identity(self):
        error = ValueError("unclassified HTTP seam failure")
        master = "https://master.example/list"
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=error,
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    [master],
                )
            )

        self.assertFalse(succeeded)
        self.assertIs(outcome, error)
        get_json.assert_called_once_with(master, deadline=mock.ANY)
        debug_msg.assert_not_called()

    def test_failed_later_master_preserves_earlier_results(self):
        first_entry = {
            **CURRENT_MINETEST_ENTRY,
            "address": "first-success.example",
            "port": 30001,
        }
        masters = [
            "https://first.example/list",
            "https://failed.example/list",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    {"list": [first_entry]},
                    {"servers": ["SECRET FAILED PAYLOAD"]},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(
            [server.endpoint for server in outcome],
            [Endpoint("first-success.example", 30001)],
        )
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(masters[0], deadline=130.0),
                mock.call(masters[1], deadline=130.0),
            ],
        )

    def test_successes_surrounding_failed_master_preserve_order(self):
        first_entry = {
            **CURRENT_MINETEST_ENTRY,
            "address": "first-success.example",
            "port": 30001,
        }
        third_entry = {
            **CURRENT_MINETEST_ENTRY,
            "address": "third-success.example",
            "port": 30003,
        }
        masters = [
            "https://first.example/list",
            "https://failed.example/list?token=middle%2Fsecret",
            "https://third.example/list",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    {"list": [first_entry]},
                    requests.Timeout("middle source secret"),
                    {"list": [third_entry]},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(
            [server.endpoint for server in outcome],
            [
                Endpoint("first-success.example", 30001),
                Endpoint("third-success.example", 30003),
            ],
        )
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [mock.call(master, deadline=130.0) for master in masters],
        )

    def test_all_failed_masters_raise_payload_free_aggregate_error(self):
        masters = [
            "https://first.example/private?token=first%2Fsecret#fragment",
            "https://second.example/private?token=second%2Fsecret#fragment",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError("SECRET TRANSPORT PAYLOAD"),
                    {"secret": "SECRET RESPONSE PAYLOAD"},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertFalse(succeeded)
        self.assertIsInstance(outcome, SafeDiagnosticError)
        self.assertIs(type(outcome), AllMastersFailedError)
        self.assertEqual(str(outcome), "All configured master sources failed.")
        for secret in (
            masters[0],
            masters[1],
            "first%2Fsecret",
            "first/secret",
            "second%2Fsecret",
            "second/secret",
            "SECRET TRANSPORT PAYLOAD",
            "SECRET RESPONSE PAYLOAD",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, str(outcome))
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [mock.call(master, deadline=130.0) for master in masters],
        )

    def test_valid_empty_master_counts_as_success(self):
        masters = [
            "https://failed.example/list",
            "https://empty.example/list",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.Timeout("failed source secret"),
                    {"list": []},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(outcome, [])
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [mock.call(master, deadline=130.0) for master in masters],
        )

    def test_failed_master_diagnostic_redacts_configured_url(self):
        secret_url = (
            "https://userinfo-name:password%2Fsecret@master.example:8443/"
            "private%2Fpath?token=query%2Fsecret#fragment%2Fsecret"
        )
        masters = [secret_url, "https://empty.example/list"]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError(
                        "userinfo-name password/secret private/path "
                        "query/secret fragment/secret"
                    ),
                    {"list": []},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(outcome, [])
        debug_msg.assert_called_once_with([
            "Adapter/Minetest:",
            "Failed to read master https://master.example:8443/<redacted>.",
            SafeDiagnostic("HTTP connection failed."),
        ])
        rendered_diagnostic = str(debug_msg.call_args_list)
        for secret in (
            secret_url,
            "userinfo-name",
            "password%2Fsecret",
            "password/secret",
            "private%2Fpath",
            "private/path",
            "query%2Fsecret",
            "query/secret",
            "fragment%2Fsecret",
            "fragment/secret",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered_diagnostic)
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [mock.call(master, deadline=130.0) for master in masters],
        )

    def test_stat_master_uses_shared_json_client_without_rtt(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value={"list": [CURRENT_MINETEST_ENTRY]},
            ) as get_json,
            mock.patch.object(
                ping,
                "add_rtt_info",
                side_effect=AssertionError("adapter performed RTT enrichment"),
            ),
        ):
            servers = minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                ["https://servers.minetest.net/list"],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("kattttt.ddns.net", 31337),
                    game_id="minetest",
                    name="kattttt's kingdom",
                    player_count=0,
                    player_limit=50,
                    game_type="minetest",
                    rules={"proto_min": 37, "proto_max": 52},
                )
            ],
        )
        get_json.assert_called_once_with(
            "https://servers.minetest.net/list",
            deadline=mock.ANY,
        )

    def test_adapt_server_list_rejects_invalid_shared_json_shape(self):
        for payload in ([], {"list": {}}, {"servers": []}):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    ValueError,
                    "^Minetest response must contain a list of servers$",
                ),
            ):
                minetest.adapt_server_list("minetest", payload)

    def test_stat_master_accepts_an_empty_server_list(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value={"list": []},
            ),
            mock.patch.object(
                ping,
                "add_rtt_info",
                side_effect=AssertionError("adapter performed RTT enrichment"),
            ),
        ):
            servers = minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                ["https://servers.minetest.net/list"],
            )

        self.assertEqual(servers, [])

    def test_stat_master_keeps_valid_entries_and_logs_invalid_count(self):
        malformed_entries = [
            {
                "address": "broken.example",
                "port": "not-a-port",
                "clients": 0,
                "clients_max": 10,
            },
            {
                "address": "overflow.example",
                "port": 30000,
                "clients": 0,
                "clients_max": float("inf"),
            },
        ]
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value={"list": [CURRENT_MINETEST_ENTRY, *malformed_entries]},
            ),
            mock.patch.object(
                ping,
                "add_rtt_info",
                side_effect=AssertionError("adapter performed RTT enrichment"),
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = minetest.stat_master(
                "minetest",
                {"name": "Minetest"},
                ["https://servers.minetest.net/list"],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("kattttt.ddns.net", 31337),
                    game_id="minetest",
                    name="kattttt's kingdom",
                    player_count=0,
                    player_limit=50,
                    game_type="minetest",
                    rules={"proto_min": 37, "proto_max": 52},
                )
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Minetest:",
            "Skipped malformed Minetest server entries: 2.",
        ])

    def test_adapt_server_list_rejects_nonempty_all_malformed_response(self):
        malformed_entries = [
            {},
            {
                "address": "broken.example",
                "port": 30000,
                "clients": 0,
                "clients_max": "many",
            },
        ]
        with self.assertRaises(ValueError) as raised:
            minetest.adapt_server_list(
                "minetest",
                {"list": malformed_entries},
            )

        self.assertEqual(
            str(raised.exception),
            "Minetest response contained no valid servers",
        )

    def test_gint_overflow_entry_is_skipped_while_valid_sibling_survives(self):
        overflowing_entry = {
            **CURRENT_MINETEST_ENTRY,
            "address": "overflow.example",
            "port": 30000,
            "clients_max": 2147483648,
        }
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = minetest.adapt_server_list(
                "minetest",
                {"list": [CURRENT_MINETEST_ENTRY, overflowing_entry]},
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("kattttt.ddns.net", 31337),
                    game_id="minetest",
                    name="kattttt's kingdom",
                    player_count=0,
                    player_limit=50,
                    game_type="minetest",
                    rules={"proto_min": 37, "proto_max": 52},
                )
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Minetest:",
            "Skipped malformed Minetest server entries: 1.",
        ])

    def test_default_master_urls_use_https(self):
        from obozrenie import resources

        game_config = resources.load_toml(
            "settings", "defaults", "games.toml"
        )

        self.assertEqual(
            {
                "minetest": game_config["minetest"]["master_uri"],
                "freeminer": game_config["freeminer"]["master_uri"],
            },
            {
                "minetest": ["https://servers.minetest.net/list"],
                "freeminer": ["https://servers.freeminer.org/list"],
            },
        )


class RigsOfRodsAdapterTests(_FailClosedHttpAdapterTestCase):
    def test_request_url_normalizes_json_without_losing_url_components(self):
        cases = (
            (
                "https://api.example/server-list",
                "https://api.example/server-list?json=true",
            ),
            (
                "https://api.example/server-list?token=a%20b",
                "https://api.example/server-list?token=a%20b&json=true",
            ),
            (
                "https://api.example/server-list#active",
                "https://api.example/server-list?json=true#active",
            ),
            (
                "https://api.example/server-list?token=abc#active",
                "https://api.example/server-list?token=abc&json=true#active",
            ),
            (
                "https://api.example/server-list?token=abc&json=true#active",
                "https://api.example/server-list?token=abc&json=true#active",
            ),
            (
                "https://api.example/server-list?token=a%2Fb&%6Ason=#active",
                "https://api.example/server-list?token=a%2Fb&json=true#active",
            ),
            (
                "https://api.example/server-list?json=false&token=a%2Fb&mode=x+y#active",
                "https://api.example/server-list?token=a%2Fb&mode=x+y&json=true#active",
            ),
            (
                "https://api.example/server-list?first=a%2Fb&json=false&middle=x+y&json=true&last=%26#active",
                "https://api.example/server-list?first=a%2Fb&middle=x+y&last=%26&json=true#active",
            ),
            (
                "https://api.example/server-list?first=a%2Fb&json=true&middle=x+y&json=false&last=%26#active",
                "https://api.example/server-list?first=a%2Fb&middle=x+y&last=%26&json=true#active",
            ),
            (
                "https://api.example/server-list?notjson=true#active",
                "https://api.example/server-list?notjson=true&json=true#active",
            ),
        )

        for configured, expected in cases:
            with self.subTest(configured=configured):
                self.assertEqual(
                    rigsofrods.master_request_url(configured),
                    expected,
                )

    def test_full_api_ipv4_entry_becomes_a_normalized_server_record(self):
        server = rigsofrods.parse_server_entry(
            "rigsofrods", CURRENT_RIGSOFRODS_IPV4_ENTRY
        )

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("71.239.149.231", 12001),
                game_id="rigsofrods",
                name="Kipp County | CDL+",
                password=True,
                player_count=1,
                player_limit=8,
                game_type="rigsofrods",
                terrain="any",
                players=(Player("Kipp Bot"),),
                rules={"verified": 1, "is_official": 0},
            ),
        )
        self.assertEqual(str(server.endpoint), "71.239.149.231:12001")

    def test_full_api_ipv6_entry_becomes_a_normalized_server_record(self):
        server = rigsofrods.parse_server_entry(
            "rigsofrods", CURRENT_RIGSOFRODS_IPV6_ENTRY
        )

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("2001:db8::20", 12000),
                game_id="rigsofrods",
                name="RoR IPv6",
                player_count=2,
                player_limit=24,
                game_type="rigsofrods",
                terrain="Desert",
                players=(Player("Ada"), Player("Lin")),
                rules={"verified": 0, "is_official": 1},
            ),
        )
        self.assertEqual(str(server.endpoint), "[2001:db8::20]:12000")

    def test_malformed_field_types_are_rejected_before_record_creation(self):
        malformed_entries = (
            (
                "has-password",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "has-password": 1},
            ),
            (
                "current-users",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "current-users": True},
            ),
            (
                "max-clients",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "max-clients": 8.5},
            ),
            (
                "verified",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "verified": "1"},
            ),
            (
                "is-official",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "is-official": False},
            ),
            ("ip", {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "ip": 71239149231}),
            ("port", {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "port": "12001"}),
            (
                "terrain-name",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "terrain-name": None},
            ),
            ("name", {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "name": 17}),
            (
                "json-userlist",
                {**CURRENT_RIGSOFRODS_IPV4_ENTRY, "json-userlist": None},
            ),
            (
                "username",
                {
                    **CURRENT_RIGSOFRODS_IPV4_ENTRY,
                    "json-userlist": [{"username": 17}],
                },
            ),
        )

        for field, entry in malformed_entries:
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    rigsofrods.parse_server_entry("rigsofrods", entry)

    def test_stat_master_passes_one_literal_deadline_to_every_source(self):
        clock = SimpleNamespace(now=100.0, calls=[])

        def monotonic():
            clock.calls.append(clock.now)
            return clock.now

        def read_master(uri, **_kwargs):
            if uri == "https://first.example/list?token=a&json=true":
                clock.now = 900.0
                raise requests.ConnectionError("first source offline")
            if uri == "https://second.example/list?json=true":
                return []
            raise AssertionError(f"unexpected Rigs of Rods master: {uri}")

        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                side_effect=monotonic,
            ),
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=read_master,
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://first.example/list?token=a",
                    "https://second.example/list",
                ],
            )

        self.assertEqual(servers, [])
        self.assertEqual(clock.calls, [100.0])
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://first.example/list?token=a&json=true",
                    deadline=130.0,
                ),
                mock.call(
                    "https://second.example/list?json=true",
                    deadline=130.0,
                ),
            ],
        )

    def test_successes_surrounding_failed_master_preserve_order_and_deadline(self):
        masters = [
            "https://first.example/list?token=first",
            "https://failed.example/private?token=middle%2Fsecret#fragment",
            "https://third.example/list?json=false&mode=last",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    [CURRENT_RIGSOFRODS_IPV4_ENTRY],
                    requests.Timeout("middle source secret"),
                    [CURRENT_RIGSOFRODS_IPV6_ENTRY],
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: rigsofrods.stat_master(
                    "rigsofrods",
                    {"name": "Rigs of Rods"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(
            [server.endpoint for server in outcome],
            [
                Endpoint("71.239.149.231", 12001),
                Endpoint("2001:db8::20", 12000),
            ],
        )
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://first.example/list?token=first&json=true",
                    deadline=130.0,
                ),
                mock.call(
                    "https://failed.example/private?token=middle%2Fsecret&json=true#fragment",
                    deadline=130.0,
                ),
                mock.call(
                    "https://third.example/list?mode=last&json=true",
                    deadline=130.0,
                ),
            ],
        )

    def test_requests_family_payload_errors_propagate_by_identity(self):
        errors = (
            requests.Timeout("payload timeout"),
            requests.ConnectionError("payload connection failure"),
            requests.HTTPError("payload status failure"),
            requests.RequestException("payload request failure"),
            requests.exceptions.InvalidURL("payload URL failure"),
        )
        master = "https://master.example/list"

        for error in errors:
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch(
                        "obozrenie.http_client.get_json",
                        return_value=[],
                    ) as get_json,
                    mock.patch.object(
                        rigsofrods,
                        "adapt_server_list",
                        side_effect=error,
                    ),
                    mock.patch.object(helpers, "debug_msg") as debug_msg,
                ):
                    succeeded, outcome = _capture_value_or_error(
                        lambda: rigsofrods.stat_master(
                            "rigsofrods",
                            {"name": "Rigs of Rods"},
                            [master],
                        )
                    )

                self.assertFalse(succeeded)
                self.assertIs(outcome, error)
                get_json.assert_called_once_with(
                    "https://master.example/list?json=true",
                    deadline=mock.ANY,
                )
                debug_msg.assert_not_called()

    def test_failed_master_diagnostic_redacts_userinfo_path_query_and_fragment(self):
        secret_url = (
            "https://userinfo-name:password%2Fsecret@master.example:8443/"
            "private%2Fpath?token=query%2Fsecret#fragment%2Fsecret"
        )
        masters = [secret_url, "https://empty.example/list"]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError(
                        "userinfo-name password/secret private/path "
                        "query/secret fragment/secret"
                    ),
                    [],
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            succeeded, outcome = _capture_value_or_error(
                lambda: rigsofrods.stat_master(
                    "rigsofrods",
                    {"name": "Rigs of Rods"},
                    masters,
                )
            )

        self.assertTrue(succeeded, outcome)
        self.assertEqual(outcome, [])
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Failed to read master https://master.example:8443/<redacted>.",
            SafeDiagnostic("HTTP connection failed."),
        ])
        rendered_diagnostic = str(debug_msg.call_args_list)
        for secret in (
            secret_url,
            "userinfo-name",
            "password%2Fsecret",
            "password/secret",
            "private%2Fpath",
            "private/path",
            "query%2Fsecret",
            "query/secret",
            "fragment%2Fsecret",
            "fragment/secret",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered_diagnostic)
        monotonic.assert_called_once_with()
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://userinfo-name:password%2Fsecret@master.example:8443/"
                    "private%2Fpath?token=query%2Fsecret&json=true#fragment%2Fsecret",
                    deadline=130.0,
                ),
                mock.call(
                    "https://empty.example/list?json=true",
                    deadline=130.0,
                ),
            ],
        )

    def test_all_failed_masters_raise_shared_payload_free_error(self):
        rigs_masters = [
            "https://rigs-one.example/private?token=rigs%2Fone",
            "https://rigs-two.example/private?token=rigs%2Ftwo",
        ]
        minetest_masters = [
            "https://mine-one.example/private?token=mine%2Fone",
            "https://mine-two.example/private?token=mine%2Ftwo",
        ]
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError("SECRET RIGS TRANSPORT"),
                    {"secret": "SECRET RIGS RESPONSE"},
                    requests.Timeout("SECRET MINETEST TRANSPORT"),
                    {"secret": "SECRET MINETEST RESPONSE"},
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg"),
        ):
            rigs_succeeded, rigs_error = _capture_value_or_error(
                lambda: rigsofrods.stat_master(
                    "rigsofrods",
                    {"name": "Rigs of Rods"},
                    rigs_masters,
                )
            )
            minetest_succeeded, minetest_error = _capture_value_or_error(
                lambda: minetest.stat_master(
                    "minetest",
                    {"name": "Minetest"},
                    minetest_masters,
                )
            )

        self.assertFalse(rigs_succeeded)
        self.assertFalse(minetest_succeeded)
        self.assertIs(type(rigs_error), type(minetest_error))
        self.assertIsInstance(rigs_error, SafeDiagnosticError)
        self.assertIs(type(rigs_error), AllMastersFailedError)
        self.assertEqual(str(rigs_error), "All configured master sources failed.")
        self.assertEqual(str(minetest_error), str(rigs_error))
        for error in (rigs_error, minetest_error):
            rendered_error = str(error)
            for secret in (
                *rigs_masters,
                *minetest_masters,
                "rigs/one",
                "rigs/two",
                "mine/one",
                "mine/two",
                "SECRET RIGS TRANSPORT",
                "SECRET RIGS RESPONSE",
                "SECRET MINETEST TRANSPORT",
                "SECRET MINETEST RESPONSE",
            ):
                with self.subTest(error=type(error).__name__, secret=secret):
                    self.assertNotIn(secret, rendered_error)
        self.assertEqual(monotonic.call_count, 2)
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://rigs-one.example/private?token=rigs%2Fone&json=true",
                    deadline=130.0,
                ),
                mock.call(
                    "https://rigs-two.example/private?token=rigs%2Ftwo&json=true",
                    deadline=130.0,
                ),
                mock.call(minetest_masters[0], deadline=130.0),
                mock.call(minetest_masters[1], deadline=130.0),
            ],
        )

    def test_hostile_first_master_body_cannot_suppress_a_later_master(self):
        self.assert_hostile_first_master_is_isolated(
            lambda masters: rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                masters,
            ),
            rigsofrods.master_request_url,
            json.dumps([CURRENT_RIGSOFRODS_IPV4_ENTRY]).encode("utf-8"),
            [Endpoint("71.239.149.231", 12001)],
        )

    def test_each_failed_master_is_isolated_from_a_later_success(self):
        failures = (
            requests.ConnectionError("offline"),
            requests.Timeout("timed out"),
            requests.HTTPError("503 Server Error"),
            json.JSONDecodeError("Expecting value", "not-json", 0),
            {"servers": []},
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch(
                        "obozrenie.http_client.get_json",
                        side_effect=[failure, [CURRENT_RIGSOFRODS_IPV4_ENTRY]],
                    ) as get_json,
                    mock.patch.object(helpers, "debug_msg"),
                    mock.patch.object(
                        ping,
                        "add_rtt_info",
                        side_effect=AssertionError(
                            "adapter performed RTT enrichment"
                        ),
                    ),
                ):
                    servers = rigsofrods.stat_master(
                        "rigsofrods",
                        {"name": "Rigs of Rods"},
                        [
                            "https://bad.example/list?token=abc#servers",
                            "https://good.example/list?json=true",
                        ],
                    )

                self.assertEqual(
                    servers,
                    [
                        Server(
                            endpoint=Endpoint("71.239.149.231", 12001),
                            game_id="rigsofrods",
                            name="Kipp County | CDL+",
                            password=True,
                            player_count=1,
                            player_limit=8,
                            game_type="rigsofrods",
                            terrain="any",
                            players=(Player("Kipp Bot"),),
                            rules={"verified": 1, "is_official": 0},
                        )
                    ],
                )
                self.assertEqual(
                    get_json.call_args_list,
                    [
                        mock.call(
                            "https://bad.example/list?token=abc&json=true#servers",
                            deadline=mock.ANY,
                        ),
                        mock.call(
                            "https://good.example/list?json=true",
                            deadline=mock.ANY,
                        ),
                    ],
                )

    def test_unnormalizable_master_url_is_isolated_without_a_request(self):
        malformed_master = (
            "https://[unclosed-host-secret/list?token=url%2Fsecret"
        )
        with (
            mock.patch(
                "obozrenie.adapters.http_masters.time.monotonic",
                return_value=100.0,
            ) as monotonic,
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value=[CURRENT_RIGSOFRODS_IPV4_ENTRY],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [malformed_master, "https://valid.example/list"],
            )

        self.assertEqual(
            [server.endpoint for server in servers],
            [Endpoint("71.239.149.231", 12001)],
        )
        monotonic.assert_called_once_with()
        get_json.assert_called_once_with(
            "https://valid.example/list?json=true",
            deadline=130.0,
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Failed to read master <invalid URL>.",
            SafeDiagnostic("Malformed master URL."),
        ])
        rendered_diagnostic = str(debug_msg.call_args_list)
        for secret in (
            malformed_master,
            "unclosed-host-secret",
            "url%2Fsecret",
            "[unclosed",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered_diagnostic)

    def test_later_size_limit_failure_preserves_earlier_success(self):
        size_error = http_client.ResponseTooLargeError(
            SafeDiagnostic("HTTP response exceeds 16777216 bytes")
        )
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    [CURRENT_RIGSOFRODS_IPV4_ENTRY],
                    size_error,
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
            mock.patch.object(
                ping,
                "add_rtt_info",
                side_effect=AssertionError("adapter performed RTT enrichment"),
            ),
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://good.example/list?json=true",
                    "https://large.example/list?token=a%2Fb#servers",
                ],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("71.239.149.231", 12001),
                    game_id="rigsofrods",
                    name="Kipp County | CDL+",
                    password=True,
                    player_count=1,
                    player_limit=8,
                    game_type="rigsofrods",
                    terrain="any",
                    players=(Player("Kipp Bot"),),
                    rules={"verified": 1, "is_official": 0},
                )
            ],
        )
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://good.example/list?json=true",
                    deadline=mock.ANY,
                ),
                mock.call(
                    "https://large.example/list?token=a%2Fb&json=true#servers",
                    deadline=mock.ANY,
                ),
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Failed to read master https://large.example/<redacted>.",
            SafeDiagnostic("HTTP response exceeds 16777216 bytes"),
        ])

    def test_later_unknown_charset_failure_preserves_earlier_success(self):
        encoding_error = http_client.UnknownEncodingError(
            SafeDiagnostic("HTTP response uses an unknown character encoding")
        )
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    [CURRENT_RIGSOFRODS_IPV4_ENTRY],
                    encoding_error,
                ],
            ) as get_json,
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://good.example/list?json=true",
                    "https://charset.example/list#servers",
                ],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("71.239.149.231", 12001),
                    game_id="rigsofrods",
                    name="Kipp County | CDL+",
                    password=True,
                    player_count=1,
                    player_limit=8,
                    game_type="rigsofrods",
                    terrain="any",
                    players=(Player("Kipp Bot"),),
                    rules={"verified": 1, "is_official": 0},
                )
            ],
        )
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "https://good.example/list?json=true",
                    deadline=mock.ANY,
                ),
                mock.call(
                    "https://charset.example/list?json=true#servers",
                    deadline=mock.ANY,
                ),
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Failed to read master https://charset.example/<redacted>.",
            SafeDiagnostic("HTTP response uses an unknown character encoding"),
        ])

    def test_every_failed_master_raises_a_clear_adapter_error(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    requests.ConnectionError("offline"),
                    {"servers": []},
                    json.JSONDecodeError("Expecting value", "not-json", 0),
                ],
            ),
            mock.patch.object(helpers, "debug_msg"),
            self.assertRaisesRegex(
                SafeDiagnosticError,
                "^All configured master sources failed\\.$",
            ),
        ):
            rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://one.example/list",
                    "https://two.example/list",
                    "https://three.example/list",
                ],
            )

    def test_successful_empty_master_prevents_an_all_failed_error(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[requests.HTTPError("503 Server Error"), []],
            ),
            mock.patch.object(helpers, "debug_msg"),
            mock.patch.object(
                ping,
                "add_rtt_info",
                side_effect=AssertionError("adapter performed RTT enrichment"),
            ),
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                ["https://bad.example/list", "https://empty.example/list"],
            )

        self.assertEqual(servers, [])

    def test_malformed_entries_are_isolated_with_payload_free_diagnostics(self):
        malformed_mapping = {
            **CURRENT_RIGSOFRODS_IPV4_ENTRY,
            "name": "SECRET SERVER PAYLOAD",
            "port": "not-a-port",
        }
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value=[
                    CURRENT_RIGSOFRODS_IPV4_ENTRY,
                    malformed_mapping,
                    "SECRET NON-MAPPING PAYLOAD",
                    CURRENT_RIGSOFRODS_IPV6_ENTRY,
                ],
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                ["https://api.example/list"],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("71.239.149.231", 12001),
                    game_id="rigsofrods",
                    name="Kipp County | CDL+",
                    password=True,
                    player_count=1,
                    player_limit=8,
                    game_type="rigsofrods",
                    terrain="any",
                    players=(Player("Kipp Bot"),),
                    rules={"verified": 1, "is_official": 0},
                ),
                Server(
                    endpoint=Endpoint("2001:db8::20", 12000),
                    game_id="rigsofrods",
                    name="RoR IPv6",
                    player_count=2,
                    player_limit=24,
                    game_type="rigsofrods",
                    terrain="Desert",
                    players=(Player("Ada"), Player("Lin")),
                    rules={"verified": 0, "is_official": 1},
                ),
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Skipped malformed Rigs of Rods server entries: 2.",
        ])

    def test_all_malformed_decoded_list_raises_payload_free_adapter_error(self):
        with self.assertRaises(ValueError) as raised:
            rigsofrods.adapt_server_list(
                "rigsofrods",
                [{"name": "SECRET PAYLOAD"}, None],
            )

        self.assertEqual(
            str(raised.exception),
            "Rigs of Rods response contained no valid servers",
        )

    def test_gint_overflow_entry_is_skipped_while_valid_sibling_survives(self):
        overflowing_entry = {
            **CURRENT_RIGSOFRODS_IPV6_ENTRY,
            "max-clients": 2147483648,
        }
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = rigsofrods.adapt_server_list(
                "rigsofrods",
                [CURRENT_RIGSOFRODS_IPV4_ENTRY, overflowing_entry],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("71.239.149.231", 12001),
                    game_id="rigsofrods",
                    name="Kipp County | CDL+",
                    password=True,
                    player_count=1,
                    player_limit=8,
                    game_type="rigsofrods",
                    terrain="any",
                    players=(Player("Kipp Bot"),),
                    rules={"verified": 1, "is_official": 0},
                )
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Skipped malformed Rigs of Rods server entries: 1.",
        ])

    def test_all_malformed_master_is_skipped_before_later_valid_master(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    [{"name": "SECRET PAYLOAD"}, None],
                    [CURRENT_RIGSOFRODS_IPV4_ENTRY],
                ],
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            servers = rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://malformed.example/list",
                    "https://valid.example/list",
                ],
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("71.239.149.231", 12001),
                    game_id="rigsofrods",
                    name="Kipp County | CDL+",
                    password=True,
                    player_count=1,
                    player_limit=8,
                    game_type="rigsofrods",
                    terrain="any",
                    players=(Player("Kipp Bot"),),
                    rules={"verified": 1, "is_official": 0},
                )
            ],
        )
        debug_msg.assert_called_once_with([
            "Adapter/Rigs of Rods:",
            "Failed to read master https://malformed.example/<redacted>.",
            SafeDiagnostic("Malformed master payload."),
        ])

    def test_all_configured_masters_all_malformed_raise_all_failed_error(self):
        with (
            mock.patch(
                "obozrenie.http_client.get_json",
                side_effect=[
                    [{"name": "SECRET FIRST PAYLOAD"}, None],
                    [{"ip": "SECRET SECOND PAYLOAD"}],
                ],
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
            self.assertRaisesRegex(
                SafeDiagnosticError,
                "^All configured master sources failed\\.$",
            ),
        ):
            rigsofrods.stat_master(
                "rigsofrods",
                {"name": "Rigs of Rods"},
                [
                    "https://first.example/list",
                    "https://second.example/list",
                ],
            )

        self.assertEqual(
            debug_msg.call_args_list,
            [
                mock.call([
                    "Adapter/Rigs of Rods:",
                    "Failed to read master https://first.example/<redacted>.",
                    SafeDiagnostic("Malformed master payload."),
                ]),
                mock.call([
                    "Adapter/Rigs of Rods:",
                    "Failed to read master https://second.example/<redacted>.",
                    SafeDiagnostic("Malformed master payload."),
                ]),
            ],
        )


QSTAT_UP_SERVER_XML = """\
<server type="Q2S" address="[2001:db8::30]:27960" status="UP">
  <hostname>wrong.example</hostname>
  <name>^1Action Server</name>
  <gametype>teamplay</gametype>
  <map>urban</map>
  <numplayers>2</numplayers>
  <maxplayers>16</maxplayers>
  <ping>23</ping>
  <rules>
    <rule name="game">action</rule>
    <rule name="gamename">Action Quake 2</rule>
    <rule name="gam">wrong-mod</rule>
    <rule name="secure">1</rule>
    <rule name="sec">0</rule>
    <rule name="needpass">1</rule>
    <rule name="pass">0</rule>
  </rules>
  <players>
    <player><name>^2Ada</name><score>7</score><ping>12</ping></player>
    <player><name>Lin</name><score>-1</score><ping>42</ping></player>
  </players>
</server>
"""

QSTAT_UP_MASTER_XML = """\
<server type="Q2M" address="master.example:27900" status="UP" servers="1" />
"""

QSTAT_DOWN_SERVER_XML = """\
<server type="Q2S" address="down.example:27910" status="DOWN" />
"""

QSTAT_FILTERED_STATUSES_XML = """\
<qstat>
  <server type="Q2M" address="master.example:27900" servers="1" />
  <server type="Q2S" address="down.example:27910" status="DOWN" />
  <server type="Q2S" address="timeout.example:27911" status="TIMEOUT" />
  <server type="Q2S" address="error.example:27912" status="ERROR" />
</qstat>
"""

QSTAT_INVALID_STATUSES_XML = """\
<qstat>
  <server type="Q2S" address="unknown.example:28600" status="UNKNOWN" />
  <server type="Q2S" address="missing.example:28601" />
  <server type="Q2S" address="empty.example:28602" status="" />
  <server type="Q2S" address="lower-down.example:28603" status="down" />
  <server type="Q2S" address="title-timeout.example:28604" status="Timeout" />
  <server type="Q2S" address="near-error.example:28605" status="ERRORS" />
  <server type="Q2S" address="near-down.example:28606" status="DOWNSTREAM" />
  <server type="Q2S" address="near-up.example:28607" status="UP " />
</qstat>
"""

QSTAT_UNKNOWN_STATUS_SERVER_XML = """\
<server type="Q2S" address="unknown.example:28600" status="UNKNOWN" />
"""

QSTAT_EMPTY_TEXT_SERVER_XML = """\
<server type="Q2S" address="empty.example:27910" status="UP">
  <name />
  <gametype />
  <map />
  <numplayers>1</numplayers>
  <maxplayers>16</maxplayers>
  <ping>23</ping>
  <rules><rule name="game" /></rules>
  <players>
    <player><name /><score /><ping /></player>
  </players>
</server>
"""

QSTAT_SINGLE_SERVER_LIST_XML = """\
<qstat>
  <server type="Q2S" address="single.example:27910" status="UP">
    <hostname>ignored.example</hostname>
    <name>Single Server</name>
    <gametype>deathmatch</gametype>
    <map>warehouse</map>
    <numplayers>1</numplayers>
    <maxplayers>12</maxplayers>
    <ping>31</ping>
    <rules />
    <players>
      <player><name>Ada</name><score>4</score><ping>18</ping></player>
    </players>
  </server>
</qstat>
"""

QSTAT_OPTIONAL_SECTIONS_XML = """\
<qstat>
  <server type="Q2S" address="absent.example:27910" status="UP">
    <name>Absent Sections</name>
    <gametype>deathmatch</gametype>
    <map>warehouse</map>
    <numplayers>0</numplayers>
    <maxplayers>12</maxplayers>
    <ping>31</ping>
  </server>
  <server type="Q2S" address="empty-sections.example:27911" status="UP">
    <name>Empty Sections</name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
    <rules />
    <players />
  </server>
</qstat>
"""

QSTAT_MIXED_SERVER_LIST_XML = """\
<qstat>
  <server type="Q2M" address="master.example:27900" status="UP" servers="1" />
  <server type="Q2S" address="down.example:27910" status="DOWN" />
  <server type="Q2S" address="up.example:27910" status="UP">
    <hostname>ignored.example</hostname>
    <name>UP Server</name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
    <rules><rule name="gamename">Action Quake 2</rule></rules>
    <players />
  </server>
</qstat>
"""

QSTAT_MIXED_MALFORMED_TEXT_XML = """\
<qstat>
  <server type="Q2S" address="valid.example:27920" status="UP">
    <name>Valid Server</name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
  </server>
  <server type="Q2S" address="bad-name.example:27921" status="UP">
    <name><part>SECRET UNTRUSTED OUTPUT</part></name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
  </server>
  <server type="Q2S" address="bad-game-type.example:27922" status="UP">
    <name>Bad Game Type</name>
    <gametype><part>SECRET UNTRUSTED OUTPUT</part></gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
  </server>
  <server type="Q2S" address="bad-map.example:27923" status="UP">
    <name>Bad Map</name>
    <gametype>teamplay</gametype>
    <map><part>SECRET UNTRUSTED OUTPUT</part></map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
  </server>
  <server type="Q2S" address="bad-rule.example:27924" status="UP">
    <name>Bad Rule</name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>0</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
    <rules>
      <rule name="game"><part>SECRET UNTRUSTED OUTPUT</part></rule>
    </rules>
  </server>
  <server type="Q2S" address="bad-player.example:27925" status="UP">
    <name>Bad Player</name>
    <gametype>teamplay</gametype>
    <map>urban</map>
    <numplayers>1</numplayers>
    <maxplayers>16</maxplayers>
    <ping>27</ping>
    <players>
      <player>
        <name><part>SECRET UNTRUSTED OUTPUT</part></name>
        <score>1</score>
        <ping>10</ping>
      </player>
    </players>
  </server>
</qstat>
"""

QSTAT_RULE_ALIASES_XML = """\
<qstat>
  <server type="Q2S" address="punkbuster.example:28001" status="UP">
    <name>punkbuster</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="punkbuster">1</rule><rule name="punk">0</rule></rules>
  </server>
  <server type="Q2S" address="sv-punkbuster.example:28002" status="UP">
    <name>sv_punkbuster</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="sv_punkbuster">1</rule><rule name="sv_punk">0</rule></rules>
  </server>
  <server type="Q2S" address="secure.example:28003" status="UP">
    <name>secure</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="secure">1</rule><rule name="sec">0</rule></rules>
  </server>
  <server type="Q2S" address="g-needpass.example:28101" status="UP">
    <name>g_needpass</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="g_needpass">1</rule><rule name="g_need">0</rule></rules>
  </server>
  <server type="Q2S" address="needpass.example:28102" status="UP">
    <name>needpass</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="needpass">1</rule><rule name="need">0</rule></rules>
  </server>
  <server type="Q2S" address="si-usepass.example:28103" status="UP">
    <name>si_usepass</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="si_usepass">1</rule><rule name="usepass">0</rule></rules>
  </server>
  <server type="Q2S" address="pswrd.example:28104" status="UP">
    <name>pswrd</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="pswrd">1</rule><rule name="psw">0</rule></rules>
  </server>
  <server type="Q2S" address="password.example:28105" status="UP">
    <name>password</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="password">1</rule><rule name="pass">0</rule></rules>
  </server>
</qstat>
"""

QSTAT_EMPTY_BOOLEAN_RULES_XML = """\
<qstat>
  <server type="Q2S" address="empty-secure.example:28201" status="UP">
    <name>Empty Security</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules>
      <rule name="punkbuster" /><rule name="sv_punkbuster" />
      <rule name="secure" />
    </rules>
  </server>
  <server type="Q2S" address="empty-password.example:28202" status="UP">
    <name>Empty Password</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules>
      <rule name="g_needpass" /><rule name="needpass" />
      <rule name="si_usepass" /><rule name="pswrd" />
      <rule name="password" />
    </rules>
  </server>
</qstat>
"""

QSTAT_BOOLEAN_RULE_VALUES_XML = """\
<qstat>
  <server type="Q2S" address="numeric-true.example:28301" status="UP">
    <name>Numeric True</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="secure">2</rule><rule name="password">-3</rule></rules>
  </server>
  <server type="Q2S" address="numeric-false.example:28302" status="UP">
    <name>Numeric False</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules><rule name="sv_punkbuster">0</rule><rule name="needpass">0</rule></rules>
  </server>
  <server type="Q2S" address="nonnumeric.example:28303" status="UP">
    <name>Nonnumeric</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>16</maxplayers><ping>20</ping>
    <rules>
      <rule name="punkbuster">enabled</rule>
      <rule name="pswrd">true</rule>
    </rules>
  </server>
</qstat>
"""

QSTAT_REQUIRED_CAPACITY_XML = """\
<qstat>
  <server type="Q2S" address="zero.example:28400" status="UP">
    <name>Zero</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>0</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="missing-current.example:28401" status="UP">
    <name>Missing Current</name><gametype>dm</gametype><map>arena</map>
    <maxplayers>16</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="missing-limit.example:28402" status="UP">
    <name>Missing Limit</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="empty-current.example:28403" status="UP">
    <name>Empty Current</name><gametype>dm</gametype><map>arena</map>
    <numplayers /><maxplayers>16</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="empty-limit.example:28404" status="UP">
    <name>Empty Limit</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers /><ping>20</ping>
  </server>
  <server type="Q2S" address="text-current.example:28405" status="UP">
    <name>Text Current</name><gametype>dm</gametype><map>arena</map>
    <numplayers>many</numplayers><maxplayers>16</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="text-limit.example:28406" status="UP">
    <name>Text Limit</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>many</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="negative-current.example:28407" status="UP">
    <name>Negative Current</name><gametype>dm</gametype><map>arena</map>
    <numplayers>-1</numplayers><maxplayers>16</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="negative-limit.example:28408" status="UP">
    <name>Negative Limit</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers>-1</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="nested-current.example:28409" status="UP">
    <name>Nested Current</name><gametype>dm</gametype><map>arena</map>
    <numplayers><part>0</part></numplayers><maxplayers>16</maxplayers><ping>20</ping>
  </server>
  <server type="Q2S" address="nested-limit.example:28410" status="UP">
    <name>Nested Limit</name><gametype>dm</gametype><map>arena</map>
    <numplayers>0</numplayers><maxplayers><part>16</part></maxplayers><ping>20</ping>
  </server>
</qstat>
"""

QSTAT_MISSING_CAPACITY_ENTRY = """\
<server type="Q2S" address="malformed.example:28500" status="UP">
  <name>SECRET UNTRUSTED OUTPUT</name><gametype>dm</gametype><map>arena</map>
  <maxplayers>16</maxplayers><ping>20</ping>
</server>
"""

QSTAT_GINT_OVERFLOW_ENTRY = """\
<server type="Q2S" address="overflow.example:28501" status="UP">
  <name>Overflowing Ping</name><gametype>teamplay</gametype><map>urban</map>
  <numplayers>2</numplayers><maxplayers>16</maxplayers>
  <ping>2147483648</ping>
</server>
"""


class _TrackingSelector:
    def __init__(self, selector):
        self.selector = selector
        self.closed = False

    def register(self, fileobj, events):
        return self.selector.register(fileobj, events)

    def unregister(self, fileobj):
        return self.selector.unregister(fileobj)

    def select(self, timeout=None):
        return self.selector.select(timeout)

    def close(self):
        self.closed = True
        self.selector.close()


class _FaultingSelector:
    """A selector that fails every registration and records its close."""

    def __init__(self):
        self.closed = False

    def register(self, fileobj, events):
        raise OSError("injected selector setup fault")

    def close(self):
        self.closed = True


class UnboundedProcessWaitError(AssertionError):
    """Raised when a tracked child is waited on without a timeout."""


class _FakeChild:
    """A fake child handle that exits only when a test says so.

    QStat process ownership is tested without ever running a child: the
    handle publishes its exit on demand, records the stop signals it was
    sent, and rejects an unbounded wait outright.
    """

    def __init__(self, *, pipes=False):
        self.returncode = None
        self.signals = []
        self.wait_timeouts = []
        # Set when a thread other than this handle's creator observes the
        # child alive, which only the supervisor's reaper ever does.
        self.observed_by_reaper = threading.Event()
        self._owner = threading.current_thread()
        self._exit_code = None
        self.stdin = None
        self.stdout = None
        # QStat sends the child's stderr to DEVNULL, exactly as `Popen` does
        # for a stream it was not asked to pipe.
        self.stderr = None
        self._child_ends = ()
        if pipes:
            stdin_read, stdin_write = os.pipe()
            stdout_read, stdout_write = os.pipe()
            self.stdin = open(stdin_write, "wb", buffering=0)
            self.stdout = open(stdout_read, "rb", buffering=0)
            self._child_ends = (stdin_read, stdout_write)

    def poll(self):
        if self.returncode is None:
            if self._exit_code is None:
                if threading.current_thread() is not self._owner:
                    self.observed_by_reaper.set()
            else:
                self.returncode = self._exit_code
        return self.returncode

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if timeout is None:
            raise UnboundedProcessWaitError(
                "A QStat child was waited on without a timeout."
            )
        if self.poll() is None:
            raise subprocess.TimeoutExpired(["/opt/bin/qstat"], timeout)
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")

    def exit(self, returncode=0):
        """Publish an exit status for the next poll or wait to observe."""
        self._exit_code = returncode

    def close(self):
        """Release the pipes and let any reaper observe this child exit."""
        if self._exit_code is None:
            self._exit_code = -9
        for pipe in (self.stdin, self.stdout):
            if pipe is not None:
                pipe.close()
        for descriptor in self._child_ends:
            os.close(descriptor)
        self._child_ends = ()


class SpawnRegistrationFault(BaseException):
    """Raised in place of an interruption that hits a spawn mid-registration."""


class _FaultingLock:
    """A lock that raises one armed fault on its next acquire in a thread.

    Arming is per thread, so a supervisor's own reaper cannot consume the
    fault meant for the thread under test.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._faults = {}

    def fail_next_acquire(self, error):
        self._faults[threading.current_thread()] = error

    def acquire(self, *args, **kwargs):
        error = self._faults.pop(threading.current_thread(), None)
        if error is not None:
            raise error
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exception_info):
        self.release()


class _IdleWaitProbe:
    """Reports a supervisor thread waiting while nothing is pending.

    It stands in for the supervisor's own condition variable, so a reaper
    that spins instead of waiting never sets the event.
    """

    def __init__(self, supervisor):
        self._supervisor = supervisor
        self._condition = supervisor._idle
        self.waited_with_nothing_pending = threading.Event()

    def wait(self, timeout=None):
        # Both call sites hold the supervisor lock, so this read is safe.
        if not self._supervisor._pending:
            self.waited_with_nothing_pending.set()
        return self._condition.wait(timeout)

    def notify_all(self):
        return self._condition.notify_all()


@contextlib.contextmanager
def installed_qstat_supervisor():
    """Install one fresh QStat supervisor and close it within one second."""
    supervisor = qstat_processes.QStatProcessSupervisor()
    previous = qstat_processes.install_qstat_process_supervisor(supervisor)
    try:
        yield supervisor
    finally:
        try:
            supervisor.close_until(time.monotonic() + 1.0)
        finally:
            qstat_processes.install_qstat_process_supervisor(previous)


class QStatProcessSupervisorTests(unittest.TestCase):
    def make_supervisor(self, lock=None):
        """Build one supervisor, closed within one second after the test."""
        if lock is None:
            supervisor = qstat_processes.QStatProcessSupervisor()
        else:
            # The condition variable is built from this lock, so the lock has
            # to be in place before the supervisor exists.
            with mock.patch.object(
                qstat_processes.threading,
                "Lock",
                return_value=lock,
            ):
                supervisor = qstat_processes.QStatProcessSupervisor()

        def close():
            supervisor.close_until(time.monotonic() + 1.0)

        self.addCleanup(close)
        return supervisor

    def probe_idle_waits(self, supervisor):
        """Watch a supervisor's idle waits; install before its reaper runs."""
        probe = _IdleWaitProbe(supervisor)
        supervisor._idle = probe
        return probe

    def make_child(self, **kwargs):
        """Build one fake child handle and close its pipes after the test."""
        child = _FakeChild(**kwargs)
        self.addCleanup(child.close)
        return child

    def assert_finite_waits(self, timeouts):
        """Assert every observed wait on a child carried a finite bound."""
        self.assertTrue(timeouts)
        for timeout in timeouts:
            self.assertIsNotNone(timeout)
            self.assertTrue(math.isfinite(timeout))
            self.assertGreaterEqual(timeout, 0)

    def run_failing_qstat(self, supervisor, child, *, budget=1.0):
        """Drive one bounded `_run_qstat` failure against a fake child."""
        selector = _FaultingSelector()
        started = time.monotonic()
        with (
            mock.patch.object(
                qstat.selectors,
                "DefaultSelector",
                return_value=selector,
            ),
            mock.patch.object(
                qstat_processes.subprocess,
                "Popen",
                return_value=child,
            ),
            self.assertRaisesRegex(OSError, r"^injected selector setup fault$"),
        ):
            qstat._run_qstat(
                ["/opt/bin/qstat"],
                b"x",
                supervisor=supervisor,
                deadline=time.monotonic() + budget,
                max_output_bytes=1024,
            )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(selector.closed)
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)

    def test_spawn_racing_begin_close_registers_then_kills_the_child(self):
        supervisor = self.make_supervisor()
        child = self.make_child(pipes=True)
        spawning = threading.Event()
        resume = threading.Event()
        outcomes = []

        def blocked_popen(command, **popen_kwargs):
            spawning.set()
            resume.wait(1)
            return child

        def spawn():
            outcomes.append(
                _capture_value_or_error(
                    lambda: supervisor.spawn(["/opt/bin/qstat"])
                )
            )

        caller = threading.Thread(target=spawn, name="test-qstat-spawn")
        try:
            with mock.patch.object(
                qstat_processes.subprocess,
                "Popen",
                side_effect=blocked_popen,
            ):
                caller.start()
                self.assertTrue(spawning.wait(1))
                supervisor.begin_close()
                resume.set()
                caller.join(1)
        finally:
            resume.set()
            caller.join(1)

        self.assertFalse(caller.is_alive())
        succeeded, error = outcomes[0]
        self.assertFalse(succeeded)
        self.assertIsInstance(error, qstat_processes.QStatSupervisorClosedError)
        # The child was born after close swept the registry: the loser of
        # that race kills its own handle and keeps it rather than losing it.
        self.assertEqual(child.signals, ["kill"])
        self.assertEqual(supervisor.registered_processes, (child,))
        # Nobody else can close the pipes of a child no caller ever received.
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.observed_by_reaper.wait(1))
        self.assertEqual(supervisor.registered_processes, (child,))

        child.exit(-9)
        supervisor.join_until(time.monotonic() + 1.0)
        self.assertEqual(supervisor.registered_processes, ())
        self.assert_finite_waits(child.wait_timeouts)

    def test_failure_cleanup_terminates_then_kills_using_only_finite_waits(self):
        with installed_qstat_supervisor() as supervisor:
            child = self.make_child(pipes=True)
            self.run_failing_qstat(supervisor, child)

            self.assertEqual(child.signals, ["terminate", "kill"])
            # Both cleanup waits ran before the reaper took the handle, so
            # the first two timeouts are the query's own.
            self.assert_finite_waits(list(child.wait_timeouts)[:2])
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(-9)

        self.assertEqual(supervisor.registered_processes, ())

    def test_failure_cleanup_grace_before_the_kill_is_short_and_fixed(self):
        with installed_qstat_supervisor() as supervisor:
            child = self.make_child(pipes=True)
            self.run_failing_qstat(supervisor, child, budget=60.0)

            self.assertEqual(child.signals, ["terminate", "kill"])
            grace, kill_wait = list(child.wait_timeouts)[:2]
            # A failure that is not the deadline's own still stops the child
            # promptly: the grace period before the kill is fixed and short
            # rather than everything the operation deadline has left.
            self.assertEqual(grace, qstat.QSTAT_TERMINATE_GRACE_SECONDS)
            self.assertGreater(kill_wait, grace)
            self.assertLessEqual(kill_wait, 60.0)
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(-9)

        self.assertEqual(supervisor.registered_processes, ())

    def test_failing_cleanup_report_cannot_replace_the_query_failure(self):
        def failing_terminate():
            raise RuntimeError("child stop failed")

        def failing_debug_msg(_message):
            raise RuntimeError("diagnostic reporting failed")

        with installed_qstat_supervisor() as supervisor:
            child = self.make_child(pipes=True)
            child.terminate = failing_terminate
            with mock.patch.object(
                qstat.helpers, "debug_msg", failing_debug_msg
            ):
                # Reporting a failed stop is best effort: its own failure
                # must not replace the selector fault propagating through.
                self.run_failing_qstat(supervisor, child)

            self.assertEqual(child.signals, [])
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(-9)

        self.assertEqual(supervisor.registered_processes, ())

    def test_failing_child_stop_still_closes_pipes_and_keeps_the_failure(self):
        stop_error = RuntimeError("child stop failed")
        messages = []

        def failing_terminate():
            raise stop_error

        with installed_qstat_supervisor() as supervisor:
            child = self.make_child(pipes=True)
            child.terminate = failing_terminate
            with mock.patch.object(
                qstat.helpers, "debug_msg", messages.append
            ):
                # The selector fault is the query's own failure: a stop that
                # fails while it propagates neither replaces it nor skips the
                # pipe and selector closes `run_failing_qstat` asserts.
                self.run_failing_qstat(supervisor, child)

            self.assertEqual(child.signals, [])
            # A child whose stop failed is still owned, and close reaps it.
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(-9)

        self.assertEqual(supervisor.registered_processes, ())
        self.assertEqual(
            [str(message[-1]) for message in messages],
            ["Unexpected RuntimeError; exception details omitted."],
        )

    def test_unreaped_child_remains_registered_after_query_returns(self):
        with installed_qstat_supervisor() as supervisor:
            child = self.make_child(pipes=True)
            self.run_failing_qstat(supervisor, child)

            # The query returned inside its bound while the child lives on.
            self.assertIsNone(child.returncode)
            self.assertEqual(supervisor.registered_processes, (child,))
            self.assertTrue(child.observed_by_reaper.wait(1))
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(0)

        self.assertEqual(supervisor.registered_processes, ())

    def test_daemon_reaper_releases_handle_only_after_observed_exit(self):
        supervisor = self.make_supervisor()
        child = self.make_child()
        supervisor.defer_reap(child)
        try:
            self.assertTrue(child.observed_by_reaper.wait(1))
            # However often it is observed, a living child stays registered.
            self.assertEqual(supervisor.registered_processes, (child,))
            child.exit(0)
        finally:
            supervisor.begin_close()
            supervisor.join_until(time.monotonic() + 1.0)

        self.assertEqual(supervisor.registered_processes, ())
        # The one daemon reaper stops once every handle has been released.
        self.assertFalse(supervisor._reaper.is_alive())
        self.assert_finite_waits(child.wait_timeouts)

    def test_begin_close_kills_all_children_and_join_is_deadline_bounded(self):
        supervisor = self.make_supervisor()
        first = self.make_child()
        second = self.make_child()
        try:
            with mock.patch.object(
                qstat_processes.subprocess,
                "Popen",
                side_effect=[first, second],
            ):
                self.assertIs(supervisor.spawn(["/opt/bin/qstat", "-a"]), first)
                self.assertIs(supervisor.spawn(["/opt/bin/qstat", "-b"]), second)
            self.assertEqual(set(supervisor.registered_processes), {first, second})

            supervisor.begin_close()
            self.assertEqual(first.signals, ["kill"])
            self.assertEqual(second.signals, ["kill"])
            supervisor.begin_close()
            self.assertEqual(first.signals, ["kill"])
            self.assertEqual(second.signals, ["kill"])
            with self.assertRaises(qstat_processes.QStatSupervisorClosedError):
                supervisor.spawn(["/opt/bin/qstat", "-c"])

            # A child that will not die bounds the join at the deadline
            # instead of holding the application open.
            bound = 0.25
            started = time.monotonic()
            supervisor.join_until(time.monotonic() + bound)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, bound / 2)
            self.assertLess(elapsed, 1.0)
            self.assertEqual(set(supervisor.registered_processes), {first, second})

            # The deadline is validated before any handle is read.
            with self.assertRaises(TypeError):
                supervisor.join_until(1)
            with self.assertRaises(ValueError):
                supervisor.join_until(float("inf"))
        finally:
            first.exit(0)
            second.exit(-9)
            supervisor.join_until(time.monotonic() + 1.0)

        self.assertEqual(supervisor.registered_processes, ())
        self.assertFalse(supervisor._reaper.is_alive())

    def test_reaper_waits_instead_of_spinning_while_a_spawn_races_close(self):
        supervisor = self.make_supervisor()
        probe = self.probe_idle_waits(supervisor)
        retained = self.make_child()
        spawned = self.make_child()
        spawning = threading.Event()
        resume = threading.Event()
        outcomes = []

        def blocked_popen(command, **popen_kwargs):
            spawning.set()
            resume.wait(1)
            return spawned

        def spawn():
            outcomes.append(
                _capture_value_or_error(
                    lambda: supervisor.spawn(["/opt/bin/qstat"])
                )
            )

        supervisor.defer_reap(retained)
        self.assertTrue(retained.observed_by_reaper.wait(1))
        caller = threading.Thread(target=spawn, name="test-qstat-spawn")
        try:
            with mock.patch.object(
                qstat_processes.subprocess,
                "Popen",
                side_effect=blocked_popen,
            ):
                caller.start()
                self.assertTrue(spawning.wait(1))
                supervisor.begin_close()
                retained.exit(0)
                # Nothing is left to reap, yet the blocked spawn can still
                # deliver a child, so the reaper must wait rather than spin.
                self.assertTrue(probe.waited_with_nothing_pending.wait(1))
                resume.set()
                caller.join(1)
        finally:
            resume.set()
            caller.join(1)

        self.assertFalse(caller.is_alive())
        succeeded, error = outcomes[0]
        self.assertFalse(succeeded)
        self.assertIsInstance(error, qstat_processes.QStatSupervisorClosedError)
        self.assertEqual(supervisor.registered_processes, (spawned,))

        spawned.exit(-9)
        supervisor.join_until(time.monotonic() + 1.0)
        self.assertEqual(supervisor.registered_processes, ())
        self.assertFalse(supervisor._reaper.is_alive())

    def test_spawn_interrupted_before_registration_kills_its_own_child(self):
        lock = _FaultingLock()
        supervisor = self.make_supervisor(lock=lock)
        retained = self.make_child()
        child = self.make_child()
        fault = SpawnRegistrationFault("injected registration fault")

        def faulting_popen(command, **popen_kwargs):
            # Fail this caller's next lock acquisition: the child exists, but
            # its spawn never reaches the registration that follows.
            lock.fail_next_acquire(fault)
            return child

        supervisor.defer_reap(retained)
        self.assertTrue(retained.observed_by_reaper.wait(1))
        with (
            mock.patch.object(
                qstat_processes.subprocess,
                "Popen",
                side_effect=faulting_popen,
            ),
            self.assertRaises(SpawnRegistrationFault),
        ):
            supervisor.spawn(["/opt/bin/qstat"])

        # No caller ever received this handle, so the supervisor adopts it:
        # registered for reaping and killed, never lost while it lives.
        self.assertEqual(
            set(supervisor.registered_processes),
            {retained, child},
        )
        self.assertEqual(child.signals, ["kill"])

        child.exit(-9)
        retained.exit(0)
        supervisor.begin_close()
        started = time.monotonic()
        supervisor.join_until(time.monotonic() + 1.0)
        # A leaked reservation would hold this reaper past every join it is
        # ever given, because no spawn can ever resolve it.
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertFalse(supervisor._reaper.is_alive())
        self.assertEqual(supervisor.registered_processes, ())


def _track_stop_signal(process, name, signals):
    """Record one stop signal by name before sending it."""
    send_signal = getattr(process, name)

    def track():
        signals.append(name)
        return send_signal()

    setattr(process, name, track)


class QStatAdapterTests(unittest.TestCase):
    @staticmethod
    def tracked_popen(
        processes,
        calls=None,
        signals=None,
        wait_timeouts=None,
        wait_pipe_states=None,
    ):
        """Track one child's spawn arguments, stop signals, and waits.

        Every tracked child rejects `wait(timeout=None)`: an unbounded wait
        on a child that ignores its stop signal is exactly the defect this
        adapter must never reintroduce, so it fails loudly instead.
        """
        real_popen = subprocess.Popen

        def start_process(*args, **kwargs):
            if calls is not None:
                calls.append((args, kwargs.copy()))
            process = real_popen(*args, **kwargs)
            if signals is not None:
                for name in ("terminate", "kill"):
                    _track_stop_signal(process, name, signals)
            real_wait = process.wait

            def track_wait(timeout=None):
                if timeout is None:
                    raise UnboundedProcessWaitError(
                        "A QStat child was waited on without a timeout."
                    )
                if wait_timeouts is not None:
                    wait_timeouts.append(timeout)
                if wait_pipe_states is not None:
                    wait_pipe_states.append(
                        (process.stdin.closed, process.stdout.closed)
                    )
                return real_wait(timeout=timeout)

            process.wait = track_wait
            processes.append(process)
            return process

        return start_process

    def qstat_supervisor(self):
        """Install one fresh supervisor for this test and close it after."""
        return self.enterContext(installed_qstat_supervisor())

    def assert_child_stopped(self, signals):
        """Assert one failed child was terminated, and killed only if needed."""
        self.assertIn(signals, (["terminate"], ["terminate", "kill"]))

    def assert_qstat_process_cleaned(self, supervisor, process):
        """Assert the child is reaped, at the latest by supervisor close.

        A child that outlives the operation deadline is handed to the
        supervisor's reaper, so proving it was reaped means closing the
        supervisor against its own bound first.
        """
        supervisor.close_until(time.monotonic() + 1.0)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def assert_qstat_adapter_error(self, outcome, expected_pattern):
        runner_options = (
            {"side_effect": outcome}
            if isinstance(outcome, BaseException)
            else {"return_value": outcome}
        )
        with (
            mock.patch.object(qstat, "find_qstat", return_value="/opt/bin/qstat"),
            mock.patch.object(qstat, "_run_qstat", **runner_options),
            self.assertRaisesRegex(RuntimeError, expected_pattern) as raised,
        ):
            qstat.stat_master(
                "q2",
                {"name": "Quake II"},
                ["master://master.example:27900"],
            )

        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        # Core logs a declared diagnostic instead of stringifying a failure,
        # so a user-actionable text only survives as a declared one.
        self.assertIs(type(raised.exception), qstat.QStatQueryError)
        self.assertEqual(
            str(diagnostic_for_exception(raised.exception)),
            str(raised.exception),
        )

    def test_find_qstat_prefers_qstat_without_checking_fallback(self):
        with mock.patch("shutil.which", return_value="/opt/bin/qstat") as which:
            self.assertEqual(qstat.find_qstat(), "/opt/bin/qstat")

        which.assert_called_once_with("qstat")

    def test_find_qstat_checks_quakestat_second(self):
        with mock.patch(
            "shutil.which",
            side_effect=(None, "/opt/bin/quakestat"),
        ) as which:
            self.assertEqual(qstat.find_qstat(), "/opt/bin/quakestat")

        self.assertEqual(
            which.call_args_list,
            [mock.call("qstat"), mock.call("quakestat")],
        )

    def test_find_qstat_names_both_installable_executables_when_missing(self):
        with (
            mock.patch("shutil.which", return_value=None) as which,
            self.assertRaisesRegex(
                FileNotFoundError,
                r"^Install QStat \(qstat or quakestat\) to query this game\.$",
            ) as raised,
        ):
            qstat.find_qstat()

        self.assertIs(type(raised.exception), qstat.QStatUnavailableError)
        self.assertEqual(
            str(diagnostic_for_exception(raised.exception)),
            "Install QStat (qstat or quakestat) to query this game.",
        )
        self.assertEqual(
            which.call_args_list,
            [mock.call("qstat"), mock.call("quakestat")],
        )

    def test_server_endpoint_comes_from_port_bearing_address(self):
        entry = xmltodict.parse(QSTAT_UP_SERVER_XML)["server"]

        server = qstat.adapt_server_entry(entry, "q2")

        self.assertIsInstance(server, Server)
        self.assertEqual(server.endpoint, Endpoint("2001:db8::30", 27960))
        self.assertEqual(str(server.endpoint), "[2001:db8::30]:27960")

    def test_game_mod_uses_only_the_exact_game_rule(self):
        entry = xmltodict.parse(QSTAT_UP_SERVER_XML)["server"]

        server = qstat.adapt_server_entry(entry, "q2")

        self.assertEqual(server.game_mod, "action")
        self.assertEqual(server.rules["gamename"], "Action Quake 2")
        self.assertEqual(server.rules["gam"], "wrong-mod")

    def test_master_and_down_server_entries_return_none(self):
        master_entry = xmltodict.parse(QSTAT_UP_MASTER_XML)["server"]
        down_entry = xmltodict.parse(QSTAT_DOWN_SERVER_XML)["server"]

        self.assertIsNone(
            qstat.adapt_qstat_entry(master_entry, "q2", "Q2M", "Q2S")
        )
        self.assertIsNone(
            qstat.adapt_qstat_entry(down_entry, "q2", "Q2M", "Q2S")
        )

    def test_players_from_literal_xml_are_player_records(self):
        entry = xmltodict.parse(QSTAT_UP_SERVER_XML)["server"]

        server = qstat.adapt_server_entry(entry, "q2")

        self.assertEqual(
            server.players,
            (Player("Ada", score=7, ping=12), Player("Lin", score=-1, ping=42)),
        )

    def test_empty_xml_text_normalizes_to_empty_strings(self):
        entry = xmltodict.parse(QSTAT_EMPTY_TEXT_SERVER_XML)["server"]

        server = qstat.adapt_server_entry(entry, "q2")

        self.assertEqual(
            server,
            Server(
                endpoint=Endpoint("empty.example", 27910),
                game_id="q2",
                player_count=1,
                player_limit=16,
                ping=23,
                players=(Player(""),),
                rules={"game": ""},
            ),
        )

    def test_security_and_password_rules_require_exact_names(self):
        entry = xmltodict.parse(QSTAT_UP_SERVER_XML)["server"]

        server = qstat.adapt_server_entry(entry, "q2")

        self.assertIs(server.secure, True)
        self.assertIs(server.password, True)
        self.assertEqual(server.rules["sec"], "0")
        self.assertEqual(server.rules["pass"], "0")

    def test_every_security_and_password_alias_requires_exact_membership(self):
        servers = qstat.adapt_server_list(
            QSTAT_RULE_ALIASES_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            None,
            None,
        )

        self.assertEqual(
            [
                (server.endpoint.host, server.secure, server.password)
                for server in servers
            ],
            [
                ("punkbuster.example", True, False),
                ("sv-punkbuster.example", True, False),
                ("secure.example", True, False),
                ("g-needpass.example", False, True),
                ("needpass.example", False, True),
                ("si-usepass.example", False, True),
                ("pswrd.example", False, True),
                ("password.example", False, True),
            ],
        )

    def test_empty_security_and_password_aliases_preserve_false_servers(self):
        servers = qstat.adapt_server_list(
            QSTAT_EMPTY_BOOLEAN_RULES_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            None,
            None,
        )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("empty-secure.example", 28201),
                    game_id="q2",
                    name="Empty Security",
                    player_limit=16,
                    ping=20,
                    game_type="dm",
                    terrain="arena",
                    rules={
                        "punkbuster": "",
                        "sv_punkbuster": "",
                        "secure": "",
                    },
                ),
                Server(
                    endpoint=Endpoint("empty-password.example", 28202),
                    game_id="q2",
                    name="Empty Password",
                    player_limit=16,
                    ping=20,
                    game_type="dm",
                    terrain="arena",
                    rules={
                        "g_needpass": "",
                        "needpass": "",
                        "si_usepass": "",
                        "pswrd": "",
                        "password": "",
                    },
                ),
            ],
        )

    def test_boolean_alias_values_use_integer_truth_without_text_truthiness(self):
        servers = qstat.adapt_server_list(
            QSTAT_BOOLEAN_RULE_VALUES_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            None,
            None,
        )

        self.assertEqual(
            [
                (server.endpoint.host, server.secure, server.password)
                for server in servers
            ],
            [
                ("numeric-true.example", True, True),
                ("numeric-false.example", False, False),
                ("nonnumeric.example", False, False),
            ],
        )

    def test_required_capacity_rejects_missing_empty_and_malformed_values(self):
        with mock.patch.object(helpers, "debug_msg"):
            servers = qstat.adapt_server_list(
                QSTAT_REQUIRED_CAPACITY_XML,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("zero.example", 28400),
                    game_id="q2",
                    name="Zero",
                    ping=20,
                    game_type="dm",
                    terrain="arena",
                )
            ],
        )

    def test_high_count_mixed_response_logs_one_malformed_summary(self):
        qstat_xml = (
            "<qstat>"
            + QSTAT_UP_MASTER_XML
            + QSTAT_DOWN_SERVER_XML
            + QSTAT_UP_SERVER_XML
            + QSTAT_MISSING_CAPACITY_ENTRY * 257
            + "</qstat>"
        )
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].endpoint, Endpoint("2001:db8::30", 27960))
        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 257.",
            ]
        )
        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(debug_msg.call_args))

    def test_all_malformed_response_logs_once_and_raises_exact_error(self):
        qstat_xml = (
            "<qstat>"
            + QSTAT_MISSING_CAPACITY_ENTRY
            + QSTAT_MISSING_CAPACITY_ENTRY
            + "</qstat>"
        )
        with (
            mock.patch.object(helpers, "debug_msg") as debug_msg,
            self.assertRaisesRegex(
                ValueError,
                r"^QStat response contained no valid server entries\.$",
            ) as raised,
        ):
            qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 2.",
            ]
        )
        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(raised.exception))
        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(debug_msg.call_args))

    def test_gint_overflow_entry_is_skipped_while_valid_sibling_survives(self):
        qstat_xml = (
            "<qstat>"
            + QSTAT_GINT_OVERFLOW_ENTRY
            + QSTAT_UP_SERVER_XML
            + "</qstat>"
        )
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].endpoint, Endpoint("2001:db8::30", 27960))
        self.assertEqual(servers[0].ping, 23)
        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 1.",
            ]
        )

    def test_master_and_down_only_response_is_valid_empty_without_diagnostics(self):
        qstat_xml = "<qstat>" + QSTAT_UP_MASTER_XML + QSTAT_DOWN_SERVER_XML + "</qstat>"
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(servers, [])
        debug_msg.assert_not_called()

    def test_master_and_exact_filtered_statuses_are_valid_empty(self):
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                QSTAT_FILTERED_STATUSES_XML,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(servers, [])
        debug_msg.assert_not_called()

    def test_all_invalid_statuses_log_once_and_raise_exact_error(self):
        with (
            mock.patch.object(helpers, "debug_msg") as debug_msg,
            self.assertRaisesRegex(
                ValueError,
                r"^QStat response contained no valid server entries\.$",
            ),
        ):
            qstat.adapt_server_list(
                QSTAT_INVALID_STATUSES_XML,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 8.",
            ]
        )

    def test_mixed_valid_and_unknown_status_logs_one_summary(self):
        qstat_xml = (
            "<qstat>"
            + QSTAT_UP_SERVER_XML
            + QSTAT_UNKNOWN_STATUS_SERVER_XML
            + "</qstat>"
        )
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].endpoint, Endpoint("2001:db8::30", 27960))
        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 1.",
            ]
        )

    def test_unknown_entry_type_is_malformed(self):
        qstat_xml = '<qstat><server type="UNEXPECTED" status="UP" /></qstat>'
        with (
            mock.patch.object(helpers, "debug_msg") as debug_msg,
            self.assertRaisesRegex(
                ValueError,
                r"^QStat response contained no valid server entries\.$",
            ),
        ):
            qstat.adapt_server_list(
                qstat_xml,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 1.",
            ]
        )

    def test_single_server_xml_shape_returns_one_server(self):
        servers = qstat.adapt_server_list(
            QSTAT_SINGLE_SERVER_LIST_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            None,
            None,
        )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("single.example", 27910),
                    game_id="q2",
                    name="Single Server",
                    player_count=1,
                    player_limit=12,
                    ping=31,
                    game_type="deathmatch",
                    terrain="warehouse",
                    players=(Player("Ada", score=4, ping=18),),
                )
            ],
        )

    def test_absent_and_empty_optional_sections_preserve_up_servers(self):
        servers = qstat.adapt_server_list(
            QSTAT_OPTIONAL_SECTIONS_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            None,
            None,
        )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("absent.example", 27910),
                    game_id="q2",
                    name="Absent Sections",
                    player_limit=12,
                    ping=31,
                    game_type="deathmatch",
                    terrain="warehouse",
                ),
                Server(
                    endpoint=Endpoint("empty-sections.example", 27911),
                    game_id="q2",
                    name="Empty Sections",
                    player_limit=16,
                    ping=27,
                    game_type="teamplay",
                    terrain="urban",
                ),
            ],
        )

    def test_list_server_xml_shape_skips_master_and_down_entries(self):
        servers = qstat.adapt_server_list(
            QSTAT_MIXED_SERVER_LIST_XML,
            "q2",
            "Quake II",
            "Q2M",
            "Q2S",
            "Action Quake 2",
            "teamplay",
        )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("up.example", 27910),
                    game_id="q2",
                    name="UP Server",
                    player_limit=16,
                    ping=27,
                    game_type="teamplay",
                    terrain="urban",
                    rules={"gamename": "Action Quake 2"},
                )
            ],
        )

    def test_empty_qstat_document_shapes_return_empty_lists(self):
        for label, qstat_xml in (
            ("none root", "<qstat />"),
            ("missing server", "<qstat><meta /></qstat>"),
            ("none server", "<qstat><server /></qstat>"),
        ):
            with self.subTest(label=label):
                servers = qstat.adapt_server_list(
                    qstat_xml,
                    "q2",
                    "Quake II",
                    "Q2M",
                    "Q2S",
                    None,
                    None,
                )

                self.assertEqual(servers, [])

    def test_missing_qstat_root_raises_payload_free_invalid_response_error(self):
        with self.assertRaisesRegex(
            ValueError,
            r"^QStat response must contain a qstat root$",
        ) as raised:
            qstat.adapt_server_list(
                "<other />",
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertNotIn("other", str(raised.exception))

    def test_mixed_malformed_text_keeps_valid_server_without_payload_logs(self):
        with mock.patch.object(helpers, "debug_msg") as debug_msg:
            servers = qstat.adapt_server_list(
                QSTAT_MIXED_MALFORMED_TEXT_XML,
                "q2",
                "Quake II",
                "Q2M",
                "Q2S",
                None,
                None,
            )

        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("valid.example", 27920),
                    game_id="q2",
                    name="Valid Server",
                    player_limit=16,
                    ping=27,
                    game_type="teamplay",
                    terrain="urban",
                )
            ],
        )
        debug_msg.assert_called_once_with(
            [
                "Adapter/QStat",
                "Quake II",
                "Skipped malformed QStat server entries: 5.",
            ]
        )
        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(debug_msg.call_args))

    def test_run_qstat_accepts_output_at_exact_limit(self):
        supervisor = self.qstat_supervisor()
        limit = 16 * 1024 * 1024
        self.assertEqual(qstat.QSTAT_MAX_OUTPUT_BYTES, limit)
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'x' * int(sys.argv[1])); "
            "sys.stdout.buffer.flush()"
        )

        output = qstat._run_qstat(
            [sys.executable, "-c", script, str(limit)],
            b"",
            supervisor=supervisor,
            deadline=time.monotonic() + 5,
        )

        self.assertEqual(len(output), limit)
        self.assertEqual((output[:1], output[-1:]), (b"x", b"x"))

    def test_run_qstat_opens_exact_binary_pipes_and_discards_stderr(self):
        supervisor = self.qstat_supervisor()
        command = [
            sys.executable,
            "-c",
            (
                "import sys; "
                "data = sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data[::-1])"
            ),
        ]
        processes = []
        calls = []

        with mock.patch.object(
            qstat.subprocess,
            "Popen",
            side_effect=self.tracked_popen(processes, calls=calls),
        ):
            output = qstat._run_qstat(
                command,
                b"binary input",
                supervisor=supervisor,
                deadline=time.monotonic() + 5,
                max_output_bytes=1024,
            )

        self.assertEqual(output, b"tupni yranib")
        self.assertEqual(
            calls,
            [
                (
                    (command,),
                    {
                        "stdin": subprocess.PIPE,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.DEVNULL,
                        "bufsize": 0,
                    },
                )
            ],
        )
        self.assertEqual(len(processes), 1)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_concurrently_drains_stdout_before_child_reads_stdin(self):
        supervisor = self.qstat_supervisor()
        size = 4 * 1024 * 1024
        script = """
import os
import sys

size = int(sys.argv[1])
chunk = b"o" * (64 * 1024)
remaining = size
while remaining:
    written = os.write(1, chunk[:remaining])
    remaining -= written

received = 0
while received < size:
    data = os.read(0, min(64 * 1024, size - received))
    if not data:
        break
    received += len(data)
os.write(1, b"OK" if received == size else b"BAD")
"""

        output = qstat._run_qstat(
            [sys.executable, "-c", script, str(size)],
            b"i" * size,
            supervisor=supervisor,
            deadline=time.monotonic() + 5,
            max_output_bytes=size + 3,
        )

        self.assertEqual(len(output), size + 2)
        self.assertEqual((output[:1], output[-2:]), (b"o", b"OK"))

    def test_run_qstat_kills_over_limit_child_without_waiting_for_shutdown(self):
        supervisor = self.qstat_supervisor()
        limit = 16 * 1024 * 1024
        self.assertEqual(qstat.QSTAT_MAX_OUTPUT_BYTES, limit)
        script = (
            "import sys, time; "
            "sys.stdout.buffer.write(b'x' * (int(sys.argv[1]) + 1)); "
            "sys.stdout.buffer.flush(); "
            "time.sleep(10)"
        )
        started = time.monotonic()
        processes = []

        with (
            mock.patch.object(
                qstat.subprocess,
                "Popen",
                side_effect=self.tracked_popen(processes),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                rf"^QStat output exceeds {limit} bytes\.$",
            ) as raised,
        ):
            qstat._run_qstat(
                [sys.executable, "-c", script, str(limit)],
                b"",
                supervisor=supervisor,
                deadline=time.monotonic() + 5,
            )

        self.assertLess(time.monotonic() - started, 2)
        self.assertIs(type(raised.exception), qstat.QStatQueryError)
        self.assertEqual(
            str(diagnostic_for_exception(raised.exception)),
            f"QStat output exceeds {limit} bytes.",
        )
        self.assertEqual(len(processes), 1)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_writes_stdin_while_discarding_large_stderr(self):
        supervisor = self.qstat_supervisor()
        payload = b"Q2M master.example:27900\n" * 10_000
        script = (
            "import sys; "
            "sys.stderr.buffer.write(b'e' * (1024 * 1024)); "
            "sys.stderr.buffer.flush(); "
            "data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(data); "
            "sys.stdout.buffer.flush()"
        )

        output = qstat._run_qstat(
            [sys.executable, "-c", script],
            payload,
            supervisor=supervisor,
            deadline=time.monotonic() + 5,
            max_output_bytes=1024 * 1024,
        )

        self.assertEqual(output, payload)

    def test_run_qstat_drains_stdout_after_child_closes_stdin_early(self):
        supervisor = self.qstat_supervisor()
        output_size = 2 * 1024 * 1024
        script = """
import os
import sys

os.close(0)
remaining = int(sys.argv[1])
chunk = b"o" * (64 * 1024)
while remaining:
    written = os.write(1, chunk[:remaining])
    remaining -= written
"""
        processes = []
        signals = []

        with mock.patch.object(
            qstat.subprocess,
            "Popen",
            side_effect=self.tracked_popen(processes, signals=signals),
        ):
            output = qstat._run_qstat(
                [sys.executable, "-c", script, str(output_size)],
                b"i" * (4 * 1024 * 1024),
                supervisor=supervisor,
                deadline=time.monotonic() + 5,
                max_output_bytes=output_size,
            )

        self.assertEqual(len(output), output_size)
        self.assertEqual((output[:1], output[-1:]), (b"o", b"o"))
        self.assertEqual(signals, [])
        self.assertEqual(len(processes), 1)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_finishes_stdin_after_stdout_closes(self):
        supervisor = self.qstat_supervisor()
        payload = b"x" * (1024 * 1024)
        script = (
            "import os, sys; "
            "os.close(1); "
            "data = sys.stdin.buffer.read(); "
            "os._exit(0 if len(data) == int(sys.argv[1]) else 3)"
        )

        output = qstat._run_qstat(
            [sys.executable, "-c", script, str(len(payload))],
            payload,
            supervisor=supervisor,
            deadline=time.monotonic() + 5,
            max_output_bytes=1024,
        )

        self.assertEqual(output, b"")

    def test_run_qstat_reports_nonzero_without_child_output(self):
        supervisor = self.qstat_supervisor()
        script = (
            "import sys; "
            "payload = bytes.fromhex('53454352455420554e54525553544544204f5554505554'); "
            "sys.stdout.buffer.write(payload); "
            "sys.stderr.buffer.write(payload); "
            "sys.exit(7)"
        )

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            qstat._run_qstat(
                [sys.executable, "-c", script],
                b"",
                supervisor=supervisor,
                deadline=time.monotonic() + 5,
                max_output_bytes=1024,
            )

        self.assertEqual(raised.exception.returncode, 7)
        self.assertNotIn("SECRET UNTRUSTED OUTPUT", str(raised.exception))

    def test_run_qstat_kills_and_reaps_timed_out_child_promptly(self):
        supervisor = self.qstat_supervisor()
        started = time.monotonic()
        processes = []

        with (
            mock.patch.object(
                qstat.subprocess,
                "Popen",
                side_effect=self.tracked_popen(processes),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            qstat._run_qstat(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                b"",
                supervisor=supervisor,
                deadline=time.monotonic() + 0.1,
                max_output_bytes=1024,
            )

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(processes), 1)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_deadline_expires_while_stdout_stays_ready(self):
        supervisor = self.qstat_supervisor()
        script = """
import os
import time

chunk = b"x" * (4 * 1024)
while True:
    os.write(1, chunk)
    time.sleep(0.0005)
"""
        processes = []
        signals = []
        stdout_reads = []
        real_read = qstat.os.read
        started = time.monotonic()

        def tracking_read(fd, size):
            data = real_read(fd, size)
            if processes and fd == processes[0].stdout.fileno():
                stdout_reads.append(len(data))
            return data

        with (
            mock.patch.object(
                qstat.subprocess,
                "Popen",
                side_effect=self.tracked_popen(
                    processes,
                    signals=signals,
                ),
            ),
            mock.patch.object(qstat.os, "read", side_effect=tracking_read),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            qstat._run_qstat(
                [sys.executable, "-c", script],
                b"",
                supervisor=supervisor,
                deadline=time.monotonic() + 0.5,
                max_output_bytes=32 * 1024 * 1024,
            )

        self.assertGreaterEqual(sum(stdout_reads), 64 * 1024)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(processes), 1)
        self.assert_child_stopped(signals)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_final_wait_uses_remaining_deadline_after_pipes_close(self):
        supervisor = self.qstat_supervisor()
        script = """
import os
import time

time.sleep(0.05)
os.close(1)
time.sleep(10)
"""
        processes = []
        wait_timeouts = []
        wait_pipe_states = []
        started = time.monotonic()

        with (
            mock.patch.object(
                qstat.subprocess,
                "Popen",
                side_effect=self.tracked_popen(
                    processes,
                    wait_timeouts=wait_timeouts,
                    wait_pipe_states=wait_pipe_states,
                ),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            qstat._run_qstat(
                [sys.executable, "-c", script],
                b"x",
                supervisor=supervisor,
                deadline=time.monotonic() + 0.4,
                max_output_bytes=1024,
            )

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(processes), 1)
        # The final wait and the cleanup waits all run before the reaper is
        # handed the child, so the first two entries are this query's own.
        observed_timeouts = list(wait_timeouts)
        self.assertEqual(wait_pipe_states[:2], [(True, True), (True, True)])
        self.assertLess(observed_timeouts[0], 0.35)
        # Cleanup gets only what the expired deadline left it, never a wait
        # without a bound.
        self.assertEqual(observed_timeouts[1], 0.0)
        for timeout in observed_timeouts:
            self.assertIsNotNone(timeout)
            self.assertTrue(math.isfinite(timeout))
            self.assertGreaterEqual(timeout, 0)
        self.assert_qstat_process_cleaned(supervisor, processes[0])

    def test_run_qstat_selector_setup_fault_cleans_process_and_pipes(self):
        supervisor = self.qstat_supervisor()
        selector = _FaultingSelector()
        processes = []
        signals = []
        try:
            with (
                mock.patch.object(
                    qstat.selectors,
                    "DefaultSelector",
                    return_value=selector,
                ),
                mock.patch.object(
                    qstat.subprocess,
                    "Popen",
                    side_effect=self.tracked_popen(
                        processes,
                        signals=signals,
                    ),
                ),
                self.assertRaisesRegex(
                    OSError,
                    r"^injected selector setup fault$",
                ),
            ):
                qstat._run_qstat(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    b"x",
                    supervisor=supervisor,
                    deadline=time.monotonic() + 1,
                    max_output_bytes=1024,
                )

            self.assertTrue(selector.closed)
            self.assertEqual(len(processes), 1)
            self.assert_child_stopped(signals)
            self.assert_qstat_process_cleaned(supervisor, processes[0])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
                process.stdin.close()
                process.stdout.close()

    def test_run_qstat_stdout_read_fault_cleans_selector_process_and_pipes(self):
        supervisor = self.qstat_supervisor()
        selector = _TrackingSelector(qstat.selectors.DefaultSelector())
        processes = []
        signals = []
        real_read = qstat.os.read

        def faulting_read(fd, size):
            if processes and fd == processes[0].stdout.fileno():
                raise OSError("injected stdout read fault")
            return real_read(fd, size)

        try:
            with (
                mock.patch.object(
                    qstat.selectors,
                    "DefaultSelector",
                    return_value=selector,
                ),
                mock.patch.object(
                    qstat.subprocess,
                    "Popen",
                    side_effect=self.tracked_popen(
                        processes,
                        signals=signals,
                    ),
                ),
                mock.patch.object(qstat.os, "read", side_effect=faulting_read),
                self.assertRaisesRegex(OSError, r"^injected stdout read fault$"),
            ):
                qstat._run_qstat(
                    [
                        sys.executable,
                        "-c",
                        "import os, time; os.write(1, b'x'); time.sleep(10)",
                    ],
                    b"",
                    supervisor=supervisor,
                    deadline=time.monotonic() + 1,
                    max_output_bytes=1024,
                )

            self.assertTrue(selector.closed)
            self.assertEqual(len(processes), 1)
            self.assert_child_stopped(signals)
            self.assert_qstat_process_cleaned(supervisor, processes[0])
        finally:
            if not selector.closed:
                selector.close()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
                process.stdin.close()
                process.stdout.close()

    def test_run_qstat_stdin_write_fault_cleans_selector_process_and_pipes(self):
        supervisor = self.qstat_supervisor()
        selector = _TrackingSelector(qstat.selectors.DefaultSelector())
        processes = []
        signals = []
        real_write = qstat.os.write
        started = time.monotonic()

        def faulting_write(fd, data):
            if processes and fd == processes[0].stdin.fileno():
                raise OSError("injected stdin write fault")
            return real_write(fd, data)

        try:
            with (
                mock.patch.object(
                    qstat.selectors,
                    "DefaultSelector",
                    return_value=selector,
                ),
                mock.patch.object(
                    qstat.subprocess,
                    "Popen",
                    side_effect=self.tracked_popen(
                        processes,
                        signals=signals,
                    ),
                ),
                mock.patch.object(qstat.os, "write", side_effect=faulting_write),
                self.assertRaisesRegex(OSError, r"^injected stdin write fault$"),
            ):
                qstat._run_qstat(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    b"x",
                    supervisor=supervisor,
                    deadline=time.monotonic() + 5,
                    max_output_bytes=1024,
                )

            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(selector.closed)
            self.assertEqual(len(processes), 1)
            self.assert_child_stopped(signals)
            self.assert_qstat_process_cleaned(supervisor, processes[0])
        finally:
            if not selector.closed:
                selector.close()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
                process.stdin.close()
                process.stdout.close()

    def test_stat_master_runs_a_bounded_qstat_query_and_returns_servers(self):
        supervisor = self.qstat_supervisor()
        started = time.monotonic()
        with (
            mock.patch.object(
                qstat,
                "find_qstat",
                return_value="/sentinel/discovered/qstat-round-1",
            ) as find_qstat,
            mock.patch.object(
                qstat,
                "_run_qstat",
                return_value=QSTAT_SINGLE_SERVER_LIST_XML.encode("utf-8"),
            ) as run_qstat,
        ):
            servers = qstat.stat_master(
                "q2",
                {"name": "Quake II"},
                [
                    "master://first.example:27900",
                    "master://second.example:27900",
                ],
            )
        finished = time.monotonic()

        self.assertEqual(run_qstat.call_count, 1)
        arguments, keywords = run_qstat.call_args
        self.assertEqual(
            arguments,
            (
                [
                    "/sentinel/discovered/qstat-round-1",
                    "-xml",
                    "-utf8",
                    "-timeout",
                    "10",
                    "-maxsim",
                    "9999",
                    "-sendinterval",
                    "1",
                    "-R",
                    "-P",
                    "-f",
                    "-",
                ],
                b"Q2M first.example:27900\nQ2M second.example:27900",
            ),
        )
        # The installed supervisor is resolved per query, and the whole
        # operation runs against one absolute 60-second deadline.
        self.assertIs(keywords.pop("supervisor"), supervisor)
        deadline = keywords.pop("deadline")
        self.assertGreaterEqual(deadline, started + qstat.QSTAT_PARENT_TIMEOUT)
        self.assertLessEqual(deadline, finished + qstat.QSTAT_PARENT_TIMEOUT)
        self.assertEqual(keywords, {"max_output_bytes": 16 * 1024 * 1024})
        find_qstat.assert_called_once_with()
        self.assertEqual(
            servers,
            [
                Server(
                    endpoint=Endpoint("single.example", 27910),
                    game_id="q2",
                    name="Single Server",
                    player_count=1,
                    player_limit=12,
                    ping=31,
                    game_type="deathmatch",
                    terrain="warehouse",
                    players=(Player("Ada", score=4, ping=18),),
                )
            ],
        )

    def test_stat_master_classifies_process_timeout_without_output(self):
        self.assert_qstat_adapter_error(
            subprocess.TimeoutExpired(
                cmd=["/opt/bin/qstat"],
                timeout=60,
                output=b"SECRET UNTRUSTED OUTPUT",
                stderr=b"SECRET UNTRUSTED OUTPUT",
            ),
            r"timed out",
        )

    def test_stat_master_classifies_nonzero_exit_without_output(self):
        self.assert_qstat_adapter_error(
            subprocess.CalledProcessError(
                returncode=2,
                cmd=["/opt/bin/qstat"],
                output=b"SECRET UNTRUSTED OUTPUT",
                stderr=b"SECRET UNTRUSTED OUTPUT",
            ),
            r"exited unsuccessfully",
        )

    def test_stat_master_rejects_non_utf8_output_without_dumping_it(self):
        self.assert_qstat_adapter_error(
            b"\xffSECRET UNTRUSTED OUTPUT",
            r"valid UTF-8",
        )

    def test_stat_master_rejects_malformed_xml_without_dumping_it(self):
        self.assert_qstat_adapter_error(
            b"<qstat><server>SECRET UNTRUSTED OUTPUT</qstat>",
            r"malformed XML",
        )


class GtkServerBoundaryTests(unittest.TestCase):
    @staticmethod
    def make_game_table(server):
        from obozrenie.core import GameTable

        table = GameTable({
            "g": {
                "name": "Game",
                "adapter": "fake",
                "launch_pattern": "quake",
                "settings": [],
            }
        })
        token = table.begin_query("g")
        table.finish_query("g", token, [server])
        return table

    def test_server_records_are_converted_when_rows_enter_the_model(self):
        from obozrenie.gtk import GUIActions

        class ListModel(list):
            pass

        window = GUIActions.__new__(GUIActions)
        model = ListModel()
        window.gtk_widgets = {
            "server-list-model": model,
            "server-connect-host": object(),
            "server-connect-game": object(),
        }
        window.server_list_model_format = (
            "host",
            "name",
            "game_id",
            "ping",
            "password",
            "secure",
            "country",
            "players",
            "rules",
        )
        window.game_list_model_format = ("game_id",)
        window.game_icons = {}
        server = Server(
            endpoint=Endpoint("2001:db8::40", 30000),
            game_id="g",
            name="GTK IPv6",
            players=(Player("Ada"),),
            rules={"mode": "survival"},
        )

        with (
            mock.patch(
                "obozrenie.gtk.gtk_helpers.get_widget_value",
                side_effect=("", None),
            ),
            mock.patch("obozrenie.gtk.gtk_helpers.set_widget_value"),
        ):
            window.fill_server_list_model([server])

        self.assertEqual(
            model,
            [[
                "[2001:db8::40]:30000",
                "GTK IPv6",
                "g",
                9999,
                False,
                False,
                "unknown",
                [{"name": "Ada", "score": None, "ping": 9999}],
                {"mode": "survival"},
            ]],
        )

    def test_server_record_is_converted_for_the_information_dialog(self):
        from obozrenie.gtk import GUIActions

        class Widget:
            def __init__(self):
                self.value = None
                self.properties = {}

            def set_property(self, name, value):
                self.properties[name] = value

        class Dialog(Widget):
            def __init__(self):
                super().__init__()
                self.ran = False
                self.hidden = False

            def run(self):
                self.ran = True

            def hide(self):
                self.hidden = True

        class ListModel(list):
            pass

        server = Server(
            endpoint=Endpoint("2001:db8::50", 30000),
            game_id="g",
            name="Details",
            player_count=1,
            player_limit=16,
            ping=21,
            terrain="Map",
            players=(Player("Ada"),),
        )
        table = self.make_game_table(server)
        window = GUIActions.__new__(GUIActions)
        window.core = SimpleNamespace(game_table=table)
        window.app = SimpleNamespace(
            core=window.core,
            settings=SimpleNamespace(settings_table={
                "common": {
                    "selected-game-connect": "g",
                    "server-host": str(server.endpoint),
                }
            }),
        )
        dialog = Dialog()
        player_model = ListModel()
        window.gtk_widgets = {
            "serverinfo-dialog": dialog,
            "serverinfo-name": Widget(),
            "serverinfo-host": Widget(),
            "serverinfo-game": Widget(),
            "serverinfo-gameid": Widget(),
            "serverinfo-terrain": Widget(),
            "serverinfo-players": Widget(),
            "serverinfo-ping": Widget(),
            "player-list-model": player_model,
        }
        window.player_list_model_format = ("name", "score", "ping")

        def set_widget_value(widget, value, *args, **kwargs):
            widget.value = value

        with mock.patch(
            "obozrenie.gtk.gtk_helpers.set_widget_value",
            side_effect=set_widget_value,
        ):
            window.cb_info_button_clicked()

        self.assertEqual(
            window.gtk_widgets["serverinfo-host"].value,
            "[2001:db8::50]:30000",
        )
        self.assertEqual(window.gtk_widgets["serverinfo-game"].value, "Game")
        self.assertEqual(player_model, [["Ada", None, 9999]])
        # The player view's visibility is the GtkBuilder document's, like
        # every other widget's; this handler only fills the model.
        self.assertTrue(dialog.ran)
        self.assertTrue(dialog.hidden)


if __name__ == "__main__":
    unittest.main()
