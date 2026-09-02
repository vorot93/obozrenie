import contextlib
import gzip
import http.client
import io
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import maxminddb
import requests
import urllib3
from requests.structures import CaseInsensitiveDict

from obozrenie import (
    core as core_module,
    geoip,
    helpers,
    http_client,
    network_calls,
)
from obozrenie.core import Core
from obozrenie.diagnostics import SafeDiagnostic
from obozrenie.network_calls import (
    CallDeadlineExceeded,
    CallOutcome,
    PoolSaturatedError,
    PoolUnavailableError,
)


class _StartupAbort(BaseException):
    pass


class _CallerAbort(BaseException):
    pass


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


def _capture_error(callback):
    try:
        callback()
    except BaseException as error:
        return error
    raise AssertionError("expected callback to raise")


class NetworkPoolFixtureTests(unittest.TestCase):
    def test_registry_patch_is_restored_when_fixture_close_raises(self):
        original_pools = network_calls.get_network_call_pools()
        cleanup_error = _CallerAbort("fixture close failed")

        def use_fixture():
            with installed_network_pools():
                pass

        try:
            with mock.patch.object(
                network_calls,
                "close_network_call_pools",
                side_effect=cleanup_error,
            ):
                raised = _capture_error(use_fixture)
                registry_after_close = network_calls.get_network_call_pools()
        finally:
            network_calls._NETWORK_CALL_POOLS = original_pools

        self.assertIs(raised, cleanup_error)
        self.assertIs(registry_after_close, original_pools)


def _candidate(address, family=socket.AF_INET):
    if family == socket.AF_INET6:
        socket_address = (address, 0, 0, 0)
    else:
        socket_address = (address, 0)
    return (family, socket.SOCK_STREAM, 6, "", socket_address)


