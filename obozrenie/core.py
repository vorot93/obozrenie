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

"""Core functions for Obozrenie Game Server Browser."""

import contextlib
import ipaddress
import os
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from enum import Enum, auto

import filelock
import maxminddb

from obozrenie.global_settings import *
from obozrenie.global_strings import *
from obozrenie.option_lists import *

from obozrenie import adapters, geoip, helpers, i18n, launch, ping, resources
from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
)
from obozrenie.game_table import GameTable, validated_server_batch
from obozrenie.models import Endpoint, Server
from obozrenie.network_calls import (
    CallDeadlineExceeded,
    PoolSaturatedError,
    PoolUnavailableError,
    getaddrinfo_many_until,
    validate_deadline,
)


_GEOIP_DNS_BATCH_TIMEOUT_SECONDS = 5.0
_PROFILE_LOCK_TIMEOUT_SECONDS = 5.0


def _query_failure_message() -> str:
    """Render the one message every failed query publishes."""
    return i18n._("Unable to refresh the server list.")


def _log_safe_diagnostic(error: BaseException) -> None:
    """Report one Core failure as fixed safe text; never raises."""
    # Diagnostics are best effort and cannot change terminal query state.
    helpers.log_safe_diagnostic(CORE_MSG, error)


def _log_cleanup_diagnostic(error: BaseException) -> None:
    """Report a cleanup failure that must never reach a primary exception."""
    try:
        _log_safe_diagnostic(error)
    except BaseException:
        # A primary exception is re-raised right after this call and has to
        # keep its identity, so nothing at all may escape a failing report.
        pass


class _AttemptState(Enum):
    """Who may still complete one admitted query."""

    STARTING = auto()
    WORKER = auto()
    COMPLETE = auto()


