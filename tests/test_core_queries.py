import contextlib
import json
import socket
import threading
import time
import unittest
from enum import Enum
from unittest import mock

import maxminddb
import requests
from icmplib import Host

from obozrenie import (
    adapters,
    core as core_module,
    helpers,
    network_calls,
)
from obozrenie.adapters import minetest, qstat, rigsofrods
from obozrenie.core import Core, GameTable
from obozrenie.diagnostics import SafeDiagnostic, SafeDiagnosticError
from obozrenie.models import Endpoint, Player, Server


GAME_CONFIG = {
    "name": "Game",
    "adapter": "fake",
    "launch_pattern": "quake",
    "settings": ["master_uri"],
}

CONFIG = {"g": GAME_CONFIG}
TWO_GAME_CONFIG = {"g": GAME_CONFIG, "h": GAME_CONFIG}


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


def _diagnostic_texts(messages):
    """Render the safe diagnostic each recorded core message carries."""
    return [str(message[-1]) for message in messages]


def _make_bypassed_server(host: str, port: int, **fields):
    with mock.patch.object(Server, "_record_final", False):
        mutable_server_type = type("LegacyServer", (Server,), {})
    server = mutable_server_type(
        endpoint=Endpoint(host, port),
        game_id="g",
        **fields,
    )
    object.__setattr__(server, "payload", [])
    return server


class _FakeClock:
    """A monotonic clock a test advances explicitly."""

    def __init__(self, now: float):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecordingThread:
    """A launched worker stand-in that records joins instead of running."""

    def __init__(self, joins, clock=None, step=0.0):
        self._joins = joins
        self._clock = clock
        self._step = step
        self.ident = None
        self.alive = False

    def start(self):
        # A launched thread has an identity, so Core keeps tracking it until
        # its target exits or a bounded join observes that it stopped.
        self.ident = id(self)
        self.alive = True

    def join(self, timeout=None):
        self._joins.append((self, timeout))
        if self._clock is not None:
            self._clock.advance(self._step)

    def is_alive(self):
        return self.alive


def _recording_threads(joins, clock=None, step=0.0):
    """Return a Thread factory recording joins and the threads it creates."""
    threads = []

    def factory(target=None, daemon=None):
        thread = _RecordingThread(joins, clock, step)
        threads.append(thread)
        return thread

    return factory, threads


class QueryStateTests(unittest.TestCase):
    def setUp(self):
        self.table = GameTable(CONFIG)

    def test_query_status_is_public_enum(self):
        from obozrenie.core import QueryStatus

        self.assertTrue(issubclass(QueryStatus, Enum))
        self.assertIs(self.table.QUERY_STATUS, QueryStatus)

    def test_begin_query_returns_one_opaque_token_atomically(self):
        gate = threading.Barrier(8)
        results_lock = threading.Lock()
        results = []
        failures = []

        def attempt():
            try:
                gate.wait(1)
                token = self.table.begin_query("g")
            except BaseException as error:
                failures.append(error)
                return
            with results_lock:
                results.append(token)

        threads = [
            threading.Thread(target=attempt, daemon=True) for _ in range(8)
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(1)
        finally:
            # Release any worker still waiting for a rendezvous that a failed
            # start can no longer complete, then join it.
            gate.abort()
            for thread in threads:
                thread.join(1)

        self.assertEqual(failures, [])
        self.assertEqual([thread for thread in threads if thread.is_alive()], [])
        self.assertEqual(len(results), 8)
        self.assertEqual(results.count(None), 7)
        admitted = [token for token in results if token is not None]
        self.assertEqual(len(admitted), 1)
        # The admission result is an opaque identity, not a reusable flag.
        self.assertNotIsInstance(admitted[0], bool)
        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.WORKING
        )
        self.assertIs(self.table.finish_query("g", admitted[0], []), True)

    def test_stale_attempt_cannot_finish_a_later_working_query(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        stale_server = Server(endpoint=Endpoint("stale.example", 1), game_id="g")
        fresh_server = Server(endpoint=Endpoint("fresh.example", 1), game_id="g")
        stale_token = self.table.begin_query("g")
        self.assertIs(
            self.table.finish_query("g", stale_token, [old_server]), True
        )
        fresh_token = self.table.begin_query("g")
        self.assertIsNotNone(fresh_token)
        self.assertIsNot(fresh_token, stale_token)

        self.assertIs(
            self.table.finish_query("g", stale_token, [stale_server]), False
        )
        self.assertIs(self.table.finish_query("g", None, [stale_server]), False)
        self.assertIs(
            self.table.finish_query("g", object(), [stale_server]), False
        )

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.WORKING
        )
        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertIsNone(self.table.get_query_error("g"))

        self.assertIs(
            self.table.finish_query("g", fresh_token, [fresh_server]), True
        )

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.READY
        )
        self.assertEqual(self.table.get_servers_data("g"), [fresh_server])
        # A committed token is consumed and cannot commit a second time.
        self.assertIs(
            self.table.finish_query("g", fresh_token, [stale_server]), False
        )

    def test_stale_attempt_cannot_fail_a_later_working_query(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        stale_token = self.table.begin_query("g")
        self.assertIs(
            self.table.finish_query("g", stale_token, [old_server]), True
        )
        fresh_token = self.table.begin_query("g")
        self.assertIsNotNone(fresh_token)
        self.assertIsNot(fresh_token, stale_token)

        self.assertIs(
            self.table.fail_query("g", stale_token, "stale error"), False
        )
        self.assertIs(self.table.fail_query("g", None, "stale error"), False)
        self.assertIs(
            self.table.fail_query("g", object(), "stale error"), False
        )

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.WORKING
        )
        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertIsNone(self.table.get_query_error("g"))

        self.assertIs(
            self.table.fail_query("g", fresh_token, "fresh error"), True
        )

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.ERROR
        )
        self.assertEqual(self.table.get_query_error("g"), "fresh error")
        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertIs(
            self.table.fail_query("g", fresh_token, "second error"), False
        )

    def test_success_replaces_servers_and_clears_error(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        new_server = Server(endpoint=Endpoint("new.example", 1), game_id="g")
        self.table.finish_query("g", self.table.begin_query("g"), [old_server])
        self.table.fail_query("g", self.table.begin_query("g"), "old error")
        self.table.finish_query("g", self.table.begin_query("g"), [new_server])

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.READY
        )
        self.assertEqual(self.table.get_servers_data("g"), [new_server])
        self.assertIsNone(self.table.get_query_error("g"))

    def test_begin_query_clears_previous_error(self):
        self.table.fail_query("g", self.table.begin_query("g"), "old error")

        self.assertIsNotNone(self.table.begin_query("g"))

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.WORKING
        )
        self.assertIsNone(self.table.get_query_error("g"))

    def test_failure_preserves_old_servers(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        self.table.finish_query("g", self.table.begin_query("g"), [old_server])
        self.table.fail_query("g", self.table.begin_query("g"), "safe message")

        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertEqual(self.table.get_query_error("g"), "safe message")
        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.ERROR
        )

    def test_finish_query_requires_working_status(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        new_server = Server(endpoint=Endpoint("new.example", 1), game_id="g")
        for status in (
            self.table.QUERY_STATUS.EMPTY,
            self.table.QUERY_STATUS.READY,
            self.table.QUERY_STATUS.ERROR,
        ):
            with self.subTest(status=status):
                table = GameTable(CONFIG)
                expected_servers = []
                expected_error = None
                spent_token = None
                if status is table.QUERY_STATUS.READY:
                    spent_token = table.begin_query("g")
                    table.finish_query("g", spent_token, [old_server])
                    expected_servers = [old_server]
                elif status is table.QUERY_STATUS.ERROR:
                    table.finish_query(
                        "g", table.begin_query("g"), [old_server]
                    )
                    spent_token = table.begin_query("g")
                    table.fail_query("g", spent_token, "old error")
                    expected_servers = [old_server]
                    expected_error = "old error"

                self.assertIs(
                    table.finish_query("g", spent_token, [new_server]), False
                )

                self.assertEqual(table.get_query_status("g"), status)
                self.assertEqual(table.get_servers_data("g"), expected_servers)
                self.assertEqual(table.get_query_error("g"), expected_error)

    def test_fail_query_requires_working_status(self):
        for status in (
            self.table.QUERY_STATUS.EMPTY,
            self.table.QUERY_STATUS.READY,
            self.table.QUERY_STATUS.ERROR,
        ):
            with self.subTest(status=status):
                table = GameTable(CONFIG)
                expected_error = None
                spent_token = None
                if status is table.QUERY_STATUS.READY:
                    spent_token = table.begin_query("g")
                    table.finish_query("g", spent_token, [])
                elif status is table.QUERY_STATUS.ERROR:
                    spent_token = table.begin_query("g")
                    table.fail_query("g", spent_token, "old error")
                    expected_error = "old error"

                self.assertIs(
                    table.fail_query("g", spent_token, "new error"), False
                )

                self.assertEqual(table.get_query_status("g"), status)
                self.assertEqual(table.get_query_error("g"), expected_error)

    def test_fail_query_rejects_mutable_error_before_lock_and_preserves_state(self):
        old_server = Server(endpoint=Endpoint("old.example", 1), game_id="g")
        self.table.finish_query("g", self.table.begin_query("g"), [old_server])
        token = self.table.begin_query("g")
        mutable_error = ["unsafe error"]

        class UnexpectedLock:
            def __enter__(self):
                raise AssertionError("invalid query error acquired the lock")

            def __exit__(self, *_args):
                return False

        actual_lock = self.table._lock
        self.table._lock = UnexpectedLock()
        raised = None
        try:
            try:
                self.table.fail_query("g", token, mutable_error)
            except BaseException as error:
                raised = error
        finally:
            self.table._lock = actual_lock

        mutable_error.append("mutated after rejection")
        self.assertIs(type(raised), TypeError)
        self.assertEqual(str(raised), "GameTable error must be an exact str.")
        self.assertEqual(
            self.table.get_query_status("g"),
            self.table.QUERY_STATUS.WORKING,
        )
        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertIsNone(self.table.get_query_error("g"))

        self.assertIs(self.table.fail_query("g", token, "safe failure"), True)

        self.assertEqual(
            self.table.get_query_status("g"),
            self.table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(self.table.get_servers_data("g"), [old_server])
        self.assertEqual(self.table.get_query_error("g"), "safe failure")

    def test_fail_query_rejects_str_subclass_before_lock_then_accepts_exact_str(self):
        class ErrorString(str):
            pass

        subclass_error = ErrorString("unsafe subclass")

        class UnexpectedLock:
            def __enter__(self):
                raise AssertionError("invalid query error acquired the lock")

            def __exit__(self, *_args):
                return False

        token = self.table.begin_query("g")
        actual_lock = self.table._lock
        self.table._lock = UnexpectedLock()
        raised = None
        try:
            try:
                self.table.fail_query("g", token, subclass_error)
            except BaseException as error:
                raised = error
        finally:
            self.table._lock = actual_lock

        self.assertIs(type(raised), TypeError)
        self.assertEqual(str(raised), "GameTable error must be an exact str.")
        self.assertEqual(
            self.table.get_query_status("g"),
            self.table.QUERY_STATUS.WORKING,
        )
        self.assertIsNone(self.table.get_query_error("g"))

        exact_error = "".join(("safe", " exact failure"))
        self.assertIs(self.table.fail_query("g", token, exact_error), True)

        stored_error = self.table.get_query_error("g")
        self.assertIs(type(stored_error), str)
        self.assertEqual(stored_error, exact_error)
        self.assertEqual(
            self.table.get_query_status("g"),
            self.table.QUERY_STATUS.ERROR,
        )

    def test_server_records_survive_focused_and_full_copy_accessors(self):
        players = [Player("Ada", score=-3, ping=-7)]
        rules = {"mode": "original"}
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=players,
            rules=rules,
        )
        submitted = [server]
        self.table.finish_query("g", self.table.begin_query("g"), submitted)

        submitted.clear()
        players.append(Player("Lin"))
        rules["mode"] = "mutated before read"

        returned = self.table.get_servers_data("g")
        full_copy = self.table.get_game_table_copy()

        self.assertIs(returned[0], server)
        self.assertIs(full_copy["g"]["servers"][0], server)
        self.assertIs(self.table.get_server_info("g", "example.org:1"), server)
        self.assertEqual(
            tuple(
                (player.name, player.score, player.ping)
                for player in returned[0].players
            ),
            (("Ada", -3, -7),),
        )
        self.assertEqual(dict(returned[0].rules), {"mode": "original"})
        with self.assertRaises(TypeError):
            returned[0].rules["mode"] = "mutated"
        returned.clear()
        full_copy["g"]["servers"].clear()
        self.assertEqual(self.table.get_servers_data("g"), [server])

    def test_finish_query_rejects_a_mixed_subclass_batch_before_locking(self):
        old_server = Server(
            endpoint=Endpoint("old.example", 1),
            game_id="g",
            country="GB",
        )
        new_server = Server(
            endpoint=Endpoint("new.example", 2),
            game_id="g",
        )
        mutable_server = _make_bypassed_server("mutable.example", 3)
        final_server = Server(
            endpoint=Endpoint("final.example", 4),
            game_id="g",
        )
        self.table.finish_query("g", self.table.begin_query("g"), [old_server])
        token = self.table.begin_query("g")
        materialization_events = []

        def mixed_batch():
            materialization_events.append("exact first")
            yield new_server
            materialization_events.append("subclass")
            yield mutable_server
            materialization_events.append("exact last")
            yield final_server

        class UnexpectedLock:
            def __enter__(self):
                raise AssertionError("invalid finish batch acquired the lock")

            def __exit__(self, *_args):
                return False

        actual_lock = self.table._lock
        self.table._lock = UnexpectedLock()
        try:
            try:
                self.table.finish_query("g", token, mixed_batch())
            except Exception as error:
                raised = error
            else:
                raised = None
        finally:
            self.table._lock = actual_lock

        mutable_server.payload.append("mutated")
        self.assertEqual(
            materialization_events,
            ["exact first", "subclass", "exact last"],
        )
        self.assertIs(type(raised), TypeError)
        self.assertEqual(
            str(raised),
            "GameTable servers[1] must be an exact Server record.",
        )
        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.WORKING
        )
        self.assertIsNone(self.table.get_query_error("g"))
        self.assertEqual(self.table.get_servers_data("g"), [old_server])

        self.assertIs(self.table.fail_query("g", token, "safe failure"), True)

        self.assertEqual(
            self.table.get_query_status("g"), self.table.QUERY_STATUS.ERROR
        )
        self.assertEqual(self.table.get_query_error("g"), "safe failure")
        self.assertEqual(self.table.get_servers_data("g"), [old_server])

    def test_game_setting_is_copied_on_input(self):
        master_uris = ["master://one.example:27950"]
        self.table.set_game_setting("g", "master_uri", master_uris)

        master_uris.append("master://two.example:27950")

        self.assertEqual(
            self.table.get_game_settings("g")["master_uri"],
            ["master://one.example:27950"],
        )