class _RecordingDatabase:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.lookups = []

    def get(self, address):
        self.lookups.append(address)
        outcome = self.outcomes[address]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DnsFacadeTests(unittest.TestCase):
    def test_empty_batch_returns_before_registry_lookup_or_startup(self):
        with mock.patch.object(
            network_calls,
            "get_network_call_pools",
            side_effect=AssertionError("empty DNS batch resolved the registry"),
        ):
            self.assertEqual(
                network_calls.getaddrinfo_many_until((), time.monotonic()),
                [],
            )

    def test_startup_base_exception_becomes_fresh_unavailable_outcomes(self):
        initial_error = _StartupAbort("payload", bytearray(1024 * 1024))

        class StartupPool:
            def __init__(self):
                self.ensure_calls = 0
                self.map_calls = 0

            def ensure_started(self):
                self.ensure_calls += 1
                raise initial_error

            def map_until(self, *_args):
                self.map_calls += 1
                raise AssertionError("startup failure reached map phase")

        pool = StartupPool()
        pools = network_calls.NetworkCallPools(
            dns=pool,
            http=mock.Mock(),
            icmp=mock.Mock(),
        )
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            outcomes = network_calls.getaddrinfo_many_until(
                ("one.example", "two.example"),
                time.monotonic() + 1,
            )

        errors = [outcome.error for outcome in outcomes]
        self.assertEqual(pool.ensure_calls, 1)
        self.assertEqual(pool.map_calls, 0)
        self.assertEqual(len(errors), 2)
        self.assertEqual(len({id(error) for error in errors}), 2)
        self.assertTrue(
            all(type(error) is PoolUnavailableError for error in errors)
        )
        self.assertTrue(all(error is not initial_error for error in errors))

    def test_later_map_caller_interruption_is_not_caught_as_startup(self):
        startup_error = _StartupAbort("first startup interrupted")
        caller_interruption = _CallerAbort("caller interrupted map")

        class PhasePool:
            def __init__(self):
                self.ensure_calls = 0
                self.map_calls = 0

            def ensure_started(self):
                self.ensure_calls += 1
                if self.ensure_calls == 1:
                    raise startup_error

            def map_until(self, *_args):
                self.map_calls += 1
                raise caller_interruption

        pool = PhasePool()
        pools = network_calls.NetworkCallPools(
            dns=pool,
            http=mock.Mock(),
            icmp=mock.Mock(),
        )
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            first = network_calls.getaddrinfo_many_until(
                ("first.example",),
                time.monotonic() + 1,
            )
            try:
                network_calls.getaddrinfo_many_until(
                    ("later.example",),
                    time.monotonic() + 1,
                )
            except BaseException as raised:
                second_error = raised
            else:
                second_error = None

        self.assertIs(type(first[0].error), PoolUnavailableError)
        self.assertIs(second_error, caller_interruption)
        self.assertEqual(pool.ensure_calls, 2)
        self.assertEqual(pool.map_calls, 1)

    def test_close_between_ensure_and_map_becomes_fresh_unavailable_outcomes(self):
        with installed_network_pools() as pools:
            real_map_until = pools.dns.map_until

            def close_then_map(*args):
                pools.dns.begin_close()
                return real_map_until(*args)

            with mock.patch.object(
                pools.dns,
                "map_until",
                side_effect=close_then_map,
            ) as map_until:
                outcomes = network_calls.getaddrinfo_many_until(
                    ("one.example", "two.example"),
                    time.monotonic() + 1,
                )

        errors = [outcome.error for outcome in outcomes]
        self.assertEqual(map_until.call_count, 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual(len({id(error) for error in errors}), 2)
        self.assertTrue(
            all(type(error) is PoolUnavailableError for error in errors)
        )

    def test_map_unavailable_subclass_propagates_by_identity(self):
        class UnexpectedUnavailable(PoolUnavailableError):
            pass

        map_error = UnexpectedUnavailable("unexpected DNS map failure")
        pool = mock.Mock()
        pool.map_until.side_effect = map_error
        pools = network_calls.NetworkCallPools(
            dns=pool,
            http=mock.Mock(),
            icmp=mock.Mock(),
        )
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            raised = _capture_error(
                lambda: network_calls.getaddrinfo_many_until(
                    ("server.example",),
                    time.monotonic() + 1,
                )
            )

        self.assertIs(raised, map_error)
        pool.ensure_started.assert_called_once_with()
        pool.map_until.assert_called_once()


class CoreGeoIPTests(unittest.TestCase):
    def make_core(self, database):
        """Build a real Core whose geolocation is one test database.

        Core initialises its lifecycle state in `__init__`, so a fixture that
        bypassed the constructor would break the moment lookup consults it.
        """
        with (
            mock.patch.object(
                core_module.resources, "load_toml", return_value={}
            ),
            mock.patch.object(
                core_module.geoip, "find_database", return_value=None
            ),
            # Construction reports the absent database; these tests assert
            # their own diagnostics, so a passing run prints nothing.
            mock.patch.object(helpers, "debug_msg"),
        ):
            core = Core()
        core.geolocation = database
        return core

    def test_geoip_fixture_core_carries_its_lifecycle_state(self):
        # Country lookup shares Core with the query lifecycle, whose state
        # exists only when `Core.__init__` ran, so this fixture constructs one.
        self.assertIs(self.make_core(None).is_closing, False)

    def test_disabled_geolocation_returns_unknown_without_dns(self):
        core = self.make_core(None)
        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            side_effect=AssertionError("disabled geolocation used DNS"),
            create=True,
        ):
            countries = core._lookup_countries(
                ("server.example", "192.0.2.1", "server.example")
            )

        self.assertEqual(
            countries,
            {"server.example": "unknown", "192.0.2.1": "unknown"},
        )
        self.assertEqual(core._lookup_country("server.example"), "unknown")

    def test_lookup_separates_literals_and_one_first_seen_hostname_batch(self):
        database = _RecordingDatabase(
            {
                "192.0.2.10": {"country_code": "US"},
                "2001:db8::10": {"country": {"iso_code": "DE"}},
                "198.51.100.1": {"country_code": "CA"},
            }
        )
        core = self.make_core(database)
        hosts = (
            "192.0.2.10",
            "one.example",
            "one.example",
            "2001:db8::10",
            "missing.example",
        )
        facade_calls = []

        def resolve_many(names, deadline):
            facade_calls.append((tuple(names), deadline))
            return [
                CallOutcome.success([_candidate("198.51.100.1")]),
                CallOutcome.failure(socket.gaierror("not found")),
            ]

        before_deadline = (
            time.monotonic() + core_module._GEOIP_DNS_BATCH_TIMEOUT_SECONDS
        )
        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            side_effect=resolve_many,
            create=True,
        ):
            countries = core._lookup_countries(hosts)
        after_deadline = (
            time.monotonic() + core_module._GEOIP_DNS_BATCH_TIMEOUT_SECONDS
        )

        self.assertEqual(
            countries,
            {
                "192.0.2.10": "US",
                "one.example": "CA",
                "2001:db8::10": "DE",
                "missing.example": "",
            },
        )
        self.assertEqual(len(facade_calls), 1)
        names, deadline = facade_calls[0]
        self.assertEqual(names, ("one.example", "missing.example"))
        self.assertGreaterEqual(deadline, before_deadline)
        self.assertLessEqual(deadline, after_deadline)
        self.assertEqual(
            database.lookups,
            ["192.0.2.10", "198.51.100.1", "2001:db8::10"],
        )

    def test_supplied_deadline_is_forwarded_unchanged(self):
        core = self.make_core(_RecordingDatabase({}))
        deadline = time.monotonic() + 123.0
        calls = []

        def resolve_many(hosts, supplied_deadline):
            calls.append((tuple(hosts), supplied_deadline))
            return [CallOutcome.failure(socket.gaierror("missing"))]

        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            side_effect=resolve_many,
            create=True,
        ):
            countries = core._lookup_countries(("server.example",), deadline)

        self.assertEqual(countries, {"server.example": ""})
        self.assertEqual(calls, [(('server.example',), deadline)])

    def test_ipv4_and_ipv6_literals_bypass_dns_and_reach_mmdb_unchanged(self):
        database = _RecordingDatabase(
            {
                "192.0.2.44": {"country_code": "US"},
                "2001:0DB8:0000:0000:0000:0000:0000:0042": {
                    "country": {"iso_code": "DE"}
                },
            }
        )
        core = self.make_core(database)
        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            side_effect=AssertionError("IP literals used DNS"),
            create=True,
        ):
            countries = core._lookup_countries(tuple(database.outcomes))

        self.assertEqual(
            countries,
            {
                "192.0.2.44": "US",
                "2001:0DB8:0000:0000:0000:0000:0000:0042": "DE",
            },
        )
        self.assertEqual(database.lookups, list(database.outcomes))

    def test_first_valid_family_matching_candidate_reaches_mmdb(self):
        candidates = [
            (9999, socket.SOCK_STREAM, 0, "", ("192.0.2.200", 0)),
            _candidate("not-an-address", socket.AF_INET6),
            _candidate("2001:db8::8", socket.AF_INET),
            _candidate("198.51.100.8", socket.AF_INET),
            _candidate("2001:db8::9", socket.AF_INET6),
        ]
        database = _RecordingDatabase(
            {"198.51.100.8": {"country_code": "NZ"}}
        )
        core = self.make_core(database)
        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            return_value=[CallOutcome.success(candidates)],
            create=True,
        ):
            country = core._lookup_country("server.example")

        self.assertEqual(country, "NZ")
        self.assertEqual(database.lookups, ["198.51.100.8"])

    def test_expected_dns_outcomes_blank_only_affected_names(self):
        database = _RecordingDatabase(
            {"198.51.100.20": {"country_code": "CA"}}
        )
        core = self.make_core(database)
        expected_errors = (
            socket.gaierror("missing"),
            UnicodeEncodeError("idna", "x", 0, 1, "bad name"),
            CallDeadlineExceeded("deadline"),
            PoolSaturatedError("saturated"),
            PoolUnavailableError("closed"),
        )
        names = ("working.example",) + tuple(
            f"failed-{index}.example" for index in range(len(expected_errors))
        )
        outcomes = [
            CallOutcome.success([_candidate("198.51.100.20")]),
            *(CallOutcome.failure(error) for error in expected_errors),
        ]

        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            return_value=outcomes,
            create=True,
        ):
            countries = core._lookup_countries(names)

        self.assertEqual(countries["working.example"], "CA")
        self.assertEqual(
            [countries[name] for name in names[1:]],
            [""] * len(expected_errors),
        )
        self.assertEqual(database.lookups, ["198.51.100.20"])

    def test_derived_infrastructure_dns_outcomes_propagate_by_identity(self):
        class DerivedDeadline(CallDeadlineExceeded):
            pass

        class DerivedSaturated(PoolSaturatedError):
            pass

        class DerivedUnavailable(PoolUnavailableError):
            pass

        class InfrastructureGaiError(PoolUnavailableError, socket.gaierror):
            pass

        errors = (
            DerivedDeadline("derived deadline"),
            DerivedSaturated("derived saturation"),
            DerivedUnavailable("derived closure"),
            InfrastructureGaiError("multiple inheritance"),
        )
        for outcome_error in errors:
            with self.subTest(error=type(outcome_error).__name__):
                core = self.make_core(_RecordingDatabase({}))
                with mock.patch.object(
                    core_module,
                    "getaddrinfo_many_until",
                    return_value=[CallOutcome.failure(outcome_error)],
                    create=True,
                ):
                    raised = _capture_error(
                        lambda: core._lookup_countries(("server.example",))
                    )

                self.assertIs(raised, outcome_error)

    def test_closed_dns_pool_blanks_name_but_not_literal(self):
        database = _RecordingDatabase(
            {"192.0.2.30": {"country_code": "US"}}
        )
        core = self.make_core(database)
        with installed_network_pools() as pools:
            pools.dns.begin_close()
            countries = core._lookup_countries(
                ("closed.example", "192.0.2.30"),
                time.monotonic() + 0.02,
            )

        self.assertEqual(
            countries,
            {"closed.example": "", "192.0.2.30": "US"},
        )
        self.assertEqual(database.lookups, ["192.0.2.30"])

    def test_first_and_later_startup_failures_blank_names(self):
        database = _RecordingDatabase(
            {"192.0.2.31": {"country_code": "US"}}
        )
        core = self.make_core(database)
        initial_error = _StartupAbort("thread startup interrupted")
        with installed_network_pools() as pools, mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ) as thread_factory:
            first = core._lookup_countries(
                ("first.example", "192.0.2.31"),
                time.monotonic() + 0.02,
            )
            second = core._lookup_countries(
                ("later.example",),
                time.monotonic() + 0.02,
            )

            self.assertTrue(pools.dns.startup_failed)

        self.assertEqual(
            first,
            {"first.example": "", "192.0.2.31": "US"},
        )
        self.assertEqual(second, {"later.example": ""})
        self.assertEqual(thread_factory.call_count, 1)
        self.assertEqual(database.lookups, ["192.0.2.31"])

    def test_saturated_dns_pool_blanks_name_without_invoking_resolver(self):
        database = _RecordingDatabase(
            {"192.0.2.32": {"country_code": "US"}}
        )
        core = self.make_core(database)
        release = threading.Event()
        all_started = threading.Event()
        state_lock = threading.Lock()
        started_count = 0

        def block():
            nonlocal started_count
            with state_lock:
                started_count += 1
                if started_count == 4:
                    all_started.set()
            if not release.wait(2):
                raise AssertionError("test did not release DNS saturation")

        with installed_network_pools() as pools:
            calls = [pools.dns.submit(block) for _index in range(4)]
            try:
                self.assertTrue(all_started.wait(1))
                with mock.patch.object(
                    network_calls.socket,
                    "getaddrinfo",
                    side_effect=AssertionError("saturated call reached resolver"),
                ):
                    countries = core._lookup_countries(
                        ("saturated.example", "192.0.2.32"),
                        time.monotonic() + 0.02,
                    )
            finally:
                release.set()
                for call in calls:
                    self.assertTrue(call._event.wait(1))

        self.assertEqual(
            countries,
            {"saturated.example": "", "192.0.2.32": "US"},
        )
        self.assertEqual(database.lookups, ["192.0.2.32"])

    def test_blocked_name_does_not_hide_other_initial_results(self):
        database = _RecordingDatabase(
            {
                "198.51.100.40": {"country_code": "CA"},
                "192.0.2.40": {"country_code": "US"},
            }
        )
        core = self.make_core(database)
        blocked_started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        results = []
        failures = []

        def resolve(host, *_args, **_kwargs):
            if host == "blocked.example":
                blocked_started.set()
                if not release.wait(2):
                    raise AssertionError("test did not release DNS blocker")
                return [_candidate("198.51.100.41")]
            if host == "working.example":
                return [_candidate("198.51.100.40")]
            raise AssertionError(f"unexpected host: {host}")

        def lookup():
            try:
                results.append(
                    core._lookup_countries(
                        (
                            "blocked.example",
                            "working.example",
                            "192.0.2.40",
                        )
                    )
                )
            except BaseException as error:
                failures.append(error)
            finally:
                caller_finished.set()

        with installed_network_pools(), mock.patch.object(
            core_module,
            "_GEOIP_DNS_BATCH_TIMEOUT_SECONDS",
            0.02,
            create=True,
        ), mock.patch.object(
            network_calls.socket,
            "getaddrinfo",
            side_effect=resolve,
        ):
            caller = threading.Thread(target=lookup)
            caller.start()
            try:
                self.assertTrue(blocked_started.wait(1))
                completed_before_release = caller_finished.wait(0.2)
            finally:
                release.set()
                caller.join(1)

        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            results,
            [
                {
                    "blocked.example": "",
                    "working.example": "CA",
                    "192.0.2.40": "US",
                }
            ],
        )
        self.assertEqual(
            database.lookups,
            ["198.51.100.40", "192.0.2.40"],
        )

    def test_unexpected_resolver_error_propagates(self):
        core = self.make_core(_RecordingDatabase({}))
        error = RuntimeError("resolver programming error")
        with mock.patch.object(
            core_module,
            "getaddrinfo_many_until",
            return_value=[CallOutcome.failure(error)],
            create=True,
        ):
            with self.assertRaises(RuntimeError) as raised:
                core._lookup_countries(("server.example",))
        self.assertIs(raised.exception, error)

    def test_exact_mmdb_schemas_and_malformed_records_are_isolated(self):
        class MutableString(str):
            pass

        mutable_code = MutableString("US")
        records = {
            "192.0.2.1": {"country_code": "US"},
            "192.0.2.2": {"country": {"iso_code": "DE"}},
            "192.0.2.3": None,
            "192.0.2.4": ["CA"],
            "192.0.2.5": {"country_code": mutable_code},
            "192.0.2.6": {
                "country_code": None,
                "country": {"iso_code": "NZ"},
            },
            "192.0.2.7": {"country": {"iso_code": ["GB"]}},
        }
        database = _RecordingDatabase(records)
        core = self.make_core(database)

        countries = core._lookup_countries(tuple(records))

        self.assertEqual(
            countries,
            {
                "192.0.2.1": "US",
                "192.0.2.2": "DE",
                "192.0.2.3": "",
                "192.0.2.4": "",
                "192.0.2.5": "",
                "192.0.2.6": "",
                "192.0.2.7": "",
            },
        )
        self.assertEqual(database.lookups, list(records))

    def test_expected_database_errors_are_isolated(self):
        records = {
            "192.0.2.50": ValueError("unsupported address"),
            "192.0.2.51": maxminddb.InvalidDatabaseError("bad database"),
            "192.0.2.52": {"country_code": "US"},
        }
        database = _RecordingDatabase(records)
        core = self.make_core(database)

        countries = core._lookup_countries(tuple(records))

        self.assertEqual(
            countries,
            {"192.0.2.50": "", "192.0.2.51": "", "192.0.2.52": "US"},
        )

    def test_unexpected_database_error_propagates(self):
        error = RuntimeError("database programming error")
        core = self.make_core(_RecordingDatabase({"192.0.2.60": error}))

        with self.assertRaises(RuntimeError) as raised:
            core._lookup_countries(("192.0.2.60",))

        self.assertIs(raised.exception, error)