class _QueryAttempt:
    """One admitted query, completed and reported exactly once.

    The caller that admitted the query and the worker thread it launches race
    for ownership through one lock-protected handoff. The worker claims
    `STARTING -> WORKER` before it may report an outcome, and whichever side
    reaches `COMPLETE` first performs the only token-aware GameTable
    transition and invokes the only callback. Both happen after the attempt
    lock is released, so no callback ever runs while a lock is held.

    Core close overrides that owner: once it has begun, a completion that the
    close could no longer cancel still publishes nothing and calls nobody.

    Every `complete_*` and `cancel_for_close` call reports whether it took
    that single completion, never whether it published: a winner that finds
    Core closing publishes nothing and calls nobody, and its caller only
    needs to know that no other owner will report the query.

    A publish ignores the ownership `GameTable` reports back: the game minted
    this token for this admission alone, the attempt performs exactly one
    transition, and a close that consumed the token had to win the completion
    first, so the attempt and the table cannot disagree.
    """

    def __init__(self, core: "Core", game: str, token: object, callback=None):
        self._core = core
        self.game = game
        self.token = token
        # Written under the attempt lock before the worker starts; read by
        # Core to release this attempt's thread registration.
        self.thread = None
        self._callback = callback
        self._lock = threading.Lock()
        self._state = _AttemptState.STARTING

    def bind_thread(self, thread: threading.Thread) -> None:
        """Record the worker thread that will run this attempt."""
        with self._lock:
            self.thread = thread

    def claim_worker(self) -> bool:
        """Take worker ownership; False once the attempt is terminal."""
        with self._lock:
            if self._state is not _AttemptState.STARTING:
                return False
            self._state = _AttemptState.WORKER
            return True

    def complete_start_failure(self, error: BaseException) -> bool:
        """Complete a query whose worker never claimed it; False once it did."""
        with self._lock:
            if self._state is not _AttemptState.STARTING:
                return False
            self._state = _AttemptState.COMPLETE
        self._report_failure(error)
        return True

    def complete_success(self, servers) -> bool:
        """Complete this attempt with its servers; False if already complete."""
        server_batch = validated_server_batch(servers)
        if not self._take_completion():
            return False
        published = self._core._commit_unless_closing(
            lambda: self._publish_success(server_batch)
        )
        if published:
            self._invoke_callback()
        return True

    def complete_failure(self, error: BaseException) -> bool:
        """Complete this attempt with its failure; False if already complete."""
        if not self._take_completion():
            return False
        self._report_failure(error)
        return True

    def cancel_for_close(self) -> bool:
        """Terminalize this attempt for shutdown; False if already complete."""
        if not self._take_completion():
            return False
        # Close publishes its own terminal state and suppresses the callback:
        # no UI work may follow it.
        self._publish_failure(_query_failure_message())
        return True

    def run_worker(self) -> None:
        """Worker thread entry point for one admitted query."""
        try:
            self._run_query()
        finally:
            self._core._untrack_worker(self)

    def _run_query(self) -> None:
        if not self.claim_worker():
            return
        try:
            self.complete_success(self._core._query_master_servers(self.game))
        except Exception as error:
            if not self.complete_failure(error):
                # Already terminal: a close cancelled the attempt, or the
                # callback itself failed. Report it without losing it.
                _log_safe_diagnostic(error)
        except BaseException as error:
            try:
                self.complete_failure(error)
            except BaseException as cleanup_error:
                # A failing completion is reported but never replaces the
                # worker's own failure, which keeps its identity through the
                # re-raise below.
                _log_cleanup_diagnostic(cleanup_error)
            raise

    def _take_completion(self) -> bool:
        with self._lock:
            if self._state is _AttemptState.COMPLETE:
                return False
            self._state = _AttemptState.COMPLETE
            return True

    def _report_failure(self, error: BaseException) -> None:
        # Rendering the message is this thread's work, not the lifecycle
        # lock's: the commit below holds that lock for one transition only.
        message = _query_failure_message()
        published = self._core._commit_unless_closing(
            lambda: self._publish_failure(message)
        )
        # Close may own the outcome, but the failure is still worth reporting.
        _log_safe_diagnostic(error)
        if published:
            self._invoke_callback()

    def _publish_success(self, server_batch) -> None:
        self._core.game_table.finish_query(self.game, self.token, server_batch)

    def _publish_failure(self, message: str) -> None:
        self._core.game_table.fail_query(self.game, self.token, message)

    def _invoke_callback(self) -> None:
        """Invoke this attempt's single callback, reporting its failure."""
        if self._callback is None:
            return
        try:
            self._callback(self.game)
        except Exception as error:
            # The published outcome stands; a failing callback only reports.
            _log_safe_diagnostic(error)