class CoreQueryTestCase(unittest.TestCase):
    """Shared Core fixture for query and lifecycle tests."""

    def setUp(self):
        # Core diagnostics are asserted through explicit per-test patches, so
        # the default path stays quiet and a passing run prints nothing.
        quiet = mock.patch.object(helpers, "debug_msg", lambda *_args: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def new_core(self, config=CONFIG):
        """Build a Core over one test game configuration."""
        with mock.patch.object(
            core_module.resources, "load_toml", return_value=config
        ), mock.patch.object(
            core_module.geoip, "find_database", return_value=None
        ):
            core = Core()
        for game in config:
            core.game_table.set_game_setting(game, "master_uri", [])
        return core

    def make_core(self, adapter, config=CONFIG):
        """Build a Core whose configured games resolve to one fake adapter."""
        core = self.new_core(config)

        missing = object()
        previous = adapters.adapter_table.get("fake", missing)
        adapters.adapter_table["fake"] = adapter
        if previous is missing:
            self.addCleanup(adapters.adapter_table.pop, "fake", None)
        else:
            self.addCleanup(adapters.adapter_table.__setitem__, "fake", previous)
        return core

    def seed_servers(self, core, servers, game="g"):
        """Publish a completed earlier query so a new attempt has old data."""
        token = core.game_table.begin_query(game)
        self.assertIsNotNone(token)
        self.assertIs(core.game_table.finish_query(game, token, servers), True)

    def admit_attempt(self, core, callback=None, game="g"):
        """Admit one query and return the attempt that owns its completion."""
        token = core.game_table.begin_query(game)
        self.assertIsNotNone(token)
        return core_module._QueryAttempt(core, game, token, callback)

    def run_query_worker(self, core, callback=None):
        """Run one admitted attempt's worker body on the calling thread."""
        self.admit_attempt(core, callback).run_worker()

    def launch_query(self, core, callback=None, game="g"):
        """Admit one query through Core and return its worker thread."""
        real_thread_type = threading.Thread
        threads = []

        def record_thread(*args, **kwargs):
            thread = real_thread_type(*args, **kwargs)
            threads.append(thread)
            return thread

        with mock.patch("obozrenie.core.threading.Thread", record_thread):
            admitted = core.update_server_list(game, callback)
        self.assertEqual(len(threads), 1)
        return admitted, threads[0]


class QueryWorkerTests(CoreQueryTestCase):
    def test_worker_uses_the_explicit_adapter_resolver_boundary(self):
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *_args: [])},
        )
        core = self.new_core()
        resolutions = []

        def resolve_adapter(name):
            resolutions.append(name)
            return adapter

        with mock.patch.object(
            adapters,
            "resolve_adapter",
            side_effect=resolve_adapter,
        ):
            self.run_query_worker(core)

        self.assertEqual(resolutions, ["fake"])
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_update_reserves_before_worker_and_completes_on_worker_thread(self):
        worker_started = threading.Event()
        release_worker = threading.Event()
        callback_finished = threading.Event()
        callback_games = []
        worker_threads = []

        def stat_master(*args):
            worker_threads.append(threading.get_ident())
            worker_started.set()
            if not release_worker.wait(2):
                raise TimeoutError("test did not release query worker")
            return []

        def callback(game):
            callback_games.append(game)
            callback_finished.set()

        adapter = type("Adapter", (), {"stat_master": staticmethod(stat_master)})
        core = self.make_core(adapter)

        try:
            admitted = core.update_server_list("g", callback)
            worker_entered = worker_started.wait(1)
            status_while_blocked = core.game_table.get_query_status("g")
        finally:
            release_worker.set()
        callback_ran = callback_finished.wait(1)

        self.assertIs(admitted, True)
        self.assertTrue(worker_entered)
        self.assertEqual(
            status_while_blocked, core.game_table.QUERY_STATUS.WORKING
        )
        self.assertTrue(callback_ran)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        self.assertEqual(callback_games, ["g"])
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_update_rejects_duplicate_before_spawning(self):
        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(lambda *args: [])}
        )
        core = self.make_core(adapter)
        callbacks = []

        self.assertIsNotNone(core.game_table.begin_query("g"))
        with mock.patch("obozrenie.core.threading.Thread") as thread_factory:
            admitted = core.update_server_list("g", callbacks.append)

        self.assertIs(admitted, False)
        thread_factory.assert_not_called()
        self.assertEqual(callbacks, [])
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.WORKING,
        )

    def test_startup_failure_finalizes_admitted_query_synchronously(self):
        def fail_construction(*args, **kwargs):
            raise RuntimeError("thread construction failed")

        class StartFailureThread:
            # A thread that never launches keeps the unset identity Core uses
            # to prove it can stop tracking the thread itself.
            ident = None

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        for stage, thread_factory in (
            ("construction", fail_construction),
            ("start", StartFailureThread),
        ):
            with self.subTest(stage=stage):
                adapter = type(
                    "Adapter", (), {"stat_master": staticmethod(lambda *args: [])}
                )
                core = self.make_core(adapter)
                old_servers = [
                    Server(
                        endpoint=Endpoint("old.example", 27015),
                        game_id="g",
                        country="GB",
                    )
                ]
                self.seed_servers(core, old_servers)
                caller_thread = threading.get_ident()
                callbacks = []

                def callback(game):
                    callbacks.append((game, threading.get_ident()))

                admitted = None
                raised = None
                with mock.patch("obozrenie.core.threading.Thread", thread_factory):
                    try:
                        admitted = core.update_server_list("g", callback)
                    except Exception as error:
                        raised = error

                self.assertEqual(
                    {
                        "raised": raised,
                        "admitted": admitted,
                        "status": core.game_table.get_query_status("g"),
                        "error": core.game_table.get_query_error("g"),
                        "servers": core.game_table.get_servers_data("g"),
                        "callbacks": callbacks,
                    },
                    {
                        "raised": None,
                        "admitted": True,
                        "status": core.game_table.QUERY_STATUS.ERROR,
                        "error": "Unable to refresh the server list.",
                        "servers": old_servers,
                        "callbacks": [("g", caller_thread)],
                    },
                )

    def test_worker_enriches_whole_batch_before_atomic_commit(self):
        first = Server(
            endpoint=Endpoint("one.example", 30000),
            game_id="g",
            name="First server",
            player_limit=16,
            game_type="g",
            terrain="Forest",
        )
        second = Server(
            endpoint=Endpoint("two.example", 30001),
            game_id="g",
            name="Second server",
            player_count=3,
            player_limit=12,
            ping=77,
            game_type="g",
            terrain="Desert",
        )
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [first, second])},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            name="Old server",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        measured_servers = [
            Server(
                endpoint=Endpoint("one.example", 30000),
                game_id="g",
                name="First server",
                player_limit=16,
                ping=12,
                game_type="g",
                terrain="Forest",
            ),
            Server(
                endpoint=Endpoint("two.example", 30001),
                game_id="g",
                name="Second server",
                player_count=3,
                player_limit=12,
                ping=34,
                game_type="g",
                terrain="Desert",
            ),
        ]
        rtt_batches = []
        snapshots = []
        callbacks = []

        def add_rtt_info(servers):
            rtt_batches.append(list(servers))
            return measured_servers

        def lookup_countries(hosts, deadline=None):
            countries = {}
            for host in hosts:
                snapshots.append(
                    (
                        host,
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                    )
                )
                countries[host] = {
                    "one.example": "CA",
                    "two.example": "NZ",
                }[host]
            return countries

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            rtt_batches,
            [
                [
                    Server(
                        endpoint=Endpoint("one.example", 30000),
                        game_id="g",
                        name="First server",
                        player_limit=16,
                        game_type="g",
                        terrain="Forest",
                    ),
                    Server(
                        endpoint=Endpoint("two.example", 30001),
                        game_id="g",
                        name="Second server",
                        player_count=3,
                        player_limit=12,
                        ping=77,
                        game_type="g",
                        terrain="Desert",
                    ),
                ]
            ],
        )
        self.assertEqual(
            snapshots,
            [
                (
                    "one.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
                (
                    "two.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        committed = core.game_table.get_servers_data("g")
        self.assertEqual(
            committed,
            [
                Server(
                    endpoint=Endpoint("one.example", 30000),
                    game_id="g",
                    name="First server",
                    player_limit=16,
                    ping=12,
                    country="CA",
                    game_type="g",
                    terrain="Forest",
                ),
                Server(
                    endpoint=Endpoint("two.example", 30001),
                    game_id="g",
                    name="Second server",
                    player_count=3,
                    player_limit=12,
                    ping=34,
                    country="NZ",
                    game_type="g",
                    terrain="Desert",
                ),
            ],
        )
        self.assertIsNot(committed[0], measured_servers[0])
        self.assertIsNot(committed[1], measured_servers[1])
        self.assertEqual(measured_servers[0].country, "unknown")
        self.assertEqual(measured_servers[1].country, "unknown")
        self.assertEqual(callbacks, ["g"])

    def test_rtt_failure_preserves_default_and_legitimate_input_pings(self):
        first = Server(
            endpoint=Endpoint("default.example", 30000),
            game_id="g",
            name="Default ping",
        )
        second = Server(
            endpoint=Endpoint("reported.example", 30001),
            game_id="g",
            name="Reported ping",
            ping=73,
        )
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [first, second])},
        )
        core = self.make_core(adapter)
        rtt_batches = []

        def fail_rtt(servers):
            rtt_batches.append(list(servers))
            raise RuntimeError("RTT service unavailable")

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=fail_rtt
        ):
            self.run_query_worker(core)

        self.assertEqual(
            rtt_batches,
            [
                [
                    Server(
                        endpoint=Endpoint("default.example", 30000),
                        game_id="g",
                        name="Default ping",
                    ),
                    Server(
                        endpoint=Endpoint("reported.example", 30001),
                        game_id="g",
                        name="Reported ping",
                        ping=73,
                    ),
                ]
            ],
        )
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("default.example", 30000),
                    game_id="g",
                    name="Default ping",
                ),
                Server(
                    endpoint=Endpoint("reported.example", 30001),
                    game_id="g",
                    name="Reported ping",
                    ping=73,
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_rtt_mutate_then_raise_uses_untouched_input_snapshot(self):
        adapter_servers = [
            Server(
                endpoint=Endpoint("first.example", 30000),
                game_id="g",
                name="First server",
                ping=41,
            ),
            Server(
                endpoint=Endpoint("second.example", 30001),
                game_id="g",
                name="Second server",
                ping=82,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        self.seed_servers(
            core,
            [
                Server(
                    endpoint=Endpoint("old.example", 27015),
                    game_id="g",
                    name="Old server",
                    ping=55,
                    country="GB",
                )
            ],
        )
        rtt_inputs = []
        lookup_snapshots = []

        def mutate_then_fail(servers):
            rtt_inputs.append(list(servers))
            servers[:] = [
                Server(
                    endpoint=Endpoint("second.example", 30001),
                    game_id="g",
                    name="Second server",
                    ping=2,
                ),
                Server(
                    endpoint=Endpoint("first.example", 30000),
                    game_id="g",
                    name="First server",
                    ping=1,
                ),
            ]
            raise RuntimeError("RTT mutated its input before failing")

        def lookup_countries(hosts, deadline=None):
            countries = {}
            for host in hosts:
                lookup_snapshots.append(
                    (
                        host,
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                    )
                )
                countries[host] = {
                    "first.example": "CA",
                    "second.example": "NZ",
                }[host]
            return countries

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=mutate_then_fail
        ):
            self.run_query_worker(core)

        self.assertEqual(
            rtt_inputs,
            [
                [
                    Server(
                        endpoint=Endpoint("first.example", 30000),
                        game_id="g",
                        name="First server",
                        ping=41,
                    ),
                    Server(
                        endpoint=Endpoint("second.example", 30001),
                        game_id="g",
                        name="Second server",
                        ping=82,
                    ),
                ]
            ],
        )
        self.assertEqual(
            lookup_snapshots,
            [
                (
                    "first.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
                (
                    "second.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("first.example", 30000),
                    game_id="g",
                    name="First server",
                    ping=41,
                    country="CA",
                ),
                Server(
                    endpoint=Endpoint("second.example", 30001),
                    game_id="g",
                    name="Second server",
                    ping=82,
                    country="NZ",
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_lazy_rtt_failure_discards_partial_results_and_uses_snapshot(self):
        adapter_servers = [
            Server(
                endpoint=Endpoint("first.example", 30000),
                game_id="g",
                name="First server",
                ping=31,
            ),
            Server(
                endpoint=Endpoint("second.example", 30001),
                game_id="g",
                name="Second server",
                ping=62,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        self.seed_servers(
            core,
            [
                Server(
                    endpoint=Endpoint("old.example", 27015),
                    game_id="g",
                    name="Old server",
                    ping=55,
                    country="GB",
                )
            ],
        )
        rtt_inputs = []
        materialization_events = []
        lookup_snapshots = []

        def lazy_rtt(servers):
            rtt_inputs.append(list(servers))

            def results():
                materialization_events.append("yield partial result")
                yield Server(
                    endpoint=Endpoint("first.example", 30000),
                    game_id="g",
                    name="First server",
                    ping=1,
                )
                materialization_events.append("raise")
                raise RuntimeError("lazy RTT result failed")

            return results()

        def lookup_countries(hosts, deadline=None):
            countries = {}
            for host in hosts:
                lookup_snapshots.append(
                    (
                        host,
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                    )
                )
                countries[host] = {
                    "first.example": "CA",
                    "second.example": "NZ",
                }[host]
            return countries

        core._lookup_countries = lookup_countries

        with mock.patch("obozrenie.ping.add_rtt_info", side_effect=lazy_rtt):
            self.run_query_worker(core)

        self.assertEqual(
            rtt_inputs,
            [
                [
                    Server(
                        endpoint=Endpoint("first.example", 30000),
                        game_id="g",
                        name="First server",
                        ping=31,
                    ),
                    Server(
                        endpoint=Endpoint("second.example", 30001),
                        game_id="g",
                        name="Second server",
                        ping=62,
                    ),
                ]
            ],
        )
        self.assertEqual(
            materialization_events,
            ["yield partial result", "raise"],
        )
        self.assertEqual(
            lookup_snapshots,
            [
                (
                    "first.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
                (
                    "second.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            name="Old server",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("first.example", 30000),
                    game_id="g",
                    name="First server",
                    ping=31,
                    country="CA",
                ),
                Server(
                    endpoint=Endpoint("second.example", 30001),
                    game_id="g",
                    name="Second server",
                    ping=62,
                    country="NZ",
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_invalid_rtt_output_fails_before_country_lookup_or_commit(self):
        first = Server(endpoint=Endpoint("one.example", 30000), game_id="g")
        second = Server(endpoint=Endpoint("two.example", 30001), game_id="g")
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [first, second])},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=45,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        rtt_batches = []
        looked_up_hosts = []

        def invalid_rtt(servers):
            rtt_batches.append(list(servers))
            return [
                Server(
                    endpoint=Endpoint("one.example", 30000),
                    game_id="g",
                    ping=10,
                ),
                {"host": "two.example:30001", "ping": 20},
            ]

        def lookup_countries(hosts, deadline=None):
            looked_up_hosts.extend(hosts)
            return {host: "CA" for host in hosts}

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=invalid_rtt
        ):
            self.run_query_worker(core)

        self.assertEqual(
            rtt_batches,
            [
                [
                    Server(
                        endpoint=Endpoint("one.example", 30000), game_id="g"
                    ),
                    Server(
                        endpoint=Endpoint("two.example", 30001), game_id="g"
                    ),
                ]
            ],
        )
        self.assertEqual(looked_up_hosts, [])
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("old.example", 27015),
                    game_id="g",
                    ping=45,
                    country="GB",
                )
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_rtt_server_subclass_fails_before_country_lookup_or_commit(self):
        source = Server(endpoint=Endpoint("source.example", 1), game_id="g")
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [source])},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            country="GB",
        )
        self.seed_servers(core, [old_server])
        exact_result = Server(
            endpoint=Endpoint("exact.example", 2), game_id="g", ping=20
        )
        mutable_result = _make_bypassed_server(
            "mutable.example", 3, ping=30
        )
        materialization_events = []
        looked_up_hosts = []

        def add_rtt_info(_servers):
            def results():
                materialization_events.append("exact")
                yield exact_result
                materialization_events.append("subclass")
                yield mutable_result

            return results()

        def lookup_countries(hosts, deadline=None):
            looked_up_hosts.extend(hosts)
            return {host: "CA" for host in hosts}

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        mutable_result.payload.append("mutated")
        self.assertEqual(materialization_events, ["exact", "subclass"])
        self.assertEqual(looked_up_hosts, [])
        self.assertEqual(core.game_table.get_servers_data("g"), [old_server])
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_expected_country_failures_are_isolated_per_server(self):
        adapter_servers = [
            Server(endpoint=Endpoint("dns-failure.example", 1), game_id="g"),
            Server(endpoint=Endpoint("empty.example", 2), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.30", 3), game_id="g"),
            Server(endpoint=Endpoint("2001:db8::40", 4), game_id="g"),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        database_lookups = []
        resolution_calls = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                if address == "192.0.2.30":
                    raise maxminddb.InvalidDatabaseError("bad database node")
                if address == "2001:db8::40":
                    return {"country": {"iso_code": "NZ"}}
                raise AssertionError(f"unexpected database lookup: {address}")

        def resolve(*args, **kwargs):
            resolution_calls.append((args, kwargs))
            if args[0] == "dns-failure.example":
                raise socket.gaierror("lookup failed")
            if args[0] == "empty.example":
                return []
            raise AssertionError(f"unexpected DNS lookup: {args[0]}")

        def add_rtt_info(_servers):
            return [
                Server(
                    endpoint=Endpoint("dns-failure.example", 1),
                    game_id="g",
                    ping=10,
                ),
                Server(
                    endpoint=Endpoint("empty.example", 2),
                    game_id="g",
                    ping=20,
                ),
                Server(
                    endpoint=Endpoint("192.0.2.30", 3),
                    game_id="g",
                    ping=30,
                ),
                Server(
                    endpoint=Endpoint("2001:db8::40", 4),
                    game_id="g",
                    ping=40,
                ),
            ]

        core.geolocation = Database()

        with mock.patch(
            "obozrenie.core.socket.getaddrinfo", side_effect=resolve
        ), mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("dns-failure.example", 1),
                    game_id="g",
                    ping=10,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("empty.example", 2),
                    game_id="g",
                    ping=20,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.30", 3),
                    game_id="g",
                    ping=30,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("2001:db8::40", 4),
                    game_id="g",
                    ping=40,
                    country="NZ",
                ),
            ],
        )
        self.assertEqual(
            resolution_calls,
            [
                (
                    ("dns-failure.example", None),
                    {"type": socket.SOCK_STREAM},
                ),
                (("empty.example", None), {"type": socket.SOCK_STREAM}),
            ],
        )
        self.assertEqual(
            database_lookups,
            ["192.0.2.30", "2001:db8::40"],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_derived_country_infrastructure_error_fails_query_by_identity(self):
        class InfrastructureGaiError(
            network_calls.PoolUnavailableError,
            socket.gaierror,
        ):
            pass

        country_error = InfrastructureGaiError(
            "unexpected country infrastructure failure"
        )
        adapter_server = Server(
            endpoint=Endpoint("server.example", 1),
            game_id="g",
        )
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [adapter_server])},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        core.geolocation = mock.Mock()
        callbacks = []

        with (
            mock.patch(
                "obozrenie.ping.add_rtt_info",
                side_effect=lambda servers: list(servers),
            ),
            mock.patch.object(
                core_module,
                "getaddrinfo_many_until",
                return_value=[network_calls.CallOutcome.failure(country_error)],
            ),
            mock.patch.object(core_module.helpers, "debug_msg") as debug_msg,
        ):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(core.game_table.get_servers_data("g"), [old_server])
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )
        self.assertEqual(callbacks, ["g"])
        debug_msg.assert_called_once()
        self.assertEqual(
            _diagnostic_texts(debug_msg.call_args.args),
            [
                "Unexpected InfrastructureGaiError;"
                " exception details omitted."
            ],
        )
        core.geolocation.get.assert_not_called()

    def test_http_multiple_inheritance_map_error_reaches_core_by_identity(self):
        class InfrastructureRequestsTimeout(
            network_calls.CallDeadlineExceeded,
            requests.Timeout,
        ):
            pass

        map_error = InfrastructureRequestsTimeout(
            "unexpected HTTP infrastructure failure"
        )
        core = self.make_core(minetest)
        core.game_table.set_game_setting(
            "g",
            "master_uri",
            ["https://master.example/servers"],
        )
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        callbacks = []

        with (
            installed_network_pools() as pools,
            mock.patch.object(
                pools.http,
                "call_until",
                side_effect=map_error,
            ),
            mock.patch.object(core_module.helpers, "debug_msg") as debug_msg,
        ):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(core.game_table.get_servers_data("g"), [old_server])
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )
        self.assertEqual(callbacks, ["g"])
        debug_msg.assert_called_once()
        self.assertEqual(
            _diagnostic_texts(debug_msg.call_args.args),
            [
                "Unexpected InfrastructureRequestsTimeout;"
                " exception details omitted."
            ],
        )

    def test_malformed_mmdb_records_are_isolated_and_batch_commits_atomically(self):
        adapter_servers = [
            Server(endpoint=Endpoint("192.0.2.41", 1), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.42", 2), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.43", 3), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.44", 4), game_id="g"),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        malformed_record = ["US"]
        malformed_flat_code = ["CA"]
        malformed_nested_code = {"code": "DE"}
        database_lookups = []
        lookup_snapshots = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                lookup_snapshots.append(
                    (
                        address,
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                    )
                )
                return {
                    "192.0.2.41": malformed_record,
                    "192.0.2.42": {"country_code": malformed_flat_code},
                    "192.0.2.43": {
                        "country": {"iso_code": malformed_nested_code}
                    },
                    "192.0.2.44": {"country": {"iso_code": "NZ"}},
                }[address]

        def add_rtt_info(_servers):
            return [
                Server(
                    endpoint=Endpoint("192.0.2.41", 1),
                    game_id="g",
                    ping=10,
                ),
                Server(
                    endpoint=Endpoint("192.0.2.42", 2),
                    game_id="g",
                    ping=20,
                ),
                Server(
                    endpoint=Endpoint("192.0.2.43", 3),
                    game_id="g",
                    ping=30,
                ),
                Server(
                    endpoint=Endpoint("192.0.2.44", 4),
                    game_id="g",
                    ping=40,
                ),
            ]

        core.geolocation = Database()

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        malformed_record.append("mutated")
        malformed_flat_code.append("mutated")
        malformed_nested_code["mutable"] = []
        self.assertEqual(
            database_lookups,
            ["192.0.2.41", "192.0.2.42", "192.0.2.43", "192.0.2.44"],
        )
        self.assertEqual(
            lookup_snapshots,
            [
                (
                    "192.0.2.41",
                    core.game_table.QUERY_STATUS.WORKING,
                    [old_server],
                ),
                (
                    "192.0.2.42",
                    core.game_table.QUERY_STATUS.WORKING,
                    [old_server],
                ),
                (
                    "192.0.2.43",
                    core.game_table.QUERY_STATUS.WORKING,
                    [old_server],
                ),
                (
                    "192.0.2.44",
                    core.game_table.QUERY_STATUS.WORKING,
                    [old_server],
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("192.0.2.41", 1),
                    game_id="g",
                    ping=10,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.42", 2),
                    game_id="g",
                    ping=20,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.43", 3),
                    game_id="g",
                    ping=30,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.44", 4),
                    game_id="g",
                    ping=40,
                    country="NZ",
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))

    def test_unicode_resolution_failure_is_isolated_in_mixed_batch(self):
        malformed_host = "bad\udcff.example"
        adapter_servers = [
            Server(endpoint=Endpoint(malformed_host, 1), game_id="g", ping=91),
            Server(
                endpoint=Endpoint("working.example", 2),
                game_id="g",
                ping=92,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        self.seed_servers(
            core,
            [
                Server(
                    endpoint=Endpoint("old.example", 27015),
                    game_id="g",
                    ping=55,
                    country="GB",
                )
            ],
        )
        resolution_calls = []
        lookup_snapshots = []
        database_lookups = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                if address == "198.51.100.50":
                    return {"country_code": "CA"}
                raise AssertionError(f"unexpected database lookup: {address}")

        def resolve(*args, **kwargs):
            resolution_calls.append((args, kwargs))
            lookup_snapshots.append(
                (
                    args[0],
                    core.game_table.get_query_status("g"),
                    core.game_table.get_servers_data("g"),
                )
            )
            if args[0] == malformed_host:
                raise UnicodeEncodeError(
                    "idna",
                    malformed_host,
                    3,
                    4,
                    "surrogates not allowed",
                )
            if args[0] == "working.example":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        ("198.51.100.50", 0),
                    )
                ]
            raise AssertionError(f"unexpected DNS lookup: {args[0]}")

        def add_rtt_info(_servers):
            return [
                Server(
                    endpoint=Endpoint(malformed_host, 1),
                    game_id="g",
                    ping=10,
                ),
                Server(
                    endpoint=Endpoint("working.example", 2),
                    game_id="g",
                    ping=20,
                ),
            ]

        core.geolocation = Database()

        with mock.patch(
            "obozrenie.core.socket.getaddrinfo", side_effect=resolve
        ), mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        self.assertEqual(
            resolution_calls,
            [
                ((malformed_host, None), {"type": socket.SOCK_STREAM}),
                (("working.example", None), {"type": socket.SOCK_STREAM}),
            ],
        )
        self.assertEqual(
            lookup_snapshots,
            [
                (
                    malformed_host,
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
                (
                    "working.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
            ],
        )
        self.assertEqual(database_lookups, ["198.51.100.50"])
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint(malformed_host, 1),
                    game_id="g",
                    ping=10,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("working.example", 2),
                    game_id="g",
                    ping=20,
                    country="CA",
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_blocked_icmp_batch_commits_countries_and_calls_back_before_release(self):
        adapter_servers = [
            Server(
                endpoint=Endpoint("192.0.2.90", 1),
                game_id="g",
                ping=31,
            ),
            Server(
                endpoint=Endpoint("2001:db8::90", 2),
                game_id="g",
                ping=32,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        database_lookups = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                return {
                    "192.0.2.90": {"country_code": "CA"},
                    "2001:db8::90": {"country_code": "DE"},
                }[address]

        blocked_started = threading.Event()
        release_blocked = threading.Event()
        blocked_returned = threading.Event()
        callback_finished = threading.Event()
        callbacks = []
        operation_threads = []

        def probe(host, **_options):
            operation_threads.append(threading.current_thread())
            if host == "192.0.2.90":
                blocked_started.set()
                if not release_blocked.wait(2):
                    raise AssertionError("test did not release Core ICMP blocker")
                blocked_returned.set()
                return Host(host, 1, [4.0])
            if host == "2001:db8::90":
                return Host(host, 1, [8.0])
            raise AssertionError(f"unexpected ICMP host: {host}")

        def callback(game):
            callbacks.append(game)
            callback_finished.set()

        core.geolocation = Database()
        attempt = self.admit_attempt(core, callback)
        query = threading.Thread(
            target=attempt.run_worker,
            name="test-bounded-icmp-query",
        )
        query_started = False
        blocker_entered = False
        completed_before_release = False
        state_before_release = None
        with (
            installed_network_pools() as pools,
            mock.patch(
                "obozrenie.ping._RTT_BATCH_TIMEOUT_SECONDS",
                0.02,
                create=True,
            ),
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=probe,
            ) as ping_call,
        ):
            pools.icmp.ensure_started()
            try:
                query.start()
                query_started = True
                blocker_entered = blocked_started.wait(1)
                completed_before_release = callback_finished.wait(0.2)
                query.join(0.2)
                state_before_release = (
                    core.game_table.get_query_status("g"),
                    core.game_table.get_servers_data("g"),
                    core.game_table.get_query_error("g"),
                    list(callbacks),
                    query.is_alive(),
                )
            finally:
                release_blocked.set()
                if query_started:
                    query.join(1)

        self.assertTrue(blocker_entered)
        self.assertTrue(completed_before_release)
        self.assertTrue(blocked_returned.wait(1))
        self.assertFalse(query.is_alive())
        self.assertEqual(ping_call.call_count, 2)
        self.assertEqual(len(operation_threads), 2)
        self.assertTrue(all(thread.daemon for thread in operation_threads))
        self.assertEqual(
            {thread.name for thread in operation_threads},
            {"obozrenie-icmp"},
        )
        self.assertEqual(
            state_before_release,
            (
                core.game_table.QUERY_STATUS.READY,
                [
                    Server(
                        endpoint=Endpoint("192.0.2.90", 1),
                        game_id="g",
                        ping=9999,
                        country="CA",
                    ),
                    Server(
                        endpoint=Endpoint("2001:db8::90", 2),
                        game_id="g",
                        ping=8,
                        country="DE",
                    ),
                ],
                None,
                ["g"],
                False,
            ),
        )
        self.assertEqual(database_lookups, ["192.0.2.90", "2001:db8::90"])
        self.assertEqual(callbacks, ["g"])

    def test_icmp_runtime_error_uses_whole_batch_fallback_in_original_order(self):
        adapter_servers = [
            Server(
                endpoint=Endpoint("192.0.2.91", 1),
                game_id="g",
                ping=0,
            ),
            Server(
                endpoint=Endpoint("192.0.2.92", 2),
                game_id="g",
                ping=9999,
            ),
            Server(
                endpoint=Endpoint("2001:db8::91", 3),
                game_id="g",
                ping=42,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)

        class Database:
            def get(self, address):
                return {
                    "192.0.2.91": {"country_code": "US"},
                    "192.0.2.92": {"country_code": "CA"},
                    "2001:db8::91": {"country_code": "DE"},
                }[address]

        programming_error = RuntimeError("unexpected ICMP programming error")

        def probe(host, **_options):
            if host == "192.0.2.92":
                raise programming_error
            return Host(host, 1, [1.0])

        callbacks = []
        core.geolocation = Database()
        with (
            installed_network_pools(),
            mock.patch(
                "obozrenie.ping._RTT_BATCH_TIMEOUT_SECONDS",
                0.02,
                create=True,
            ),
            mock.patch(
                "obozrenie.ping.icmp_ping",
                side_effect=probe,
            ),
        ):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))
        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("192.0.2.91", 1),
                    game_id="g",
                    ping=0,
                    country="US",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.92", 2),
                    game_id="g",
                    ping=9999,
                    country="CA",
                ),
                Server(
                    endpoint=Endpoint("2001:db8::91", 3),
                    game_id="g",
                    ping=42,
                    country="DE",
                ),
            ],
        )

    def test_icmp_map_unavailable_subclass_uses_whole_batch_fallback(self):
        adapter_servers = [
            Server(
                endpoint=Endpoint("192.0.2.93", 1),
                game_id="g",
                ping=3,
            ),
            Server(
                endpoint=Endpoint("2001:db8::93", 2),
                game_id="g",
                ping=42,
            ),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)

        class UnexpectedUnavailable(network_calls.PoolUnavailableError):
            pass

        map_error = UnexpectedUnavailable("unexpected ICMP map failure")
        callbacks = []
        with installed_network_pools() as pools, mock.patch.object(
            pools.icmp,
            "map_until",
            side_effect=map_error,
        ) as map_until, mock.patch(
            "obozrenie.ping.icmp_ping",
            side_effect=AssertionError("map failure reached ICMP"),
        ) as ping_call:
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))
        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            [
                (server.endpoint, server.ping)
                for server in core.game_table.get_servers_data("g")
            ],
            [
                (Endpoint("192.0.2.93", 1), 3),
                (Endpoint("2001:db8::93", 2), 42),
            ],
        )
        map_until.assert_called_once()
        ping_call.assert_not_called()

    def test_blocked_http_master_batch_fails_and_calls_back_before_release(self):
        cases = (
            (
                "minetest",
                minetest,
                "https://blocked.example/minetest",
                "https://blocked.example/minetest",
                {
                    "list": [
                        {
                            "address": "late.example",
                            "port": 30000,
                            "clients": 0,
                            "clients_max": 10,
                        }
                    ]
                },
            ),
            (
                "rigsofrods",
                rigsofrods,
                "https://blocked.example/rigsofrods",
                "https://blocked.example/rigsofrods?json=true",
                [
                    {
                        "has-password": False,
                        "current-users": 0,
                        "max-clients": 10,
                        "ip": "late.example",
                        "port": 12000,
                        "name": "Late server",
                        "terrain-name": "Late terrain",
                    }
                ],
            ),
        )
        for case_name, adapter, master_uri, request_uri, payload in cases:
            with self.subTest(adapter=case_name):
                core = self.make_core(adapter)
                core.game_table.set_game_setting(
                    "g",
                    "master_uri",
                    [master_uri],
                )
                old_servers = [
                    Server(
                        endpoint=Endpoint("old.example", 27015),
                        game_id="g",
                        ping=55,
                        country="GB",
                    )
                ]
                self.seed_servers(core, old_servers)

                request_started = threading.Event()
                release_request = threading.Event()
                callback_finished = threading.Event()
                response_closed = threading.Event()
                callbacks = []
                request_threads = []

                class LateResponse:
                    encoding = "utf-8"

                    def __init__(self):
                        self.body = json.dumps(payload).encode("utf-8")
                        self.status_code = 200
                        self.headers = {}
                        self.was_entered = False
                        self.was_read = False
                        self.was_closed = False

                    def __enter__(self):
                        self.was_entered = True
                        return self

                    def __exit__(self, *_args):
                        self.close()
                        return False

                    def raise_for_status(self):
                        pass

                    def iter_content(self, chunk_size):
                        self.was_read = True
                        self.chunk_size = chunk_size
                        yield self.body

                    def close(self):
                        self.was_closed = True
                        response_closed.set()

                response = LateResponse()

                def blocked_request(url, **_kwargs):
                    request_threads.append(threading.current_thread())
                    if url != request_uri:
                        raise AssertionError(f"unexpected HTTP request: {url}")
                    request_started.set()
                    if not release_request.wait(2):
                        raise AssertionError(
                            "test did not release Core HTTP blocker"
                        )
                    return response

                class BlockingSession:
                    def __init__(self):
                        self.get = mock.Mock(side_effect=blocked_request)
                        self.was_entered = False
                        self.was_closed = False

                    def __enter__(self):
                        self.was_entered = True
                        return self

                    def __exit__(self, *_args):
                        self.was_closed = True
                        return False

                session = BlockingSession()

                def callback(game):
                    callbacks.append(game)
                    callback_finished.set()

                attempt = self.admit_attempt(core, callback)
                query = threading.Thread(
                    target=attempt.run_worker,
                    name=f"test-bounded-http-{case_name}",
                )
                query_started = False
                http_workers = []

                class ShortBatchClock:
                    @staticmethod
                    def monotonic():
                        return time.monotonic() - 29.98

                with (
                    installed_network_pools() as pools,
                    mock.patch(
                        "obozrenie.adapters.http_masters.time",
                        ShortBatchClock,
                    ),
                    mock.patch(
                        "obozrenie.http_client.requests.Session",
                        return_value=session,
                    ) as session_constructor,
                    mock.patch(
                        "obozrenie.http_client.requests.get",
                        side_effect=AssertionError(
                            "Core HTTP request bypassed its owned Session"
                        ),
                    ) as direct_request,
                    mock.patch(
                        "obozrenie.ping.add_rtt_info",
                        side_effect=lambda servers: list(servers),
                    ),
                ):
                    try:
                        query.start()
                        query_started = True
                        request_entered = request_started.wait(1)
                        working_state = (
                            core.game_table.get_query_status("g"),
                            core.game_table.get_servers_data("g"),
                        )
                        completed_before_release = callback_finished.wait(0.2)
                        state_before_release = (
                            core.game_table.get_query_status("g"),
                            core.game_table.get_servers_data("g"),
                            core.game_table.get_query_error("g"),
                            list(callbacks),
                            query.is_alive(),
                        )
                    finally:
                        release_request.set()
                        if query_started:
                            query.join(1)
                        late_response_closed = response_closed.wait(1)
                    http_workers = list(pools.http._workers)

                self.assertTrue(request_entered)
                self.assertEqual(
                    working_state,
                    (
                        core.game_table.QUERY_STATUS.WORKING,
                        old_servers,
                    ),
                )
                self.assertTrue(completed_before_release)
                self.assertEqual(
                    state_before_release,
                    (
                        core.game_table.QUERY_STATUS.ERROR,
                        old_servers,
                        "Unable to refresh the server list.",
                        ["g"],
                        False,
                    ),
                )
                self.assertFalse(query.is_alive())
                self.assertTrue(late_response_closed)
                self.assertTrue(response.was_entered)
                self.assertTrue(response.was_closed)
                self.assertFalse(response.was_read)
                (request_call,) = session.get.call_args_list
                self.assertEqual(request_call.args, (request_uri,))
                request_kwargs = dict(request_call.kwargs)
                hop_timeout = request_kwargs.pop("timeout")
                self.assertEqual(
                    request_kwargs,
                    {"allow_redirects": False, "stream": True},
                )
                # The nearly expired batch deadline bounds both hop waits.
                connect_wait, read_wait = hop_timeout
                self.assertEqual(connect_wait, read_wait)
                self.assertGreater(connect_wait, 0)
                self.assertLessEqual(connect_wait, 0.02)
                self.assertTrue(session.was_entered)
                self.assertTrue(session.was_closed)
                session_constructor.assert_called_once_with()
                direct_request.assert_not_called()
                self.assertEqual(len(request_threads), 1)
                self.assertTrue(request_threads[0].daemon)
                self.assertEqual(request_threads[0].name, "obozrenie-http")
                self.assertEqual(callbacks, ["g"])
                self.assertEqual(
                    core.game_table.get_query_status("g"),
                    core.game_table.QUERY_STATUS.ERROR,
                )
                self.assertEqual(
                    core.game_table.get_servers_data("g"),
                    old_servers,
                )
                self.assertEqual(
                    core.game_table.get_query_error("g"),
                    "Unable to refresh the server list.",
                )
                self.assertTrue(
                    all(not worker.is_alive() for worker in http_workers)
                )

    def test_blocked_geoip_batch_commits_and_calls_back_before_release(self):
        adapter_servers = [
            Server(endpoint=Endpoint("blocked.example", 1), game_id="g"),
            Server(endpoint=Endpoint("working.example", 2), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.80", 3), game_id="g"),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        blocked_started = threading.Event()
        release = threading.Event()
        callback_finished = threading.Event()
        callbacks = []
        database_lookups = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                return {
                    "198.51.100.80": {"country_code": "CA"},
                    "192.0.2.80": {"country_code": "US"},
                }[address]

        def candidate(address):
            return (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0),
            )

        def resolve(host, *_args, **_kwargs):
            if host == "blocked.example":
                blocked_started.set()
                if not release.wait(2):
                    raise AssertionError("test did not release Core DNS blocker")
                return [candidate("198.51.100.81")]
            if host == "working.example":
                return [candidate("198.51.100.80")]
            raise AssertionError(f"unexpected DNS host: {host}")

        def add_rtt_info(servers):
            return [
                Server(
                    endpoint=server.endpoint,
                    game_id=server.game_id,
                    ping=(index + 1) * 10,
                )
                for index, server in enumerate(servers)
            ]

        def callback(game):
            callbacks.append(game)
            callback_finished.set()

        core.geolocation = Database()
        attempt = self.admit_attempt(core, callback)
        query = threading.Thread(
            target=attempt.run_worker,
            name="test-batched-geoip-query",
        )
        with installed_network_pools(), mock.patch.object(
            core_module,
            "_GEOIP_DNS_BATCH_TIMEOUT_SECONDS",
            0.02,
            create=True,
        ), mock.patch.object(
            network_calls.socket,
            "getaddrinfo",
            side_effect=resolve,
        ), mock.patch.object(
            core_module.socket,
            "getaddrinfo",
            side_effect=resolve,
        ), mock.patch(
            "obozrenie.ping.add_rtt_info",
            side_effect=add_rtt_info,
        ):
            query.start()
            try:
                self.assertTrue(blocked_started.wait(1))
                completed_before_release = callback_finished.wait(0.2)
                state_before_release = (
                    core.game_table.get_query_status("g"),
                    core.game_table.get_servers_data("g"),
                    core.game_table.get_query_error("g"),
                    list(callbacks),
                )
            finally:
                release.set()
                query.join(1)

        self.assertTrue(completed_before_release)
        self.assertFalse(query.is_alive())
        self.assertEqual(
            state_before_release,
            (
                core.game_table.QUERY_STATUS.READY,
                [
                    Server(
                        endpoint=Endpoint("blocked.example", 1),
                        game_id="g",
                        ping=10,
                        country="",
                    ),
                    Server(
                        endpoint=Endpoint("working.example", 2),
                        game_id="g",
                        ping=20,
                        country="CA",
                    ),
                    Server(
                        endpoint=Endpoint("192.0.2.80", 3),
                        game_id="g",
                        ping=30,
                        country="US",
                    ),
                ],
                None,
                ["g"],
            ),
        )
        self.assertEqual(
            database_lookups,
            ["198.51.100.80", "192.0.2.80"],
        )

    def test_dns_close_at_map_boundary_commits_blank_and_calls_back(self):
        servers = [
            Server(endpoint=Endpoint("closed.example", 1), game_id="g"),
            Server(endpoint=Endpoint("192.0.2.81", 2), game_id="g"),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: servers)},
        )
        core = self.make_core(adapter)
        callbacks = []

        class Database:
            def get(self, address):
                return {"192.0.2.81": {"country_code": "US"}}[address]

        core.geolocation = Database()
        with installed_network_pools() as pools:
            real_map_until = pools.dns.map_until

            def close_then_map(*args):
                pools.dns.begin_close()
                return real_map_until(*args)

            with mock.patch.object(
                pools.dns,
                "map_until",
                side_effect=close_then_map,
            ), mock.patch(
                "obozrenie.ping.add_rtt_info",
                side_effect=lambda batch: list(batch),
            ):
                self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))
        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("closed.example", 1),
                    game_id="g",
                    country="",
                ),
                Server(
                    endpoint=Endpoint("192.0.2.81", 2),
                    game_id="g",
                    country="US",
                ),
            ],
        )

    def test_blocked_resolution_times_out_and_mixed_batch_commits(self):
        adapter_servers = [
            Server(endpoint=Endpoint("blocked.example", 1), game_id="g"),
            Server(endpoint=Endpoint("working.example", 2), game_id="g"),
        ]
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: adapter_servers)},
        )
        core = self.make_core(adapter)
        self.seed_servers(
            core,
            [
                Server(
                    endpoint=Endpoint("old.example", 27015),
                    game_id="g",
                    ping=55,
                    country="GB",
                )
            ],
        )
        release_blocker = threading.Event()
        blocked_started = threading.Event()
        resolver_lock = threading.Lock()
        resolver_calls = []
        resolver_threads = []
        lookup_snapshots = []
        database_lookups = []
        callbacks = []
        callback_finished = threading.Event()
        callback_elapsed = []

        class Database:
            def get(self, address):
                database_lookups.append(address)
                return {
                    "198.51.100.70": {"country_code": "US"},
                    "198.51.100.71": {"country_code": "CA"},
                }[address]

        def resolve(*args, **kwargs):
            with resolver_lock:
                resolver_calls.append((args, kwargs))
                resolver_threads.append(threading.current_thread())
                lookup_snapshots.append(
                    (
                        args[0],
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                    )
                )
            if args[0] == "blocked.example":
                blocked_started.set()
                release_blocker.wait()
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        ("198.51.100.70", 0),
                    )
                ]
            if args[0] == "working.example":
                return [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        ("198.51.100.71", 0),
                    )
                ]
            raise AssertionError(f"unexpected DNS lookup: {args[0]}")

        def add_rtt_info(_servers):
            return [
                Server(
                    endpoint=Endpoint("blocked.example", 1),
                    game_id="g",
                    ping=10,
                ),
                Server(
                    endpoint=Endpoint("working.example", 2),
                    game_id="g",
                    ping=20,
                ),
            ]

        def callback(game):
            callbacks.append(game)
            callback_elapsed.append(time.monotonic() - started_at)
            callback_finished.set()

        core.geolocation = Database()
        attempt = self.admit_attempt(core, callback)

        started_at = time.monotonic()
        query_thread = threading.Thread(
            target=attempt.run_worker,
            name="test-bounded-dns-query",
        )
        completed_before_release = False
        blocked_worker_alive_before_release = False
        query_alive_before_release = True
        state_before_release = None
        query_started = False
        pool_workers = []
        with installed_network_pools() as pools:
            try:
                with mock.patch(
                    "obozrenie.network_calls.socket.getaddrinfo",
                    side_effect=resolve,
                ), mock.patch(
                    "obozrenie.core._GEOIP_DNS_BATCH_TIMEOUT_SECONDS",
                    0.02,
                ), mock.patch(
                    "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
                ):
                    query_thread.start()
                    query_started = True
                    self.assertTrue(blocked_started.wait(1))
                    query_thread.join(0.2)
                    completed_before_release = callback_finished.is_set()
                    with resolver_lock:
                        blocked_worker_alive_before_release = bool(
                            resolver_threads and resolver_threads[0].is_alive()
                        )
                    query_alive_before_release = query_thread.is_alive()
                    state_before_release = (
                        core.game_table.get_query_status("g"),
                        core.game_table.get_servers_data("g"),
                        core.game_table.get_query_error("g"),
                        list(callbacks),
                    )
            finally:
                release_blocker.set()
                if query_started:
                    query_thread.join(1)
            pool_workers = list(pools.dns._workers)

        self.assertTrue(blocked_started.is_set())
        self.assertTrue(completed_before_release)
        self.assertTrue(blocked_worker_alive_before_release)
        self.assertFalse(query_alive_before_release)
        self.assertEqual(len(callback_elapsed), 1)
        self.assertLess(callback_elapsed[0], 0.2)
        self.assertEqual(
            state_before_release,
            (
                core.game_table.QUERY_STATUS.READY,
                [
                    Server(
                        endpoint=Endpoint("blocked.example", 1),
                        game_id="g",
                        ping=10,
                        country="",
                    ),
                    Server(
                        endpoint=Endpoint("working.example", 2),
                        game_id="g",
                        ping=20,
                        country="CA",
                    ),
                ],
                None,
                ["g"],
            ),
        )
        self.assertEqual(
            resolver_calls,
            [
                (("blocked.example", None), {"type": socket.SOCK_STREAM}),
                (("working.example", None), {"type": socket.SOCK_STREAM}),
            ],
        )
        self.assertEqual(len({id(worker) for worker in resolver_threads}), 2)
        self.assertTrue(all(worker.daemon for worker in resolver_threads))
        self.assertEqual(
            {worker.name for worker in resolver_threads},
            {"obozrenie-dns-resolver"},
        )
        self.assertEqual(len(pool_workers), 4)
        self.assertTrue(
            all(not worker.is_alive() for worker in pool_workers)
        )
        self.assertEqual(
            lookup_snapshots,
            [
                (
                    "blocked.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
                (
                    "working.example",
                    core.game_table.QUERY_STATUS.WORKING,
                    [
                        Server(
                            endpoint=Endpoint("old.example", 27015),
                            game_id="g",
                            ping=55,
                            country="GB",
                        )
                    ],
                ),
            ],
        )
        self.assertEqual(database_lookups, ["198.51.100.71"])
        self.assertEqual(
            core.game_table.get_servers_data("g"),
            [
                Server(
                    endpoint=Endpoint("blocked.example", 1),
                    game_id="g",
                    ping=10,
                    country="",
                ),
                Server(
                    endpoint=Endpoint("working.example", 2),
                    game_id="g",
                    ping=20,
                    country="CA",
                ),
            ],
        )
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))
        self.assertEqual(callbacks, ["g"])

    def test_mapping_adapter_result_sets_error_and_calls_back_once(self):
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [{"host": "new.example:1"}])},
        )
        core = self.make_core(adapter)
        old_servers = [
            Server(
                endpoint=Endpoint("old.example", 27015),
                game_id="g",
                country="GB",
            )
        ]
        self.seed_servers(core, old_servers)
        callbacks = []

        self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)
        self.assertEqual(callbacks, ["g"])

    def test_all_adapter_results_are_validated_before_enrichment(self):
        valid = Server(endpoint=Endpoint("valid.example", 1), game_id="g")
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [valid, {"host": "bad:1"}])},
        )
        core = self.make_core(adapter)
        looked_up_hosts = []
        rtt_batches = []

        def lookup_countries(hosts, deadline=None):
            looked_up_hosts.extend(hosts)
            return {host: "XY" for host in hosts}

        def add_rtt_info(servers):
            rtt_batches.append(list(servers))
            return []

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(rtt_batches, [])
        self.assertEqual(looked_up_hosts, [])
        self.assertEqual(core.game_table.get_servers_data("g"), [])

    def test_adapter_server_subclass_fails_before_rtt_or_country_lookup(self):
        exact_server = Server(
            endpoint=Endpoint("exact.example", 1), game_id="g"
        )
        mutable_server = _make_bypassed_server("mutable.example", 2)
        adapter = type(
            "Adapter",
            (),
            {
                "stat_master": staticmethod(
                    lambda *args: [exact_server, mutable_server]
                )
            },
        )
        core = self.make_core(adapter)
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            country="GB",
        )
        self.seed_servers(core, [old_server])
        rtt_batches = []
        looked_up_hosts = []

        def add_rtt_info(servers):
            rtt_batches.append(list(servers))
            return servers

        def lookup_countries(hosts, deadline=None):
            looked_up_hosts.extend(hosts)
            return {host: "CA" for host in hosts}

        core._lookup_countries = lookup_countries

        with mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=add_rtt_info
        ):
            self.run_query_worker(core)

        mutable_server.payload.append("mutated")
        self.assertEqual(rtt_batches, [])
        self.assertEqual(looked_up_hosts, [])
        self.assertEqual(core.game_table.get_servers_data("g"), [old_server])
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_all_gint_invalid_adapter_records_terminalize_query_before_ready(
        self,
    ):
        # Every entry names a value the delivered `gint` columns cannot hold,
        # so the adapter keeps no record and the query must never reach READY.
        overflowing_payload = {
            "list": [
                {
                    "address": "overflow-count.example",
                    "port": 30000,
                    "clients": 2147483648,
                    "clients_max": 64,
                },
                {
                    "address": "overflow-limit.example",
                    "port": 30001,
                    "clients": 0,
                    "clients_max": 2147483648,
                },
            ]
        }
        core = self.make_core(minetest)
        core.game_table.set_game_setting(
            "g",
            "master_uri",
            ["https://master.example/servers"],
        )
        old_server = Server(
            endpoint=Endpoint("old.example", 27015),
            game_id="g",
            ping=55,
            country="GB",
        )
        self.seed_servers(core, [old_server])
        callbacks = []

        with (
            mock.patch.object(
                core.game_table,
                "finish_query",
                wraps=core.game_table.finish_query,
            ) as finish_query,
            mock.patch(
                "obozrenie.http_client.get_json",
                return_value=overflowing_payload,
            ),
            mock.patch(
                "obozrenie.ping.add_rtt_info",
                side_effect=AssertionError("query enriched an empty batch"),
            ),
        ):
            self.run_query_worker(core, callbacks.append)

        finish_query.assert_not_called()
        self.assertEqual(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )
        self.assertEqual(core.game_table.get_servers_data("g"), [old_server])
        self.assertEqual(callbacks, ["g"])

    def test_logging_failure_does_not_interrupt_query_finalization(self):
        def fail_master(*args):
            raise RuntimeError("private backend detail")

        def fail_log(*args):
            raise BrokenPipeError("diagnostic output is closed")

        adapter = type("Adapter", (), {"stat_master": staticmethod(fail_master)})
        core = self.make_core(adapter)
        old_servers = [
            Server(
                endpoint=Endpoint("old.example", 27015),
                game_id="g",
                country="GB",
            )
        ]
        self.seed_servers(core, old_servers)
        callbacks = []
        raised = None

        with mock.patch("obozrenie.core.helpers.debug_msg", fail_log):
            try:
                self.run_query_worker(core, callbacks.append)
            except Exception as error:
                raised = error

        self.assertEqual(
            {
                "raised": raised,
                "status": core.game_table.get_query_status("g"),
                "error": core.game_table.get_query_error("g"),
                "servers": core.game_table.get_servers_data("g"),
                "callbacks": callbacks,
            },
            {
                "raised": None,
                "status": core.game_table.QUERY_STATUS.ERROR,
                "error": "Unable to refresh the server list.",
                "servers": old_servers,
                "callbacks": ["g"],
            },
        )

    def test_post_launch_start_exception_hands_completion_to_worker_once(self):
        adapter_entered = threading.Event()
        release_adapter = threading.Event()

        def stat_master(*_args):
            adapter_entered.set()
            if not release_adapter.wait(1):
                raise AssertionError("test did not release the query worker")
            return []

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        callbacks = []
        messages = []
        launched = []

        class PostLaunchFailureThread(threading.Thread):
            def start(self):
                super().start()
                launched.append(self)
                if not adapter_entered.wait(1):
                    raise AssertionError(
                        "query worker never claimed the attempt"
                    )
                raise RuntimeError("thread bookkeeping failed after launch")

        try:
            with mock.patch(
                "obozrenie.core.threading.Thread", PostLaunchFailureThread
            ), mock.patch(
                "obozrenie.core.helpers.debug_msg", messages.append
            ):
                admitted = core.update_server_list("g", callbacks.append)
                status_while_working = core.game_table.get_query_status("g")
                release_adapter.set()
                for thread in launched:
                    thread.join(1)
        finally:
            release_adapter.set()
            for thread in launched:
                thread.join(1)

        self.assertIs(admitted, True)
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0].is_alive())
        self.assertIs(
            status_while_working, core.game_table.QUERY_STATUS.WORKING
        )
        # The worker owned completion, so the caller only logged its own
        # failure safely instead of finalizing the query a second time.
        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected RuntimeError; exception details omitted."],
        )
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))

    def test_launched_but_unclaimed_start_base_exception_terminalizes_once(self):
        class StartAbort(BaseException):
            pass

        release_worker = threading.Event()
        adapter_calls = []

        def stat_master(*_args):
            adapter_calls.append("queried")
            return []

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        old_servers = [
            Server(
                endpoint=Endpoint("old.example", 27015),
                game_id="g",
                country="GB",
            )
        ]
        self.seed_servers(core, old_servers)
        caller_thread = threading.get_ident()
        callbacks = []
        launched = []
        messages = []
        abort = StartAbort("thread start interrupted after launch")

        def callback(game):
            callbacks.append((game, threading.get_ident()))

        class LaunchedStartFailureThread(threading.Thread):
            def __init__(self, target=None, **kwargs):
                self._released_target = target
                super().__init__(target=self._run_when_released, **kwargs)

            def _run_when_released(self):
                if not release_worker.wait(1):
                    raise AssertionError(
                        "test did not release the query worker"
                    )
                self._released_target()

            def start(self):
                super().start()
                launched.append(self)
                raise abort

        raised = None
        try:
            with mock.patch(
                "obozrenie.core.threading.Thread", LaunchedStartFailureThread
            ), mock.patch(
                "obozrenie.core.helpers.debug_msg", messages.append
            ):
                try:
                    core.update_server_list("g", callback)
                except StartAbort as error:
                    raised = error
            terminal_state = (
                core.game_table.get_query_status("g"),
                core.game_table.get_query_error("g"),
                list(callbacks),
            )
        finally:
            release_worker.set()
            for thread in launched:
                thread.join(1)

        self.assertIs(raised, abort)
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected StartAbort; exception details omitted."],
        )
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0].is_alive())
        self.assertEqual(
            terminal_state,
            (
                core.game_table.QUERY_STATUS.ERROR,
                "Unable to refresh the server list.",
                [("g", caller_thread)],
            ),
        )
        # A worker that could not claim a terminalized attempt never queries
        # the adapter and never repeats the callback.
        self.assertEqual(adapter_calls, [])
        self.assertEqual(callbacks, [("g", caller_thread)])
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )

    def test_worker_base_exception_terminalizes_once_then_reraises_identity(self):
        class WorkerAbort(BaseException):
            pass

        abort = WorkerAbort("worker interrupted; do not report")

        def stat_master(*_args):
            raise abort

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        old_servers = [
            Server(
                endpoint=Endpoint("old.example", 27015),
                game_id="g",
                country="GB",
            )
        ]
        self.seed_servers(core, old_servers)
        callbacks = []
        messages = []
        hook_calls = []

        with mock.patch(
            "obozrenie.core.helpers.debug_msg", messages.append
        ), mock.patch.object(threading, "excepthook", hook_calls.append):
            admitted, worker = self.launch_query(core, callbacks.append)
            worker.join(1)

        self.assertIs(admitted, True)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(hook_calls), 1)
        self.assertIs(hook_calls[0].exc_value, abort)
        self.assertIs(hook_calls[0].thread, worker)
        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected WorkerAbort; exception details omitted."],
        )
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)

    def test_worker_base_exception_identity_survives_callback_base_exception(self):
        class WorkerAbort(BaseException):
            pass

        class CallbackAbort(BaseException):
            pass

        abort = WorkerAbort("worker interrupted")

        def stat_master(*_args):
            raise abort

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        callbacks = []
        hook_calls = []

        def callback(game):
            callbacks.append(game)
            raise CallbackAbort("callback interrupted")

        with mock.patch(
            "obozrenie.core.helpers.debug_msg", lambda *_args: None
        ), mock.patch.object(threading, "excepthook", hook_calls.append):
            _admitted, worker = self.launch_query(core, callback)
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(hook_calls), 1)
        self.assertIs(hook_calls[0].exc_type, WorkerAbort)
        self.assertIs(hook_calls[0].exc_value, abort)
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_qstat_missing_executable_reaches_the_log_as_its_fixed_text(self):
        def stat_master(*_args):
            with mock.patch("shutil.which", return_value=None):
                return qstat.find_qstat()

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        callbacks = []
        messages = []

        with mock.patch("obozrenie.core.helpers.debug_msg", messages.append):
            self.run_query_worker(core, callbacks.append)

        # Without a declared diagnostic this would log only the exception
        # type, hiding the one instruction that resolves the failure.
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Install QStat (qstat or quakestat) to query this game."],
        )
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_safe_adapter_failure_uses_declared_diagnostic(self):
        declared = SafeDiagnostic("Master server declined the request.")

        def stat_master(*_args):
            raise SafeDiagnosticError(declared)

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        callbacks = []
        messages = []

        with mock.patch("obozrenie.core.helpers.debug_msg", messages.append):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(len(messages), 1)
        self.assertIs(messages[0][-1], declared)
        self.assertEqual(
            _diagnostic_texts(messages), ["Master server declined the request."]
        )
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_unexpected_query_failure_logs_only_exception_type(self):
        secret = "https://operator:hunter2@master.example/private"

        class MasterQueryError(Exception):
            pass

        def stat_master(*_args):
            raise MasterQueryError(secret)

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        callbacks = []
        messages = []

        with mock.patch("obozrenie.core.helpers.debug_msg", messages.append):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected MasterQueryError; exception details omitted."],
        )
        self.assertNotIn(
            "hunter2",
            " ".join(
                str(part) for message in messages for part in message
            ),
        )
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )

    def test_rtt_fallback_diagnostic_never_stringifies_exception(self):
        stringifications = []

        class UnreadableRttError(Exception):
            def __str__(self):
                stringifications.append("stringified")
                raise AssertionError(
                    "RTT diagnostics must not stringify the failure"
                )

        adapter_server = Server(
            endpoint=Endpoint("one.example", 30000),
            game_id="g",
            ping=42,
        )
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *args: [adapter_server])},
        )
        core = self.make_core(adapter)
        core._lookup_countries = lambda hosts, deadline=None: {
            host: "XY" for host in hosts
        }
        callbacks = []
        messages = []

        with mock.patch(
            "obozrenie.core.helpers.debug_msg", messages.append
        ), mock.patch(
            "obozrenie.ping.add_rtt_info", side_effect=UnreadableRttError
        ):
            self.run_query_worker(core, callbacks.append)

        self.assertEqual(stringifications, [])
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected UnreadableRttError; exception details omitted."],
        )
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        committed = core.game_table.get_servers_data("g")
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].endpoint, adapter_server.endpoint)
        self.assertEqual(committed[0].ping, 42)
        self.assertEqual(committed[0].country, "XY")

    def test_success_callback_failure_is_reported_without_recompleting(self):
        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(lambda *args: [])}
        )
        core = self.make_core(adapter)
        callbacks = []
        messages = []

        def callback(game):
            callbacks.append(game)
            raise RuntimeError("callback failed; do not report")

        with mock.patch("obozrenie.core.helpers.debug_msg", messages.append):
            self.run_query_worker(core, callback)

        self.assertEqual(callbacks, ["g"])
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected RuntimeError; exception details omitted."],
        )
        # The committed result stands; a failing callback cannot fail it.
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))

    def test_cancel_for_close_terminalizes_once_and_suppresses_callback(self):
        adapter_calls = []

        def stat_master(*_args):
            adapter_calls.append("queried")
            return []

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        old_servers = [
            Server(
                endpoint=Endpoint("old.example", 27015),
                game_id="g",
                country="GB",
            )
        ]
        self.seed_servers(core, old_servers)
        callbacks = []
        attempt = self.admit_attempt(core, callbacks.append)

        self.assertIs(attempt.cancel_for_close(), True)
        self.assertIs(attempt.cancel_for_close(), False)
        self.assertIs(attempt.claim_worker(), False)
        attempt.run_worker()

        self.assertEqual(callbacks, [])
        self.assertEqual(adapter_calls, [])
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertEqual(
            core.game_table.get_query_error("g"),
            "Unable to refresh the server list.",
        )