def _forbidden_stream(reason):
    """Build a body factory that fails the test if the body is ever read."""

    def factory():
        raise AssertionError(reason)

    return factory


def _forbidden_progress(reason):
    """Build a progress callback that fails the test if it is ever called."""

    def callback(_downloaded, _total):
        raise AssertionError(reason)

    return callback


def _recorded_stream(chunks, delivered):
    """Build a body factory that records each chunk as it is pulled."""

    def factory():
        for chunk in chunks:
            delivered.append(chunk)
            yield chunk

    return factory


class _FakeResponse:
    """Stand in for the terminal response the HTTP boundary yields.

    It carries only what the transfer uses: case-insensitive headers, a
    streamed body, and a close another thread may call while that body is
    being read.
    """

    def __init__(self, body=(), *, headers=None):
        self.headers = CaseInsensitiveDict(headers or {})
        self.closed = False
        # A response with no read side to shut down is closed by a detached
        # daemon, so a test that watches for that close needs a barrier.
        self.close_event = threading.Event()
        self.iter_calls = 0
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def close(self):
        self.closed = True
        self.close_event.set()

    def iter_content(self, chunk_size=None):
        self.iter_calls += 1
        return iter(self._body() if callable(self._body) else self._body)


class _FakeOpen:
    """Replace `http_client.open_https_response` and record its arguments."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        # The boundary is entered on the transfer's opening helper, so a
        # test that abandons that helper needs a barrier before it reads
        # what the call recorded.
        self.entered = threading.Event()

    @contextlib.contextmanager
    def __call__(self, url, *, timeout, deadline):
        self.calls.append(
            SimpleNamespace(url=url, timeout=timeout, deadline=deadline)
        )
        self.entered.set()
        # The real boundary yields inside `with response:`, so the response is
        # closed however the caller leaves the block.
        with self.response:
            yield self.response


class DatabaseDownloadTests(unittest.TestCase):
    """Tests for the owned, cancellable, bounded database transfer."""

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.destination = os.path.join(self.directory, "dbip-country.mmdb")
        self.debug_msg = self.enterContext(
            mock.patch.object(helpers, "debug_msg")
        )
        # However the transfer misbehaves, no test may reach a transport.
        for attribute in ("get", "Session"):
            self.enterContext(
                mock.patch.object(
                    geoip.requests,
                    attribute,
                    side_effect=AssertionError(
                        "transfer bypassed the HTTP boundary"
                    ),
                )
            )

    def make_transfer(self, *, seconds=60.0, **options):
        return geoip.GeoIPDatabaseDownload(
            self.destination,
            time.monotonic() + seconds,
            **options,
        )

    def install(self, response):
        """Serve one fake response to every transfer opened by this test."""
        fake = _FakeOpen(response)
        self.enterContext(
            mock.patch.object(http_client, "open_https_response", fake)
        )
        return fake

    def stub_validation(self):
        return self.enterContext(
            mock.patch.object(geoip, "_validate_database")
        )

    def entries(self):
        return sorted(os.listdir(self.directory))

    def modes(self):
        return {
            name: stat.S_IMODE(
                os.stat(os.path.join(self.directory, name)).st_mode
            )
            for name in os.listdir(self.directory)
        }

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def write(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def test_download_allocates_unique_private_same_directory_partial_files(
        self,
    ):
        self.stub_validation()
        coexisting = []
        inner_results = []
        outer = self.make_transfer()
        inner = self.make_transfer()

        def inner_progress(_downloaded, _total):
            coexisting.append(self.modes())

        def outer_progress(_downloaded, _total):
            if inner_results:
                return
            # A second transfer to the same destination runs while this one
            # still holds its own partial file.
            with mock.patch.object(
                http_client,
                "open_https_response",
                _FakeOpen(_FakeResponse((b"inner body",))),
            ):
                inner_results.append(inner.run(inner_progress))

        self.install(_FakeResponse((b"outer body",)))
        outer_result = outer.run(outer_progress)

        [partials] = coexisting
        self.assertEqual(len(partials), 2)
        self.assertEqual(set(partials.values()), {0o600})
        self.assertNotIn(os.path.basename(self.destination), partials)
        self.assertNotIn(
            os.path.basename(self.destination) + ".part",
            partials,
        )
        self.assertEqual(inner_results, [self.destination])
        self.assertEqual(outer_result, self.destination)
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.assertEqual(self.read(self.destination), b"outer body")
        self.debug_msg.assert_not_called()

    def test_declared_oversize_body_is_rejected_before_stream_read(self):
        validate = self.stub_validation()
        response = _FakeResponse(
            _forbidden_stream("declared oversize body was streamed"),
            headers={"Content-Length": "1025"},
        )
        self.install(response)
        transfer = self.make_transfer(max_bytes=1024)

        result = transfer.run(
            _forbidden_progress("declared oversize body reported progress")
        )

        self.assertIsNone(result)
        self.assertEqual(response.iter_calls, 0)
        self.assertTrue(response.closed)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP database exceeds 1024 bytes"),
                ])
            ],
        )

    def test_streamed_oversize_body_is_rejected_before_validation_or_replace(
        self,
    ):
        validate = self.stub_validation()
        self.write(self.destination, b"existing database")
        delivered = []
        progress = []
        # The declared length stays inside the limit; only the body lies.
        response = _FakeResponse(
            _recorded_stream((b"12345", b"6789", b"never read"), delivered),
            headers={"Content-Length": "8"},
        )
        self.install(response)
        transfer = self.make_transfer(max_bytes=8)

        result = transfer.run(
            lambda downloaded, total: progress.append((downloaded, total))
        )

        self.assertIsNone(result)
        self.assertEqual(delivered, [b"12345", b"6789"])
        self.assertEqual(progress, [(5, 8)])
        validate.assert_not_called()
        self.assertTrue(response.closed)
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.assertEqual(self.read(self.destination), b"existing database")
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP database exceeds 8 bytes"),
                ])
            ],
        )

    def test_deadline_is_checked_before_every_write(self):
        validate = self.stub_validation()
        clock = SimpleNamespace(now=1000.0)
        self.enterContext(
            mock.patch.object(
                geoip,
                "time",
                SimpleNamespace(monotonic=lambda: clock.now),
            )
        )
        deadline = 1005.0
        delivered = []
        progress = []

        def endless():
            # Data never stops arriving, so no single read can time out.
            while True:
                clock.now += 1.0
                delivered.append(b"chunk")
                yield b"chunk"

        response = _FakeResponse(endless)
        self.install(response)
        transfer = geoip.GeoIPDatabaseDownload(self.destination, deadline)

        result = transfer.run(
            lambda downloaded, _total: progress.append(downloaded)
        )

        self.assertIsNone(result)
        self.assertEqual(len(delivered), 5)
        self.assertEqual(progress, [5, 10, 15, 20])
        self.assertEqual(clock.now, deadline)
        validate.assert_not_called()
        self.assertTrue(response.closed)
        self.assertEqual(self.entries(), [])
        # However a transfer notices its own deadline, it reports it once.
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP download exceeded its time limit."),
                ])
            ],
        )

    def test_watchdog_ends_a_read_the_absolute_deadline_overtook(self):
        # A unit test of the watchdog itself: this response's read ends only
        # when another thread releases it, so what it proves is that the
        # watchdog fires on time, releases the read from outside it, and
        # reports the deadline once. That the bound holds over the transport
        # which really enforces it is `RealTransportDownloadTests`.
        validate = self.stub_validation()
        clock = SimpleNamespace(now=1000.0)
        self.enterContext(
            mock.patch.object(
                geoip,
                "time",
                SimpleNamespace(monotonic=lambda: clock.now),
            )
        )
        # This clock never moves, so no check the transfer makes for itself
        # can stop it; the watchdog waits real time.
        deadline = clock.now + 0.2
        released = threading.Event()
        closed_by = []

        class _BlockedResponse(_FakeResponse):
            def close(self):
                closed_by.append(threading.current_thread().name)
                released.set()
                super().close()

            def iter_content(self, chunk_size=None):
                self.iter_calls += 1
                if not released.wait(1.0):
                    raise AssertionError("the blocked read was never released")
                raise ValueError("I/O operation on closed file")

        response = _BlockedResponse()
        self.install(response)
        transfer = geoip.GeoIPDatabaseDownload(self.destination, deadline)

        started = time.monotonic()
        result = transfer.run()
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(response.iter_calls, 1)
        self.assertTrue(response.closed)
        # The read was released by a close from another thread; the test's
        # own thread only closes the response afterwards, leaving the block.
        self.assertNotEqual(closed_by[0], threading.current_thread().name)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP download exceeded its time limit."),
                ])
            ],
        )

    def test_a_process_out_of_threads_fails_the_transfer_it_cannot_bound(
        self,
    ):
        # The watchdog and the opening helper are what bound this transfer,
        # so a process that cannot start one ends the transfer as the
        # failure it is, with one fixed reason, rather than as an unexpected
        # exception in a worker nobody reports to.
        start = threading.Thread.start
        refused = SafeDiagnostic(
            "GeoIP download could not start a helper thread."
        )
        for refuse in ("every helper", geoip._OPENER_THREAD_NAME):
            with self.subTest(refuse=refuse):
                self.debug_msg.reset_mock()
                with contextlib.ExitStack() as stack:
                    validate = stack.enter_context(
                        mock.patch.object(geoip, "_validate_database")
                    )
                    stack.enter_context(
                        mock.patch.object(
                            http_client,
                            "open_https_response",
                            _FakeOpen(_FakeResponse((b"body",))),
                        )
                    )
                    transfer = self.make_transfer()

                    def refuse_start(thread, _refuse=refuse):
                        if _refuse in ("every helper", thread.name):
                            raise RuntimeError("can't start new thread")
                        start(thread)

                    stack.enter_context(
                        mock.patch.object(
                            threading.Thread,
                            "start",
                            refuse_start,
                        )
                    )

                    self.assertIsNone(transfer.run())

                validate.assert_not_called()
                self.assertEqual(self.entries(), [])
                self.assertEqual(
                    self.debug_msg.call_args_list,
                    [mock.call([geoip._GEOIP_MSG, refused])],
                )

    def test_read_timeout_is_never_a_negative_wait(self):
        # Construction, the entry check, and the read bound each read the
        # clock once; the deadline passes between the second and the third.
        self.stub_validation()
        readings = iter((1000.0, 1000.0))
        self.enterContext(
            mock.patch.object(
                geoip,
                "time",
                SimpleNamespace(monotonic=lambda: next(readings, 1100.0)),
            )
        )
        fake = self.install(_FakeResponse(()))
        transfer = geoip.GeoIPDatabaseDownload(self.destination, 1005.0)

        self.assertIsNone(transfer.run())

        self.assertTrue(fake.entered.wait(1.0))
        [call] = fake.calls
        self.assertEqual(call.timeout, (5.0, 0.0))

    def test_read_timeout_never_exceeds_fifteen_seconds_or_remaining_deadline(
        self,
    ):
        self.stub_validation()
        clock = SimpleNamespace(now=1000.0)
        self.enterContext(
            mock.patch.object(
                geoip,
                "time",
                SimpleNamespace(monotonic=lambda: clock.now),
            )
        )
        cases = (
            # Ample time left: the fixed inactivity bound applies.
            (60.0, 15.0, 60.0),
            # Less time left than that bound: the remainder applies.
            (3.0, 3.0, 3.0),
            # More time than one operation may take: the limit applies.
            (600.0, 15.0, geoip.DOWNLOAD_TIMEOUT_SECONDS),
        )
        for supplied, expected_read, expected_span in cases:
            with self.subTest(supplied=supplied):
                start = clock.now
                fake = _FakeOpen(_FakeResponse((b"body",)))
                with mock.patch.object(
                    http_client,
                    "open_https_response",
                    fake,
                ):
                    transfer = geoip.GeoIPDatabaseDownload(
                        self.destination,
                        start + supplied,
                    )
                    self.assertEqual(transfer.run(), self.destination)

                [call] = fake.calls
                self.assertEqual(call.url, geoip.DOWNLOAD_URL)
                self.assertEqual(call.timeout, (5.0, expected_read))
                self.assertLessEqual(
                    call.timeout[1],
                    geoip.MAX_READ_TIMEOUT_SECONDS,
                )
                self.assertEqual(call.deadline, start + expected_span)
                self.assertEqual(call.deadline, transfer.deadline)
                clock.now += 1000.0

    def test_cancellation_after_validation_preserves_existing_database(self):
        self.write(self.destination, b"existing database")
        validating = threading.Event()
        cancelled = threading.Event()
        validated = []
        transfer = self.make_transfer()

        def validate(path):
            validated.append(path)
            validating.set()
            if not cancelled.wait(1):
                raise AssertionError("test did not cancel during validation")

        def cancel_during_validation():
            if validating.wait(1):
                transfer.cancel()
            cancelled.set()

        canceller = threading.Thread(
            target=cancel_during_validation,
            name="geoip-transfer-canceller",
        )
        self.enterContext(
            mock.patch.object(geoip, "_validate_database", validate)
        )
        response = _FakeResponse((b"replacement body",))
        self.install(response)

        canceller.start()
        try:
            result = transfer.run()
        finally:
            cancelled.set()
            canceller.join(1)

        self.assertIsNone(result)
        self.assertFalse(canceller.is_alive())
        [validated_path] = validated
        self.assertNotEqual(validated_path, self.destination)
        self.assertEqual(os.path.dirname(validated_path), self.directory)
        self.assertTrue(response.closed)
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.assertEqual(self.read(self.destination), b"existing database")
        self.debug_msg.assert_not_called()

    def test_cancellation_during_replacement_reports_the_installed_path(self):
        self.write(self.destination, b"existing database")
        unrelated = os.path.join(self.directory, "unrelated.mmdb.part")
        self.write(unrelated, b"not ours")
        self.stub_validation()
        transfer = self.make_transfer()
        install = geoip.os.replace

        def replace_after_cancellation(source, destination):
            # Cancellation that lands here arrives once the validated bytes
            # are already being published under the destination name.
            transfer.cancel()
            install(source, destination)

        self.enterContext(
            mock.patch.object(geoip.os, "replace", replace_after_cancellation)
        )
        response = _FakeResponse((b"replacement body",))
        self.install(response)

        result = transfer.run()

        # The replacement succeeded, so the database is installed and its
        # path is reported; loading it is the caller's own decision.
        self.assertEqual(result, self.destination)
        self.assertEqual(self.read(self.destination), b"replacement body")
        self.assertEqual(
            self.entries(),
            sorted(
                [os.path.basename(self.destination), "unrelated.mmdb.part"]
            ),
        )
        self.assertEqual(self.read(unrelated), b"not ours")
        self.assertTrue(response.closed)
        self.debug_msg.assert_not_called()

    def test_cancel_closes_registered_response_and_removes_only_its_partial(
        self,
    ):
        validate = self.stub_validation()
        unrelated = {
            self.destination + ".part": b"another transfer",
            os.path.join(self.directory, "unrelated.mmdb.part"): b"not ours",
        }
        for path, data in unrelated.items():
            self.write(path, data)
        streaming = threading.Event()
        release = threading.Event()
        outcome = []
        transfer = self.make_transfer()

        def blocked_stream():
            yield b"first"
            streaming.set()
            if not release.wait(1):
                raise AssertionError("test did not release the blocked read")
            if response.closed:
                # Reading a connection another thread closed fails; the
                # transfer must see its own cancellation before it decides
                # what that failure was.
                raise requests.exceptions.ChunkedEncodingError("closed")
            yield b"second"

        response = _FakeResponse(blocked_stream)
        self.install(response)

        def run_transfer():
            try:
                outcome.append(transfer.run())
            except BaseException as error:
                outcome.append(error)

        worker = threading.Thread(target=run_transfer, name="geoip-transfer")
        worker.start()
        try:
            self.assertTrue(streaming.wait(1))
            self.assertFalse(response.closed)
            transfer.cancel()
            # This response exposes no read side to shut down, so the close
            # that releases the blocked read is handed to a daemon rather
            # than performed on this thread.
            self.assertTrue(response.close_event.wait(1))
        finally:
            release.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [None])
        validate.assert_not_called()
        self.assertFalse(os.path.exists(self.destination))
        self.assertEqual(
            self.entries(),
            sorted(os.path.basename(path) for path in unrelated),
        )
        for path, data in unrelated.items():
            self.assertEqual(self.read(path), data)
        self.debug_msg.assert_not_called()

    def test_successful_transfer_installs_only_validated_private_bytes(self):
        self.write(self.destination, b"old database")
        validated = []

        def validate(path):
            validated.append((path, self.read(path)))

        self.enterContext(
            mock.patch.object(geoip, "_validate_database", validate)
        )
        progress = []
        response = _FakeResponse(
            (b"abcd", b"efghi"),
            headers={"content-length": "9"},
        )
        self.install(response)
        transfer = self.make_transfer()

        result = transfer.run(
            lambda downloaded, total: progress.append((downloaded, total))
        )

        self.assertEqual(result, self.destination)
        [(validated_path, validated_body)] = validated
        self.assertNotEqual(validated_path, self.destination)
        self.assertEqual(os.path.dirname(validated_path), self.directory)
        self.assertEqual(validated_body, b"abcdefghi")
        self.assertEqual(progress, [(4, 9), (9, 9)])
        self.assertTrue(response.closed)
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.assertEqual(self.read(self.destination), b"abcdefghi")
        self.assertEqual(self.modes(), {os.path.basename(result): 0o600})
        self.debug_msg.assert_not_called()

    def test_invalid_database_body_is_never_installed(self):
        response = _FakeResponse((b"not a maxmind database",))
        self.install(response)
        transfer = self.make_transfer()

        self.assertIsNone(transfer.run())

        self.assertTrue(response.closed)
        self.assertEqual(self.entries(), [])
        # The raw MaxMind failure names the file it opened; the report may
        # carry only fixed text.
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic(
                        "Unexpected InvalidDatabaseError; "
                        "exception details omitted."
                    ),
                ])
            ],
        )


def _header(declared, *extra_lines):
    """Build one 200 response header declaring `declared` body bytes."""
    lines = ["HTTP/1.1 200 OK", f"Content-Length: {declared}", *extra_lines]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class _FakePeer:
    """Stand in for one connected socket, without opening one.

    A peer hands its prepared wire response out one byte per read, so
    `http.client` and `urllib3` perform exactly the byte-granular reads a
    trickling server provokes: one read per header byte, and a body read
    that can return as soon as a single byte has arrived. Once the prepared
    bytes run out the peer stalls — neither closed nor sending — until its
    read side is shut down or its own bounded stall passes, which is what a
    silent connection looks like from above.
    """

    def __init__(self, wire, *, interval=0.0, slow_from=0, stall=1.0):
        self.wire = bytes(wire)
        self.interval = interval
        self.stall = stall
        self.reads = 0
        self.closes = 0
        self.closed = False
        self.shut = threading.Event()
        self.body_started = threading.Event()
        self.file_closed = threading.Event()
        self._slow_from = slow_from
        self._served = 0

    # The part of the socket protocol `http.client` and `urllib3` use.
    def makefile(self, mode="rb", buffering=None):
        return io.BufferedReader(_PeerReader(self))

    def settimeout(self, value):
        """Accept the timeout urllib3 sets; this peer bounds itself."""

    def shutdown(self, how):
        """Shut this peer's read side down, exactly as `SHUT_RD` does."""
        self.shut.set()

    def close(self):
        self.closes += 1
        self.closed = True
        self.shut.set()

    def serve(self) -> bytes:
        """Return one byte, end of file, or block as a stalled peer does."""
        self.reads += 1
        if self.shut.is_set():
            return b""
        if self._served >= len(self.wire):
            self.shut.wait(self.stall)
            return b""
        if self._served >= self._slow_from:
            self.body_started.set()
            if self.interval and self.shut.wait(self.interval):
                return b""
        served = self.wire[self._served:self._served + 1]
        self._served += 1
        return served