class Core:
    """
    Contains core logic of Obozrenie server browser.

    Core owns every query it admits. The lifecycle lock guards the closing
    flag, the attempts that may still complete, and the worker threads that
    may still be running, so close can reject new admissions, terminalize what
    is in flight, and join what it launched.
    """

    def __init__(self):
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._active_attempts = set()
        self._tracked_threads = set()

        self.game_table = GameTable(resources.load_toml(
            "settings", "internal", "game_lists.toml"
        ))

        self.geolocation = None
        path = geoip.find_database()
        if path is not None:
            self.load_geoip(path)
        else:
            helpers.debug_msg(
                [CORE_MSG, i18n._("GeoIP database not found. Disabling geolocation.")])

    def load_geoip(self, path: str) -> None:
        """Open a MaxMind DB country database and enable geolocation."""
        try:
            self.geolocation = maxminddb.open_database(path)
            helpers.debug_msg([CORE_MSG, i18n._("GeoIP database %(path)s opened successfully.") % {
                              'path': path}])
        except (OSError, maxminddb.InvalidDatabaseError):
            helpers.debug_msg(
                [CORE_MSG, i18n._("Failed to open GeoIP database. Disabling geolocation.")])
            self.geolocation = None

    def _lookup_countries(
        self,
        hosts,
        deadline: float | None = None,
    ) -> dict[str, str]:
        """Resolve one host batch to ISO country codes."""
        unique_hosts = tuple(dict.fromkeys(hosts))
        if self.geolocation is None:
            return {host: "unknown" for host in unique_hosts}

        addresses = {}
        hostnames = []
        for host in unique_hosts:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                hostnames.append(host)
            else:
                addresses[host] = host

        if hostnames:
            if deadline is None:
                deadline = (
                    time.monotonic() + _GEOIP_DNS_BATCH_TIMEOUT_SECONDS
                )
            outcomes = getaddrinfo_many_until(hostnames, deadline)
            for host, outcome in zip(hostnames, outcomes, strict=True):
                try:
                    candidates = outcome.result()
                except (
                    CallDeadlineExceeded,
                    PoolSaturatedError,
                    PoolUnavailableError,
                ) as error:
                    if type(error) not in (
                        CallDeadlineExceeded,
                        PoolSaturatedError,
                        PoolUnavailableError,
                    ):
                        raise
                    continue
                except (socket.gaierror, UnicodeError):
                    continue

                for candidate in candidates:
                    try:
                        family = candidate[0]
                        address = candidate[4][0]
                    except (IndexError, TypeError):
                        continue
                    if family not in (socket.AF_INET, socket.AF_INET6):
                        continue
                    if type(address) is not str:
                        continue
                    try:
                        parsed = ipaddress.ip_address(address)
                    except ValueError:
                        continue
                    expected_version = 4 if family == socket.AF_INET else 6
                    if parsed.version == expected_version:
                        addresses[host] = address
                        break

        countries = {}
        for host in unique_hosts:
            address = addresses.get(host)
            if address is None:
                countries[host] = ""
                continue
            try:
                record = self.geolocation.get(address)
            except (ValueError, maxminddb.InvalidDatabaseError):
                countries[host] = ""
                continue
            if not isinstance(record, Mapping):
                countries[host] = ""
                continue

            # Flat ip-location-db format vs. nested MaxMind/DB-IP format.
            missing = object()
            code = record.get("country_code", missing)
            if code is not missing:
                countries[host] = code if type(code) is str else ""
                continue
            country = record.get("country")
            if not isinstance(country, Mapping):
                countries[host] = ""
                continue
            code = country.get("iso_code")
            countries[host] = code if type(code) is str else ""
        return countries

    def _lookup_country(self, host: str) -> str:
        return self._lookup_countries((host,))[host]

    def _enrich_servers(self, servers: list[Server]) -> list[Server]:
        snapshot = list(servers)
        try:
            rtt_servers = list(ping.add_rtt_info(list(snapshot)))
        except Exception as error:
            # RTT is optional; retain adapter-provided values if the service fails.
            _log_safe_diagnostic(error)
            rtt_servers = list(snapshot)

        if any(type(server) is not Server for server in rtt_servers):
            raise TypeError("RTT enrichment must return exact Server records.")
        countries = self._lookup_countries(
            server.endpoint.host for server in rtt_servers
        )
        return [
            replace(
                server,
                country=countries[server.endpoint.host],
            )
            for server in rtt_servers
        ]

    def _query_master_servers(self, game: str) -> list[Server]:
        """Query one game's adapter and return enriched server records."""
        game_info = self.game_table.get_game_info(game)
        settings = self.game_table.get_game_settings(game)
        adapter = adapters.resolve_adapter(game_info["adapter"])
        servers = list(
            adapter.stat_master(game, game_info, list(settings["master_uri"]))
        )
        if any(type(server) is not Server for server in servers):
            raise TypeError("Adapters must return exact Server records.")
        return self._enrich_servers(servers)

    def update_server_list(self, game: str, stat_callback=None) -> bool:
        """Admit one query for a game and hand it to a worker thread."""
        attempt = self._admit_query(game, stat_callback)
        if attempt is None:
            return False
        try:
            self._launch_worker(attempt)
        except Exception as error:
            self._abandon_launch(attempt, error)
        except BaseException as error:
            try:
                self._abandon_launch(attempt, error)
            except BaseException as cleanup_error:
                # Cleanup is reported but never replaces the caller's own
                # interruption, which keeps its identity through the re-raise.
                _log_cleanup_diagnostic(cleanup_error)
            raise
        return True

    def _admit_query(self, game: str, stat_callback) -> _QueryAttempt | None:
        """Admit one query, or report that Core is closing or already busy."""
        with self._lifecycle_lock:
            if self._closing:
                return None
            token = self.game_table.begin_query(game)
            if token is None:
                return None
            # Registering before the worker exists keeps close from missing an
            # attempt that is only part way into its launch.
            attempt = _QueryAttempt(self, game, token, stat_callback)
            self._active_attempts.add(attempt)
            return attempt

    def _launch_worker(self, attempt: _QueryAttempt) -> None:
        """Construct, register and start one attempt's worker thread."""
        thread = threading.Thread(target=attempt.run_worker, daemon=True)
        attempt.bind_thread(thread)
        with self._lifecycle_lock:
            self._tracked_threads.add(thread)
        thread.start()

    def _abandon_launch(
        self,
        attempt: _QueryAttempt,
        error: BaseException,
    ) -> None:
        """Clean up a failed launch for whoever now owns the query.

        Ownership is decided before any registration is released: a launch
        that failed while its worker was already claiming the attempt cannot
        prove the thread never ran, and an unpublished identity is no proof
        either, because the new thread publishes its own.
        """
        if attempt.complete_start_failure(error):
            # The caller took the attempt's single completion, so no worker
            # will ever report it and nothing else will untrack it. Whether
            # that completion also published is Core close's business, not
            # this bookkeeping's.
            self._untrack_unstarted_thread(attempt)
            self._untrack_attempt(attempt)
            return
        # A worker that already claimed the attempt owns its completion and
        # its registrations, so a post-claim startup failure is only a safe
        # diagnostic.
        _log_safe_diagnostic(error)

    def _untrack_attempt(self, attempt: _QueryAttempt) -> None:
        """Stop tracking one attempt that can no longer complete."""
        with self._lifecycle_lock:
            self._active_attempts.discard(attempt)

    def _untrack_worker(self, attempt: _QueryAttempt) -> None:
        """Release one exited worker's attempt and thread registrations."""
        with self._lifecycle_lock:
            self._active_attempts.discard(attempt)
            self._tracked_threads.discard(attempt.thread)

    def _untrack_unstarted_thread(self, attempt: _QueryAttempt) -> None:
        """Release a worker thread that provably never launched.

        A thread that failed to launch has no identity and no target that
        could release it. A thread that failed its caller after launching has
        one, and stays tracked until its target exits or a join observes it.
        """
        thread = attempt.thread
        if thread is None or thread.ident is not None:
            return
        with self._lifecycle_lock:
            self._tracked_threads.discard(thread)

    def _commit_unless_closing(self, transition) -> bool:
        """Run one query transition unless close has already begun.

        The closing check and the `GameTable` transition share the lifecycle
        lock, so a commit either lands entirely before close began or never
        lands at all; `begin_close` cannot return while one is in flight.
        The caller learns here whether it may call back, and then does so
        outside this critical section: no callback runs under a lock.
        """
        with self._lifecycle_lock:
            if self._closing:
                return False
            transition()
            return True

    @property
    def is_closing(self) -> bool:
        """Report whether close has begun; no query may commit after it."""
        with self._lifecycle_lock:
            return self._closing

    def begin_close(self) -> None:
        """Reject new queries and terminalize every attempt still in flight."""
        with self._lifecycle_lock:
            self._closing = True
            attempts = tuple(self._active_attempts)
        # Cancellation publishes a terminal state and takes the GameTable
        # lock, so it happens outside this Core's own lock. One attempt that
        # fails to cancel must not strand the rest, and an attempt is
        # forgotten only once its cancellation has returned, so a close that
        # an interruption cut short leaves the remainder for the next one.
        for attempt in attempts:
            try:
                attempt.cancel_for_close()
            except Exception as error:
                _log_safe_diagnostic(error)
            self._untrack_attempt(attempt)

    def join_until(self, deadline: float) -> None:
        """Join tracked worker threads against one absolute deadline."""
        deadline = validate_deadline(deadline)
        with self._lifecycle_lock:
            threads = tuple(self._tracked_threads)

        exited = []
        for thread in threads:
            if thread.ident is None:
                # Registered but not launched yet: it cannot be joined, and
                # only its admitting caller can prove it never will be.
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(remaining)
            if not thread.is_alive():
                exited.append(thread)

        if exited:
            with self._lifecycle_lock:
                self._tracked_threads.difference_update(exited)

    def close_until(self, deadline: float) -> None:
        """Close Core and join its workers against one absolute deadline."""
        # Validate first: a malformed deadline must not half-close Core.
        deadline = validate_deadline(deadline)
        self.begin_close()
        self.join_until(deadline)

    def start_game(
        self,
        game: str,
        server: str,
        password: str,
        callback=None,
    ) -> bool:
        """Validate and start a game in a background worker.

        Game metadata, settings, endpoint, activation directory and the
        complete launch specification are all captured here, on the calling
        thread. The worker only spawns what this snapshot already describes,
        so it never has to read a process-wide value such as the working
        directory that the caller alone can observe reliably.
        """
        if game in ('', None):
            raise ValueError(i18n._('Please specify a valid game id.'))

        game_info = self.game_table.get_game_info(game)
        game_settings = self.game_table.get_game_settings(game)
        launch_pattern = game_info["launch_pattern"]
        steam_app_id = None
        if game_settings.get("steam_launch") is True:
            steam_app_id = game_info.get("steam_app_id")
            if steam_app_id is not None:
                launch_pattern = "steam"
        endpoint = Endpoint.parse(server)
        # `build_launch_spec` re-checks the same setting by game id as an
        # invariant guard; checking it here first, through the same
        # pattern-to-setting accessor, only changes the message to name the
        # game the user recognizes.
        executable_setting = launch.executable_setting_name(launch_pattern)
        if not str(game_settings.get(executable_setting, "")).strip():
            raise ValueError(
                i18n._("Please configure the executable path for %(game)s.")
                % {"game": game_info["name"]}
            )
        spec = launch.build_launch_spec(
            game,
            launch_pattern,
            game_settings,
            endpoint,
            password,
            steam_app_id,
            activation_directory=os.getcwd(),
        )

        def launch_target():
            error = None
            try:
                error = launch.do_launch(spec)
            except Exception as launch_error:
                error = launch_error
            finally:
                if callback is not None:
                    callback(error)

        launch_process = threading.Thread(target=launch_target, daemon=True)
        launch_process.start()
        return True


