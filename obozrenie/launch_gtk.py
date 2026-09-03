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

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TYPE_CHECKING

from obozrenie.core import Core
from obozrenie.game_table import GameTable
from obozrenie.geoip import Geolocation
from obozrenie.global_settings import PROFILE_PATH
from obozrenie.network_calls import (
    NetworkCallPools,
    get_network_call_pools,
    validate_deadline,
)
from obozrenie.qstat_processes import (
    QStatProcessSupervisor,
    get_qstat_process_supervisor,
)
from obozrenie.settings import Settings

if TYPE_CHECKING:
    from obozrenie.gtk_geoip import GeoIPDownloadCoordinator


# Every owned service gets this long, in total, to finish closing.
_APPLICATION_SHUTDOWN_TIMEOUT_SECONDS = 1.0


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


def _begin_close_owners(owners):
    """Mark every owner closing; report which completed and the first failure.

    Every owner is visited even after one of them fails, because an owner that
    is never told to close keeps its workers running. The first failure is
    preserved for the caller to re-raise once the whole sweep is done.
    """
    completed = []
    first_error = None
    for owner in owners:
        try:
            owner.begin_close()
        except BaseException as error:
            completed.append(False)
            if first_error is None:
                first_error = error
        else:
            completed.append(True)
    return completed, first_error


def _join_owners_until(owners, deadline):
    """Join every owner against one unchanged deadline; report the first error.

    The same absolute deadline reaches every owner, so the whole shutdown is
    bounded once rather than once per service, and each owner computes its own
    remaining time from it.
    """
    first_error = None
    for owner in owners:
        try:
            owner.join_until(deadline)
        except BaseException as error:
            if first_error is None:
                first_error = error
    return first_error


class ApplicationServices:

    """Owner of every service one application run creates.

    The services stay purpose-specific; this owner only gives them one
    two-phase shutdown. `begin_close` marks all of them closing and requests
    cancellation, and only then does `join_until` wait for their workers, so no
    service is joined while another is still accepting work. Both phases visit
    every owner even after a failure and preserve the first one, and repeating
    `begin_close` retries only the owners not yet known to have completed it.
    """

    def __init__(
        self,
        core_instance: Core,
        geoip_downloads: GeoIPDownloadCoordinator,
        qstat_processes: QStatProcessSupervisor,
        network_pools: NetworkCallPools,
    ) -> None:
        self.core = core_instance
        self.geoip_downloads = geoip_downloads
        self.qstat_processes = qstat_processes
        self.network_pools = network_pools
        # Core first: it stops admitting the queries that keep the rest busy.
        self._owners = (
            core_instance,
            geoip_downloads,
            qstat_processes,
            network_pools.dns,
            network_pools.http,
            network_pools.icmp,
        )
        self._lock = threading.Lock()
        self._complete = [False] * len(self._owners)

    def begin_close(self) -> None:
        """Mark every service closing and request cancellation from each."""
        first_error = self._begin_close_pending()
        if first_error is not None:
            raise first_error

    def join_until(self, deadline: float) -> None:
        """Join every service against one absolute deadline."""
        deadline = validate_deadline(deadline)
        first_error = _join_owners_until(self._owners, deadline)
        if first_error is not None:
            raise first_error

    def close_until(self, deadline: float) -> None:
        """Close every service and join it against one absolute deadline."""
        # Validate first: a malformed deadline must not half-close the app.
        deadline = validate_deadline(deadline)
        first_error = self._begin_close_pending()
        join_error = _join_owners_until(self._owners, deadline)
        if first_error is None:
            first_error = join_error
        if first_error is not None:
            raise first_error

    def _begin_close_pending(self) -> BaseException | None:
        """Begin close on the owners not known complete; report the failure.

        An owner whose `begin_close` raised is not marked complete, so a later
        close retries exactly that owner instead of closing the rest twice.
        """
        with self._lock:
            pending = [
                index
                for index, complete in enumerate(self._complete)
                if not complete
            ]
        # Closing an owner is that owner's own work, never this lock's.
        completed, first_error = _begin_close_owners(
            [self._owners[index] for index in pending])
        with self._lock:
            for index, complete in zip(pending, completed):
                if complete:
                    self._complete[index] = True
        return first_error


def _process_wide_owners():
    """Return the services that exist before one launch constructs anything.

    The QStat supervisor and the network pools are created with their modules,
    so a launch never builds them and cleanup owns them unconditionally.
    """
    pools = get_network_call_pools()
    return [
        get_qstat_process_supervisor(),
        pools.dns,
        pools.http,
        pools.icmp,
    ]


def _close_application_services(services, started_owners):
    """Close every service this launch created, bounded by one deadline.

    The deadline is taken here so that it bounds the shutdown itself rather
    than the run before it. Once the services owner exists it owns every
    service, and closing it is the whole cleanup; before that, the owners this
    process already had and whatever construction built get the same two
    phases.
    """
    deadline = time.monotonic() + _APPLICATION_SHUTDOWN_TIMEOUT_SECONDS
    if services is not None:
        services.close_until(deadline)
        return
    _completed, first_error = _begin_close_owners(started_owners)
    join_error = _join_owners_until(started_owners, deadline)
    if first_error is None:
        first_error = join_error
    if first_error is not None:
        raise first_error


def main():
    exit_status = 0
    body_completed = False
    services_instance = None
    # Seeded with the services this process already owns, so a construction
    # failure before the services owner exists still closes them.
    started_owners = _process_wide_owners()
    try:
        try:
            _ensure_macos_library_path()
            from obozrenie.gtk import App
            from obozrenie.gtk_geoip import GeoIPDownloadCoordinator
            game_table = GameTable.from_shipped_list()
            geolocation = Geolocation.discover()
            core_instance = Core(game_table, geolocation)
            started_owners.append(core_instance)
            coordinator_instance = GeoIPDownloadCoordinator(geolocation)
            started_owners.append(coordinator_instance)
            services_instance = ApplicationServices(
                core_instance,
                coordinator_instance,
                get_qstat_process_supervisor(),
                get_network_call_pools(),
            )
            settings_instance = Settings(game_table, os.path.expanduser(PROFILE_PATH))
            app_instance = App(core_instance, settings_instance, services_instance)
            app_instance.run(None)
            if app_instance.handler_failure is not None:
                # PyGObject prints and swallows whatever a startup or
                # shutdown handler raises, so `run()` returns success for a
                # settings file that never loaded or never saved. The App
                # recorded that failure instead of losing it, and this run
                # ends as the failure it was.
                exit_status = 1
        except Exception as error:
            print(error)
            exit_status = 1
        body_completed = True
    finally:
        try:
            _close_application_services(services_instance, started_owners)
        except BaseException:
            if body_completed and not exit_status:
                raise

    if exit_status:
        raise SystemExit(exit_status)


if __name__ == "__main__":
    main()
