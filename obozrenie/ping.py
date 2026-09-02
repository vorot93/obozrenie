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

from collections.abc import Iterable
import time

from icmplib import ping as icmp_ping
from icmplib.exceptions import ICMPLibError

from obozrenie import network_calls
from obozrenie.models import Endpoint, Server


_RTT_BATCH_TIMEOUT_SECONDS = 5.0
_UNAVAILABLE_RTT = 9999


def _measure_host(host: str) -> int:
    try:
        result = icmp_ping(
            host,
            count=1,
            interval=0,
            timeout=1,
            privileged=False,
        )
        if not result.is_alive:
            return _UNAVAILABLE_RTT
        return round(result.avg_rtt)
    except (ICMPLibError, OSError, TypeError, ValueError):
        return _UNAVAILABLE_RTT


def _unavailable_outcomes(
    count: int,
) -> list[network_calls.CallOutcome[int]]:
    return [
        network_calls.CallOutcome.failure(
            network_calls.PoolUnavailableError(
                "ICMP network call pool is unavailable."
            )
        )
        for _ in range(count)
    ]


def _validated_supplied_deadline(deadline: float | None) -> float | None:
    """Validate a caller-supplied deadline, passing `None` through unchanged.

    Both facades validate before they touch their own inputs, so a malformed
    deadline is rejected before any iterable is consumed or any pool resolved.
    """
    if deadline is None:
        return None
    return network_calls.validate_deadline(deadline)


def measure_rtt(
    endpoints: Iterable[Endpoint],
    *,
    deadline: float | None = None,
) -> dict[Endpoint, int]:
    """Return an RTT or the unavailable sentinel for every endpoint."""
    deadline = _validated_supplied_deadline(deadline)

    endpoints = list(endpoints)
    if not endpoints:
        return {}

    hosts = tuple(dict.fromkeys(endpoint.host for endpoint in endpoints))
    if deadline is None:
        deadline = time.monotonic() + _RTT_BATCH_TIMEOUT_SECONDS
        deadline = network_calls.validate_deadline(deadline)

    pool = network_calls.get_network_call_pools().icmp
    try:
        pool.ensure_started()
    except BaseException:
        outcomes = _unavailable_outcomes(len(hosts))
    else:
        try:
            outcomes = pool.map_until(deadline, _measure_host, hosts)
        except network_calls.PoolUnavailableError as error:
            if type(error) is not network_calls.PoolUnavailableError:
                raise
            outcomes = _unavailable_outcomes(len(hosts))

    response_times = {}
    infrastructure_errors = (
        network_calls.CallDeadlineExceeded,
        network_calls.PoolSaturatedError,
        network_calls.PoolUnavailableError,
    )
    for host, outcome in zip(hosts, outcomes, strict=True):
        if not outcome.succeeded and type(outcome.error) in infrastructure_errors:
            response_times[host] = _UNAVAILABLE_RTT
            continue
        response_times[host] = outcome.result()

    return {
        endpoint: response_times[endpoint.host]
        for endpoint in endpoints
    }


def add_rtt_info(
    servers: Iterable[Server],
    *,
    deadline: float | None = None,
) -> list[Server]:
    """Return server records enriched with their response times."""
    deadline = _validated_supplied_deadline(deadline)

    servers = list(servers)
    if not servers:
        return []

    response_times = measure_rtt(
        (server.endpoint for server in servers),
        deadline=deadline,
    )

    return [
        server.with_network(response_times[server.endpoint], server.country)
        for server in servers
    ]