class SettingsLockError(SafeDiagnosticError):
    def __init__(self) -> None:
        super().__init__(SafeDiagnostic(
            "Another process holds the settings profile lock."
        ))


class Settings:

    """Settings main class.
    Contains methods for saving and loading user settings and setting lists.

    One bounded cross-process lock per profile serializes the user-table reads
    in `load` against the atomic writes in `save`, and is always released
    before a caller's callbacks run.
    """

    def __init__(
        self,
        core: Core,
        profile_path: str,
        resource_loader=resources.load_toml,
    ):
        """Loads base variables into the class."""
        self._resource_loader = resource_loader

        # User configs
        self.user_common_settings_path = os.path.join(
            profile_path, COMMON_SETTINGS_FILE)
        self.user_game_settings_path = os.path.join(
            profile_path, GAME_SETTINGS_FILE)
        self._profile_lock = filelock.FileLock(
            f"{profile_path}.lock",
            timeout=_PROFILE_LOCK_TIMEOUT_SECONDS,
            mode=0o600,
        )

        self.dynamic_widget_table = get_game_options()
        self.common_settings_table = get_common_options()

        self.settings_table = {}

        self.core = core

    @contextlib.contextmanager
    def _hold_profile_lock(self):
        """Hold the profile lock for the duration of one settings operation.

        Acquisition is bounded, so contention becomes the payload-free
        `SettingsLockError` instead of an unbounded wait. The `filelock`
        timeout is suppressed rather than chained: its own message names the
        lock file, which an uncaught traceback would otherwise print.
        """
        try:
            self._profile_lock.acquire()
        except filelock.Timeout:
            raise SettingsLockError from None
        try:
            yield
        finally:
            self._profile_lock.release()

    def load(self, callback_postgenload=None) -> list[str]:
        """Loads configuration."""
        default_common_settings_table = self._resource_loader(
            "settings", "defaults", "settings.toml"
        )
        default_game_settings_table = self._resource_loader(
            "settings", "defaults", "games.toml"
        )

        # One acquisition covers both reads, so a concurrent save cannot land
        # between them; it is released well before the callbacks below run.
        with self._hold_profile_lock():
            user_common_settings_table, common_warning = helpers.load_user_table(
                self.user_common_settings_path)
            user_game_settings_table, game_warning = helpers.load_user_table(
                self.user_game_settings_path)

        warnings = [
            warning
            for warning in (common_warning, game_warning)
            if warning is not None
        ]

        for group in self.common_settings_table:
            self.settings_table[group] = {}  # type: dict
            for option in self.common_settings_table[group]:
                value = default_common_settings_table[group][option]
                try:
                    value = user_common_settings_table[group][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.settings_table[group][option] = value

        for game in self.core.game_table.get_game_set():
            for option in self.core.game_table.get_game_settings(game):
                value = default_game_settings_table[game][option]
                try:
                    value = user_game_settings_table[game][option]
                except (ValueError, KeyError, TypeError):
                    pass

                self.core.game_table.set_game_setting(game, option, value)

        self.normalize_game_selection()

        if callback_postgenload is not None:
            for group in self.common_settings_table:
                for option in self.common_settings_table[group]:
                    callback_postgenload(
                        self.common_settings_table,
                        group,
                        option,
                        self.settings_table[group][option],
                    )

        return warnings

    def normalize_game_selection(self) -> None:
        """Replace invalid selectors with the first configured game."""
        games = self.core.game_table.get_game_set()
        first_game = ""
        if games:
            first_game = min(
                games,
                key=lambda game_id: (
                    self.core.game_table.get_game_info(game_id)["name"].casefold(),
                    game_id,
                ),
            )

        common_settings = self.settings_table["common"]
        for option in ("selected-game-browser", "selected-game-connect"):
            if common_settings.get(option) not in games:
                common_settings[option] = first_game

    def save(self) -> None:
        """Saves configuration."""
        persistent_common_settings = {
            group: {
                option: self.settings_table[group][option]
                for option in options
            }
            for group, options in self.common_settings_table.items()
        }
        # Compile game settings table
        user_game_settings_table = {}
        for game in self.core.game_table.get_game_set():
            user_game_settings_table[game] = self.core.game_table.get_game_settings(
                game)

        # Both atomic replacements happen under one acquisition, so a reader
        # never observes a half-updated profile.
        with self._hold_profile_lock():
            helpers.save_table(
                self.user_common_settings_path,
                persistent_common_settings,
            )
            helpers.save_table(self.user_game_settings_path,
                               user_game_settings_table)


if __name__ == "__main__":
    helpers.debug_msg([CORE_MSG, i18n._(
        "This is the core module of Obozrenie Game Server Browser. Please run an appropriate UI instead.")])