class CoreLifecycleTests(CoreQueryTestCase):
    def setUp(self):
        super().setUp()
        # Lifecycle tests never measure response times; keep every worker off
        # the network without changing the batch it enriches.
        rtt = mock.patch.object(
            core_module.ping, "add_rtt_info", lambda servers: list(servers)
        )
        rtt.start()
        self.addCleanup(rtt.stop)

    def blocking_adapter(self, entered, release):
        """Return an adapter whose query blocks until the test releases it."""

        def stat_master(game, *_args):
            entered[game].set()
            if not release.wait(1):
                raise AssertionError("test did not release the query worker")
            return []

        return type("Adapter", (), {"stat_master": staticmethod(stat_master)})

    def old_servers_for(self, game):
        return [
            Server(
                endpoint=Endpoint(f"old.{game}.example", 27015),
                game_id=game,
                country="GB",
            )
        ]

    def published_state(self, core, game="g"):
        """Snapshot everything one game's query publishes."""
        return (
            core.game_table.get_query_status(game),
            core.game_table.get_query_error(game),
            core.game_table.get_servers_data(game),
        )

    def assert_close_waits_for_commit(self, transition, stat_master):
        """Close must not return while one attempt is still publishing."""
        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        self.seed_servers(core, self.old_servers_for("g"))
        commit_entered = threading.Event()
        release_commit = threading.Event()
        closed = threading.Event()
        observed = []
        callbacks = []
        threads = []
        real_transition = getattr(core.game_table, transition)

        def blocking_transition(*args):
            commit_entered.set()
            if not release_commit.wait(1):
                raise AssertionError("test did not release the query worker")
            return real_transition(*args)

        def close_core():
            core.begin_close()
            observed.append(self.published_state(core))
            closed.set()

        try:
            with mock.patch.object(
                core.game_table, transition, blocking_transition
            ):
                _admitted, worker = self.launch_query(core, callbacks.append)
                threads.append(worker)
                self.assertTrue(commit_entered.wait(1))
                closer = threading.Thread(target=close_core, daemon=True)
                threads.append(closer)
                closer.start()
                # The transition this close would have to observe is still in
                # flight, so close cannot finish while it is pending.
                self.assertFalse(closed.wait(0.1))
                release_commit.set()
                for thread in threads:
                    thread.join(1)
        finally:
            release_commit.set()
            for thread in threads:
                thread.join(1)

        self.assertEqual([thread for thread in threads if thread.is_alive()], [])
        # What close observed on return is final: no query transition may land
        # after begin_close has returned.
        self.assertEqual(observed, [self.published_state(core)])
        self.assertEqual(callbacks, ["g"])

    def test_close_cannot_return_while_a_commit_is_in_flight(self):
        new_server = Server(
            endpoint=Endpoint("new.example", 27016), game_id="g"
        )

        def failing_master(*_args):
            raise RuntimeError("master query failed")

        for outcome, transition, stat_master in (
            ("success", "finish_query", lambda *_args: [new_server]),
            ("failure", "fail_query", failing_master),
        ):
            with self.subTest(outcome=outcome):
                self.assert_close_waits_for_commit(transition, stat_master)

    def test_begin_close_cancels_every_attempt_when_one_cancellation_fails(self):
        entered = {game: threading.Event() for game in TWO_GAME_CONFIG}
        release = threading.Event()
        core = self.make_core(
            self.blocking_adapter(entered, release), TWO_GAME_CONFIG
        )
        old_servers = {
            game: self.old_servers_for(game) for game in TWO_GAME_CONFIG
        }
        for game, servers in old_servers.items():
            self.seed_servers(core, servers, game=game)
        real_fail_query = core.game_table.fail_query

        def fail_query(game, token, error):
            if game == "g":
                raise RuntimeError("cancellation failed")
            return real_fail_query(game, token, error)

        callbacks = []
        messages = []
        workers = []
        try:
            for game in TWO_GAME_CONFIG:
                _admitted, worker = self.launch_query(
                    core, callbacks.append, game=game
                )
                workers.append(worker)
                self.assertTrue(entered[game].wait(1))
            with mock.patch.object(
                core.game_table, "fail_query", fail_query
            ), mock.patch.object(helpers, "debug_msg", messages.append):
                core.begin_close()
                # Every attempt is terminal, so a repeated close repeats
                # neither the cancellation nor its failure.
                core.begin_close()
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

        self.assertEqual([worker for worker in workers if worker.is_alive()], [])
        self.assertIs(core.is_closing, True)
        self.assertEqual(callbacks, [])
        # One failing cancellation cannot strand the other attempt.
        self.assertEqual(
            self.published_state(core, "h"),
            (
                core.game_table.QUERY_STATUS.ERROR,
                "Unable to refresh the server list.",
                old_servers["h"],
            ),
        )
        # The attempt whose publish failed is terminal but unpublished, and a
        # retry cannot publish it a second time.
        self.assertEqual(
            self.published_state(core, "g"),
            (core.game_table.QUERY_STATUS.WORKING, None, old_servers["g"]),
        )
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected RuntimeError; exception details omitted."],
        )

    def test_begin_close_retries_attempts_an_interrupted_cancellation_left(self):
        class CancelAbort(BaseException):
            pass

        entered = {game: threading.Event() for game in TWO_GAME_CONFIG}
        release = threading.Event()
        core = self.make_core(
            self.blocking_adapter(entered, release), TWO_GAME_CONFIG
        )
        old_servers = {
            game: self.old_servers_for(game) for game in TWO_GAME_CONFIG
        }
        for game, servers in old_servers.items():
            self.seed_servers(core, servers, game=game)
        real_fail_query = core.game_table.fail_query
        cancelled = []
        abort = CancelAbort("close interrupted")

        def fail_query(game, token, error):
            cancelled.append(game)
            if len(cancelled) == 1:
                raise abort
            return real_fail_query(game, token, error)

        callbacks = []
        workers = []
        raised = None
        try:
            for game in TWO_GAME_CONFIG:
                _admitted, worker = self.launch_query(
                    core, callbacks.append, game=game
                )
                workers.append(worker)
                self.assertTrue(entered[game].wait(1))
            with mock.patch.object(core.game_table, "fail_query", fail_query):
                try:
                    core.begin_close()
                except CancelAbort as error:
                    raised = error
                # An interrupted pass forgot nothing it had not cancelled.
                core.begin_close()
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

        self.assertEqual([worker for worker in workers if worker.is_alive()], [])
        self.assertIs(raised, abort)
        self.assertEqual(len(cancelled), 2)
        self.assertNotEqual(cancelled[0], cancelled[1])
        interrupted, retried = cancelled
        self.assertEqual(
            self.published_state(core, retried),
            (
                core.game_table.QUERY_STATUS.ERROR,
                "Unable to refresh the server list.",
                old_servers[retried],
            ),
        )
        # The interrupted attempt is terminal but never published, so the
        # retry could only reach the attempt it had not touched.
        self.assertEqual(
            self.published_state(core, interrupted),
            (
                core.game_table.QUERY_STATUS.WORKING,
                None,
                old_servers[interrupted],
            ),
        )
        self.assertEqual(callbacks, [])

    def test_begin_close_terminalizes_all_attempts_and_rejects_new_admission(self):
        entered = {game: threading.Event() for game in TWO_GAME_CONFIG}
        release = threading.Event()
        core = self.make_core(
            self.blocking_adapter(entered, release), TWO_GAME_CONFIG
        )
        old_servers = {
            game: self.old_servers_for(game) for game in TWO_GAME_CONFIG
        }
        for game, servers in old_servers.items():
            self.seed_servers(core, servers, game=game)
        callbacks = []
        workers = []

        try:
            for game in TWO_GAME_CONFIG:
                admitted, worker = self.launch_query(
                    core, callbacks.append, game=game
                )
                workers.append(worker)
                self.assertIs(admitted, True)
                self.assertTrue(entered[game].wait(1))
            was_closing = core.is_closing

            core.begin_close()

            closed_state = {
                game: (
                    core.game_table.get_query_status(game),
                    core.game_table.get_query_error(game),
                    core.game_table.get_servers_data(game),
                )
                for game in TWO_GAME_CONFIG
            }
            with mock.patch(
                "obozrenie.core.threading.Thread"
            ) as thread_factory:
                readmitted = core.update_server_list("g", callbacks.append)
            readmitted_calls = thread_factory.call_count
        finally:
            release.set()
            for worker in workers:
                worker.join(1)

        self.assertIs(was_closing, False)
        self.assertIs(core.is_closing, True)
        self.assertEqual(
            closed_state,
            {
                game: (
                    core.game_table.QUERY_STATUS.ERROR,
                    "Unable to refresh the server list.",
                    old_servers[game],
                )
                for game in TWO_GAME_CONFIG
            },
        )
        # A closing Core admits nothing, even though cancellation left every
        # game in a terminal state that would otherwise accept a new query.
        self.assertIs(readmitted, False)
        self.assertEqual(readmitted_calls, 0)
        self.assertEqual([worker for worker in workers if worker.is_alive()], [])
        # Cancellation suppressed the callback, and the released workers could
        # not commit over the terminal state close published for them.
        self.assertEqual(callbacks, [])
        self.assertEqual(
            {
                game: (
                    core.game_table.get_query_status(game),
                    core.game_table.get_servers_data(game),
                )
                for game in TWO_GAME_CONFIG
            },
            {
                game: (core.game_table.QUERY_STATUS.ERROR, old_servers[game])
                for game in TWO_GAME_CONFIG
            },
        )

    def test_late_worker_after_close_cannot_commit_or_invoke_callback(self):
        completion_won = threading.Event()
        release_worker = threading.Event()

        class LateCompletionAttempt(core_module._QueryAttempt):
            """Win the completion race, then wait for the test to close Core."""

            def _take_completion(self):
                won = super()._take_completion()
                if won:
                    completion_won.set()
                    if not release_worker.wait(1):
                        raise AssertionError(
                            "test did not release the query worker"
                        )
                return won

        new_server = Server(
            endpoint=Endpoint("new.example", 27016), game_id="g"
        )
        adapter = type(
            "Adapter",
            (),
            {"stat_master": staticmethod(lambda *_args: [new_server])},
        )
        core = self.make_core(adapter)
        old_servers = self.old_servers_for("g")
        self.seed_servers(core, old_servers)
        callbacks = []
        workers = []

        try:
            with mock.patch.object(
                core_module, "_QueryAttempt", LateCompletionAttempt
            ):
                admitted, worker = self.launch_query(core, callbacks.append)
            workers.append(worker)
            self.assertTrue(completion_won.wait(1))

            # The worker already owns completion, so close cannot terminalize
            # its attempt; only Core's own close state can hold it back.
            core.begin_close()
            closed_state = (
                core.game_table.get_query_status("g"),
                core.game_table.get_query_error("g"),
                core.game_table.get_servers_data("g"),
                list(callbacks),
            )
        finally:
            release_worker.set()
            for worker in workers:
                worker.join(1)

        self.assertIs(admitted, True)
        self.assertEqual([worker for worker in workers if worker.is_alive()], [])
        self.assertEqual(
            closed_state,
            (core.game_table.QUERY_STATUS.WORKING, None, old_servers, []),
        )
        # The late worker neither published its servers nor reached the UI.
        self.assertEqual(callbacks, [])
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.WORKING,
        )
        self.assertIsNone(core.game_table.get_query_error("g"))

    def test_join_until_tracks_post_launch_start_failure_thread(self):
        entered = {"g": threading.Event()}
        release = threading.Event()
        core = self.make_core(self.blocking_adapter(entered, release))
        joins = []
        launched = []

        class PostLaunchFailureThread(threading.Thread):
            def start(self):
                super().start()
                launched.append(self)
                if not entered["g"].wait(1):
                    raise AssertionError(
                        "query worker never claimed the attempt"
                    )
                raise RuntimeError("thread bookkeeping failed after launch")

            def join(self, timeout=None):
                joins.append(timeout)
                super().join(timeout)

        callbacks = []
        try:
            with mock.patch(
                "obozrenie.core.threading.Thread", PostLaunchFailureThread
            ):
                admitted = core.update_server_list("g", callbacks.append)
            # The launch failed after the thread was running, so Core cannot
            # prove it never started and must keep joining it.
            core.join_until(time.monotonic() + 0.2)
            joined_while_blocked = list(joins)
            alive_after_join = launched[0].is_alive()
        finally:
            release.set()
            for thread in launched:
                thread.join(1)

        self.assertIs(admitted, True)
        self.assertEqual(len(launched), 1)
        self.assertTrue(alive_after_join)
        self.assertEqual(len(joined_while_blocked), 1)
        self.assertGreater(joined_while_blocked[0], 0)
        self.assertLessEqual(joined_while_blocked[0], 0.2)
        self.assertFalse(launched[0].is_alive())
        self.assertEqual(callbacks, ["g"])

        # The exited target released its own registration, so a later join has
        # nothing left to wait for.
        joins.clear()
        core.join_until(time.monotonic() + 1.0)
        self.assertEqual(joins, [])

    def test_join_until_tracks_a_claiming_worker_whose_start_was_interrupted(
        self,
    ):
        class StartAbort(BaseException):
            pass

        entered = {"g": threading.Event()}
        release = threading.Event()
        core = self.make_core(self.blocking_adapter(entered, release))
        joins = []
        launched = []
        abort = StartAbort("thread start interrupted before its child ran")

        class UnidentifiedStartFailureThread(threading.Thread):
            """A worker whose `start()` failed before its child had identity.

            CPython publishes `Thread.ident` from inside the new thread, so a
            `BaseException` escaping `start()` can leave a launched worker
            that Core cannot yet tell apart from one that never ran.
            """

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._identity_published = False
                launched.append(self)

            @property
            def ident(self):
                return super().ident if self._identity_published else None

            def publish_identity(self):
                """Publish the identity the child's own bootstrap would."""
                self._identity_published = True

            def start(self):
                super().start()
                if not entered["g"].wait(1):
                    raise AssertionError(
                        "query worker never claimed the attempt"
                    )
                raise abort

            def join(self, timeout=None):
                joins.append(timeout)
                super().join(timeout)

        callbacks = []
        raised = None
        try:
            with mock.patch(
                "obozrenie.core.threading.Thread",
                UnidentifiedStartFailureThread,
            ):
                try:
                    core.update_server_list("g", callbacks.append)
                except StartAbort as error:
                    raised = error
            # The worker claimed the attempt before the interruption, so it
            # owns the query; its identity only appears afterwards.
            launched[0].publish_identity()
            core.join_until(time.monotonic() + 0.2)
            joined_while_blocked = list(joins)
            alive_after_join = launched[0].is_alive()
        finally:
            release.set()
            for thread in launched:
                thread.join(1)

        self.assertIs(raised, abort)
        self.assertEqual(len(launched), 1)
        self.assertTrue(alive_after_join)
        self.assertEqual(len(joined_while_blocked), 1)
        self.assertGreater(joined_while_blocked[0], 0)
        self.assertLessEqual(joined_while_blocked[0], 0.2)
        self.assertFalse(launched[0].is_alive())
        self.assertEqual(callbacks, ["g"])
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.READY,
        )

    def test_join_until_skips_a_registered_thread_that_has_not_launched(self):
        # Admission registers the worker thread before it starts, so a join
        # that runs in between finds a thread with no identity: it has
        # nothing it can wait for, returns at once, and leaves the thread
        # tracked, because it is about to run and only its admitting caller
        # could prove otherwise.
        entered = {"g": threading.Event()}
        release = threading.Event()
        core = self.make_core(self.blocking_adapter(entered, release))
        joins = []
        launched = []
        joins_before_launch = []

        class ObservedLaunchThread(threading.Thread):
            def start(self):
                started = time.monotonic()
                core.join_until(time.monotonic() + 1.0)
                joins_before_launch.append(
                    (time.monotonic() - started, list(joins))
                )
                super().start()
                launched.append(self)

            def join(self, timeout=None):
                joins.append(timeout)
                super().join(timeout)

        callbacks = []
        try:
            with mock.patch(
                "obozrenie.core.threading.Thread", ObservedLaunchThread
            ):
                admitted = core.update_server_list("g", callbacks.append)
            self.assertTrue(entered["g"].wait(1))
            # Once running, that same registration is what a join waits on.
            core.join_until(time.monotonic() + 0.2)
            joined_while_blocked = list(joins)
        finally:
            release.set()
            for thread in launched:
                thread.join(1)

        self.assertIs(admitted, True)
        [(elapsed_before_launch, seen_before_launch)] = joins_before_launch
        self.assertLess(elapsed_before_launch, 0.5)
        self.assertEqual(seen_before_launch, [])
        self.assertEqual(len(joined_while_blocked), 1)
        self.assertGreater(joined_while_blocked[0], 0)
        self.assertLessEqual(joined_while_blocked[0], 0.2)
        self.assertFalse(launched[0].is_alive())
        self.assertEqual(callbacks, ["g"])

    def test_late_worker_failure_after_close_is_logged_but_never_published(self):
        completion_won = threading.Event()
        release_worker = threading.Event()

        class LateFailureAttempt(core_module._QueryAttempt):
            """Win the completion race, then wait for the test to close Core."""

            def _take_completion(self):
                won = super()._take_completion()
                if won:
                    completion_won.set()
                    if not release_worker.wait(1):
                        raise AssertionError(
                            "test did not release the query worker"
                        )
                return won

        failure = RuntimeError("adapter failed while close began")

        def stat_master(*_args):
            raise failure

        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(stat_master)}
        )
        core = self.make_core(adapter)
        old_servers = self.old_servers_for("g")
        self.seed_servers(core, old_servers)
        callbacks = []
        messages = []
        workers = []

        try:
            with mock.patch.object(
                core_module, "_QueryAttempt", LateFailureAttempt
            ), mock.patch(
                "obozrenie.core.helpers.debug_msg", messages.append
            ):
                admitted, worker = self.launch_query(core, callbacks.append)
                workers.append(worker)
                self.assertTrue(completion_won.wait(1))
                # The worker already owns completion, so close cannot
                # terminalize its attempt; only the closing check inside the
                # failure report can hold its publication back.
                core.begin_close()
                release_worker.set()
                worker.join(1)
        finally:
            release_worker.set()
            for worker in workers:
                worker.join(1)

        self.assertIs(admitted, True)
        self.assertEqual([worker for worker in workers if worker.is_alive()], [])
        # Close owns the outcome, but the failure itself is still reported.
        self.assertEqual(
            _diagnostic_texts(messages),
            ["Unexpected RuntimeError; exception details omitted."],
        )
        self.assertEqual(callbacks, [])
        self.assertEqual(
            self.published_state(core),
            (core.game_table.QUERY_STATUS.WORKING, None, old_servers),
        )

    def test_join_until_uses_one_absolute_deadline_for_all_tracked_threads(self):
        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(lambda *_args: [])}
        )
        core = self.make_core(adapter, TWO_GAME_CONFIG)
        clock = _FakeClock(1000.0)
        deadline = 1001.0
        joins = []
        factory, threads = _recording_threads(joins, clock=clock, step=0.25)

        with mock.patch("obozrenie.core.threading.Thread", factory):
            for game in TWO_GAME_CONFIG:
                self.assertIs(core.update_server_list(game), True)
        self.assertEqual(len(threads), 2)

        # A malformed deadline is rejected before any thread state is read.
        with self.assertRaises(TypeError):
            core.join_until(1)
        with self.assertRaises(ValueError):
            core.join_until(float("inf"))
        self.assertEqual(joins, [])

        # Every join is derived from the supplied deadline, not from a fresh
        # per-thread budget: each waits exactly until the clock reaches it.
        with mock.patch.object(core_module, "time", clock):
            core.join_until(deadline)

        self.assertEqual([timeout for _thread, timeout in joins], [1.0, 0.75])
        self.assertEqual({thread for thread, _timeout in joins}, set(threads))

        # A thread that outlives one join stays tracked, and repeating the
        # close deadline keeps shrinking the same absolute budget.
        joins.clear()
        with mock.patch.object(core_module, "time", clock):
            core.join_until(deadline)

        self.assertEqual([timeout for _thread, timeout in joins], [0.5, 0.25])
        self.assertEqual({thread for thread, _timeout in joins}, set(threads))

    def test_close_until_is_idempotent(self):
        adapter = type(
            "Adapter", (), {"stat_master": staticmethod(lambda *_args: [])}
        )
        core = self.make_core(adapter)
        old_servers = self.old_servers_for("g")
        self.seed_servers(core, old_servers)
        joins = []
        factory, threads = _recording_threads(joins)
        callbacks = []

        with mock.patch("obozrenie.core.threading.Thread", factory):
            self.assertIs(core.update_server_list("g", callbacks.append), True)
        self.assertEqual(len(threads), 1)
        worker = threads[0]
        deadline = time.monotonic() + 1.0

        # A malformed deadline is rejected before Core is half closed.
        with self.assertRaises(TypeError):
            core.close_until(1)
        self.assertIs(core.is_closing, False)

        with mock.patch.object(
            core.game_table, "fail_query", wraps=core.game_table.fail_query
        ) as fail_query:
            core.close_until(deadline)
            closed_once = (
                fail_query.call_count,
                len(joins),
                core.game_table.get_query_status("g"),
                core.game_table.get_query_error("g"),
            )
            core.close_until(deadline)
            # The worker stopped without running its target, so only a join can
            # observe that Core may stop tracking its thread.
            worker.alive = False
            core.close_until(deadline)
            core.close_until(deadline)

        self.assertEqual(
            closed_once,
            (
                1,
                1,
                core.game_table.QUERY_STATUS.ERROR,
                "Unable to refresh the server list.",
            ),
        )
        # Repeated close retries the incomplete join and stops once the thread
        # is gone, without terminalizing the cancelled attempt again.
        self.assertEqual(fail_query.call_count, 1)
        self.assertEqual([thread for thread, _timeout in joins], [worker] * 3)
        self.assertIs(core.is_closing, True)
        self.assertEqual(callbacks, [])
        self.assertEqual(core.game_table.get_servers_data("g"), old_servers)
        self.assertIs(
            core.game_table.get_query_status("g"),
            core.game_table.QUERY_STATUS.ERROR,
        )
        self.assertIs(core.update_server_list("g", callbacks.append), False)


if __name__ == "__main__":
    unittest.main()
