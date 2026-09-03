import contextlib
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
from requests.structures import CaseInsensitiveDict

from obozrenie import (
    geoip,
    helpers,
    http_client,
    network_calls,
)
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


class GeolocationTests(unittest.TestCase):
    def make_geolocation(self, database):
        """Build a Geolocation over one test database (None = disabled)."""
        return geoip.Geolocation(reader=database)

    def test_disabled_geolocation_returns_unknown_without_dns(self):
        geolocation = self.make_geolocation(None)
        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            side_effect=AssertionError("disabled geolocation used DNS"),
        ):
            countries = geolocation.countries_for(
                ("server.example", "192.0.2.1", "server.example")
            )

        self.assertEqual(
            countries,
            {"server.example": "unknown", "192.0.2.1": "unknown"},
        )
        self.assertEqual(
            geolocation.countries_for(("server.example",))["server.example"],
            "unknown",
        )

    def test_lookup_separates_literals_and_one_first_seen_hostname_batch(self):
        database = _RecordingDatabase(
            {
                "192.0.2.10": {"country_code": "US"},
                "2001:db8::10": {"country": {"iso_code": "DE"}},
                "198.51.100.1": {"country_code": "CA"},
            }
        )
        geolocation = self.make_geolocation(database)
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
            time.monotonic() + geoip._DNS_BATCH_TIMEOUT_SECONDS
        )
        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            side_effect=resolve_many,
        ):
            countries = geolocation.countries_for(hosts)
        after_deadline = (
            time.monotonic() + geoip._DNS_BATCH_TIMEOUT_SECONDS
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
        geolocation = self.make_geolocation(_RecordingDatabase({}))
        deadline = time.monotonic() + 123.0
        calls = []

        def resolve_many(hosts, supplied_deadline):
            calls.append((tuple(hosts), supplied_deadline))
            return [CallOutcome.failure(socket.gaierror("missing"))]

        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            side_effect=resolve_many,
        ):
            countries = geolocation.countries_for(
                ("server.example",), deadline
            )

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
        geolocation = self.make_geolocation(database)
        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            side_effect=AssertionError("IP literals used DNS"),
        ):
            countries = geolocation.countries_for(tuple(database.outcomes))

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
        geolocation = self.make_geolocation(database)
        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            return_value=[CallOutcome.success(candidates)],
        ):
            country = geolocation.countries_for(
                ("server.example",)
            )["server.example"]

        self.assertEqual(country, "NZ")
        self.assertEqual(database.lookups, ["198.51.100.8"])

    def test_expected_dns_outcomes_blank_only_affected_names(self):
        database = _RecordingDatabase(
            {"198.51.100.20": {"country_code": "CA"}}
        )
        geolocation = self.make_geolocation(database)
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
            geoip,
            "getaddrinfo_many_until",
            return_value=outcomes,
        ):
            countries = geolocation.countries_for(names)

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
                geolocation = self.make_geolocation(_RecordingDatabase({}))
                with mock.patch.object(
                    geoip,
                    "getaddrinfo_many_until",
                    return_value=[CallOutcome.failure(outcome_error)],
                ):
                    raised = _capture_error(
                        lambda: geolocation.countries_for(("server.example",))
                    )

                self.assertIs(raised, outcome_error)

    def test_closed_dns_pool_blanks_name_but_not_literal(self):
        database = _RecordingDatabase(
            {"192.0.2.30": {"country_code": "US"}}
        )
        geolocation = self.make_geolocation(database)
        with installed_network_pools() as pools:
            pools.dns.begin_close()
            countries = geolocation.countries_for(
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
        geolocation = self.make_geolocation(database)
        initial_error = _StartupAbort("thread startup interrupted")
        with installed_network_pools() as pools, mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ) as thread_factory:
            first = geolocation.countries_for(
                ("first.example", "192.0.2.31"),
                time.monotonic() + 0.02,
            )
            second = geolocation.countries_for(
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
        geolocation = self.make_geolocation(database)
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
                    countries = geolocation.countries_for(
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
        geolocation = self.make_geolocation(database)
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
                    geolocation.countries_for(
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
            geoip,
            "_DNS_BATCH_TIMEOUT_SECONDS",
            0.02,
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
        geolocation = self.make_geolocation(_RecordingDatabase({}))
        error = RuntimeError("resolver programming error")
        with mock.patch.object(
            geoip,
            "getaddrinfo_many_until",
            return_value=[CallOutcome.failure(error)],
        ):
            with self.assertRaises(RuntimeError) as raised:
                geolocation.countries_for(("server.example",))
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
        geolocation = self.make_geolocation(database)

        countries = geolocation.countries_for(tuple(records))

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
        geolocation = self.make_geolocation(database)

        countries = geolocation.countries_for(tuple(records))

        self.assertEqual(
            countries,
            {"192.0.2.50": "", "192.0.2.51": "", "192.0.2.52": "US"},
        )

    def test_unexpected_database_error_propagates(self):
        error = RuntimeError("database programming error")
        geolocation = self.make_geolocation(
            _RecordingDatabase({"192.0.2.60": error})
        )

        with self.assertRaises(RuntimeError) as raised:
            geolocation.countries_for(("192.0.2.60",))

        self.assertIs(raised.exception, error)

    def test_load_enables_and_a_failed_load_disables(self):
        opened = object()
        with (
            mock.patch.object(geoip.maxminddb, "open_database", return_value=opened),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            geolocation = geoip.Geolocation()
            self.assertFalse(geolocation.enabled)
            geolocation.load("/db/country.mmdb")
        self.assertTrue(geolocation.enabled)
        self.assertEqual(debug_msg.call_count, 1)

        with (
            mock.patch.object(
                geoip.maxminddb, "open_database",
                side_effect=geoip.maxminddb.InvalidDatabaseError("bad"),
            ),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            geolocation.load("/db/country.mmdb")
        self.assertFalse(geolocation.enabled)
        self.assertEqual(debug_msg.call_count, 1)

    def test_discover_loads_the_found_database_or_stays_disabled(self):
        with (
            mock.patch.object(geoip, "find_database", return_value=None),
            mock.patch.object(helpers, "debug_msg") as debug_msg,
        ):
            self.assertFalse(geoip.Geolocation.discover().enabled)
        self.assertEqual(debug_msg.call_count, 1)

        with (
            mock.patch.object(geoip, "find_database", return_value="/db/x.mmdb"),
            mock.patch.object(geoip.maxminddb, "open_database", return_value=object()),
            mock.patch.object(helpers, "debug_msg"),
        ):
            self.assertTrue(geoip.Geolocation.discover().enabled)


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
        # which really enforces it is `RealTransportDownloadTests` in
        # `tests/test_transport.py`.
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

    def test_header_wait_that_runs_out_reports_the_deadline_once(self):
        # The wait for a response can return a few microseconds before the
        # clock reaches the deadline, so neither the watchdog nor the
        # transfer's own check has recorded expiry yet. A wait that runs out
        # is the deadline all the same, and it is reported exactly once.
        validate = self.stub_validation()
        clock = SimpleNamespace(now=1000.0)
        self.enterContext(
            mock.patch.object(
                geoip,
                "time",
                SimpleNamespace(monotonic=lambda: clock.now),
            )
        )
        self.enterContext(
            mock.patch.object(geoip._ResponseOpener, "start", lambda self: None)
        )
        self.enterContext(
            mock.patch.object(
                geoip._ResponseOpener, "claim", lambda self, timeout: None
            )
        )
        transfer = geoip.GeoIPDatabaseDownload(self.destination, clock.now + 0.5)

        self.assertIsNone(transfer.run())

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

    def test_cancellation_before_the_open_arms_and_starts_nothing(self):
        # A cancellation that lands after `run()`'s first check but before
        # the deadline is armed is seen in `_begin`: no watchdog, no opener
        # thread, no request, and nothing to report.
        validate = self.stub_validation()
        opened = self.install(
            _FakeResponse(_forbidden_stream("a cancelled transfer read a body"))
        )
        transfer = self.make_transfer()
        real_opener = geoip._ResponseOpener

        class CancellingOpener(real_opener):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                transfer.cancel()

            def start(self):
                raise AssertionError("a cancelled transfer started its opener")

        with (
            mock.patch.object(geoip, "_ResponseOpener", CancellingOpener),
            mock.patch.object(
                geoip.threading,
                "Timer",
                side_effect=AssertionError(
                    "a cancelled transfer armed its watchdog"
                ),
            ) as timer,
        ):
            self.assertIsNone(transfer.run())

        timer.assert_not_called()
        self.assertEqual(opened.calls, [])
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.debug_msg.assert_not_called()

    def test_a_descriptor_that_will_not_close_cannot_escape_the_worker(self):
        # `os.fdopen` failing leaves the transfer holding the raw descriptor;
        # if closing that descriptor fails as well, the worker still returns
        # None over one reported reason instead of dying on the close.
        validate = self.stub_validation()
        self.install(_FakeResponse((b"body",)))
        transfer = self.make_transfer()
        open_failure = OSError(24, "Too many open files")
        close_failure = OSError(5, "Input/output error")

        with (
            mock.patch.object(geoip.os, "fdopen", side_effect=open_failure),
            mock.patch.object(
                geoip.os, "close", side_effect=close_failure
            ) as close,
        ):
            result = transfer.run()
        # The close that failed above never released the descriptor.
        close.assert_called_once()
        os.close(close.call_args.args[0])

        self.assertIsNone(result)
        validate.assert_not_called()
        self.assertEqual(self.entries(), [])
        self.assertEqual(
            self.debug_msg.call_args_list,
            [
                mock.call([
                    geoip._GEOIP_MSG,
                    SafeDiagnostic(
                        "Unexpected OSError; exception details omitted."
                    ),
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
        for refuse in (
            "every helper",
            geoip._WATCHDOG_THREAD_NAME,
            geoip._OPENER_THREAD_NAME,
        ):
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


if __name__ == "__main__":
    unittest.main()