class _PeerReader(io.RawIOBase):
    """The connection file `http.client` reads one peer through."""

    def __init__(self, peer):
        super().__init__()
        self._peer = peer

    def readable(self):
        return True

    def close(self):
        self._peer.file_closed.set()
        super().close()

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("read of a closed connection file")
        if self._peer.closed:
            raise OSError("read from a closed socket")
        served = self._peer.serve()
        if not served:
            return 0
        buffer[0] = served[0]
        return 1


class _FakePeerAdapter(requests.adapters.HTTPAdapter):
    """Serve one peer through the transport classes Requests really uses.

    Everything below this adapter is the shipped stack: `http.client` parses
    the status line and the headers, `urllib3` owns the body reads and the
    read-side shutdown, and `requests` wraps both. Only the socket is a
    fake, so no port is ever opened and no timing depends on one.
    """

    def __init__(self, peer):
        super().__init__()
        self._peer = peer

    def send(self, request, **_options):
        peer = self._peer
        parsed = http.client.HTTPResponse(
            peer,
            method=request.method,
            url=request.url,
        )
        # Reading the status line and the headers happens here, inside the
        # send a caller waits for, exactly as it does over a real socket.
        parsed.begin()
        raw = urllib3.HTTPResponse(
            body=parsed,
            headers=urllib3.HTTPHeaderDict(parsed.msg.items()),
            status=parsed.status,
            version=parsed.version,
            reason=parsed.reason,
            preload_content=False,
            decode_content=False,
            original_response=parsed,
            enforce_content_length=True,
            request_method=request.method,
            request_url=request.url,
            connection=peer,
            sock_shutdown=peer.shutdown,
        )
        return self.build_response(request, raw)


class RealTransportDownloadTests(unittest.TestCase):
    """The transfer's bounds, proved against the stack that enforces them.

    `DatabaseDownloadTests` replaces the response; these tests replace only
    the socket, so `requests`, `urllib3` and `http.client` are the shipped
    classes — the ones whose buffered reads and buffer locks decide whether
    a deadline or a cancellation can end a read at all.
    """

    URL = "https://peer.invalid/dbip-country.mmdb"

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.destination = os.path.join(self.directory, "dbip-country.mmdb")
        self.debug_msg = self.enterContext(
            mock.patch.object(helpers, "debug_msg")
        )

    def serve(self, peer):
        """Answer every HTTPS request of this test from one fake peer."""
        session_class = requests.Session

        def open_session():
            session = session_class()
            session.mount("https://", _FakePeerAdapter(peer))
            return session

        self.enterContext(
            mock.patch.object(http_client.requests, "Session", open_session)
        )
        return peer

    def transfer(self, seconds, **options):
        return geoip.GeoIPDatabaseDownload(
            self.destination,
            time.monotonic() + seconds,
            url=self.URL,
            **options,
        )

    def stub_validation(self):
        return self.enterContext(
            mock.patch.object(geoip, "_validate_database")
        )

    def entries(self):
        return sorted(os.listdir(self.directory))

    def assert_reported_the_deadline(self):
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic("GeoIP download exceeded its time limit."),
                ])
            ],
        )

    def test_trickling_body_cannot_outlast_the_absolute_deadline(self):
        # One byte every 10 ms satisfies every read wait without ever
        # completing a 64 KiB buffered chunk, so a transfer that reads whole
        # chunks reaches no check of its own and ends only when this peer
        # stops — three seconds past a deadline of four tenths of one.
        validate = self.stub_validation()
        header = _header(100000)
        peer = self.serve(
            _FakePeer(
                header + b"x" * 200,
                interval=0.01,
                slow_from=len(header),
            )
        )
        transfer = self.transfer(0.4)

        started = time.monotonic()
        result = transfer.run()
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertTrue(peer.body_started.is_set())
        self.assertLess(elapsed, 0.9)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assert_reported_the_deadline()

    def test_cancel_returns_without_waiting_for_the_blocked_reader(self):
        # `Response.close()` closes the buffered reader first, and CPython's
        # buffered close takes the very lock the worker's read holds, so a
        # cancellation that closes would wait out that read on the caller's
        # own thread — here the thread pressing "Not now".
        validate = self.stub_validation()
        header = _header(100000)
        peer = self.serve(
            _FakePeer(
                header + b"x" * 40,
                interval=0.05,
                slow_from=len(header),
            )
        )
        transfer = self.transfer(30.0)
        outcome = []

        worker = threading.Thread(
            target=lambda: outcome.append(transfer.run()),
            name="geoip-transfer",
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(peer.body_started.wait(1.0))
            started = time.monotonic()
            transfer.cancel()
            returned_in = time.monotonic() - started
            worker.join(1.0)
            stopped_in = time.monotonic() - started
        finally:
            peer.shut.set()
            worker.join(1.0)

        self.assertLess(returned_in, 0.1)
        self.assertFalse(worker.is_alive())
        self.assertLess(stopped_in, 0.5)
        self.assertEqual(outcome, [None])
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.debug_msg.assert_not_called()

    def test_trickling_header_phase_cannot_outlast_the_deadline(self):
        # A peer that trickles its status line holds whichever thread
        # entered the HTTP boundary, and there is no response to interrupt
        # yet, so only a worker that can abandon that thread is bounded.
        validate = self.stub_validation()
        peer = self.serve(_FakePeer(_header(4) + b"body", interval=0.05))
        transfer = self.transfer(0.3)

        started = time.monotonic()
        result = transfer.run()
        elapsed = time.monotonic() - started
        # The abandoned helper finishes the hop it began; it must close what
        # it receives and install nothing.
        peer.interval = 0.0

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.9)
        self.assertTrue(peer.file_closed.wait(1.0))
        self.assertGreaterEqual(peer.closes, 1)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assert_reported_the_deadline()

    def test_byte_granular_reads_reassemble_one_decoded_body(self):
        validated = []
        self.enterContext(
            mock.patch.object(geoip, "_validate_database", validated.append)
        )
        body = b"mmdb-bytes-" * 50
        compressed = gzip.compress(body)
        self.serve(
            _FakePeer(
                _header(len(compressed), "Content-Encoding: gzip")
                + compressed
            )
        )
        transfer = self.transfer(5.0)
        progress = []

        result = transfer.run(
            lambda downloaded, total: progress.append((downloaded, total))
        )

        self.assertEqual(result, self.destination)
        [validated_path] = validated
        self.assertNotEqual(validated_path, self.destination)
        with open(self.destination, "rb") as handle:
            self.assertEqual(handle.read(), body)
        self.assertEqual(progress[-1], (len(body), len(compressed)))
        self.assertEqual(self.entries(), [os.path.basename(self.destination)])
        self.debug_msg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
