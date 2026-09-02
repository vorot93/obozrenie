import time
from collections.abc import Callable

import requests

from obozrenie import helpers, http_client, i18n
from obozrenie.diagnostics import SafeDiagnostic, SafeDiagnosticError, safe_url
from obozrenie.models import Server


class AllMastersFailedError(SafeDiagnosticError):
    def __init__(self) -> None:
        super().__init__(SafeDiagnostic("All configured master sources failed."))


_MALFORMED_URL = SafeDiagnostic("Malformed master URL.")
_MALFORMED_PAYLOAD = SafeDiagnostic("Malformed master payload.")


def _log_master_failure(
    diagnostic_prefix: str,
    source: str,
    diagnostic: SafeDiagnostic,
) -> None:
    helpers.debug_msg([
        diagnostic_prefix,
        i18n._("Failed to read master %(uri)s.") % {
            "uri": safe_url(source),
        },
        diagnostic,
    ])


def collect_http_masters(
    game: str,
    master_list: list[str],
    *,
    request_url: Callable[[str], str],
    adapt_payload: Callable[[str, object], list[Server]],
    diagnostic_prefix: str,
) -> list[Server]:
    deadline = time.monotonic() + 30.0
    servers = []
    successful_masters = 0

    for source in master_list:
        try:
            url = request_url(source)
        except ValueError:
            _log_master_failure(diagnostic_prefix, source, _MALFORMED_URL)
            continue

        try:
            payload = http_client.get_json(url, deadline=deadline)
        except Exception as error:
            failure = http_client.describe_http_failure(error)
            if failure is None:
                raise
            _log_master_failure(
                diagnostic_prefix,
                source,
                failure.diagnostic,
            )
            continue

        try:
            source_servers = adapt_payload(game, payload)
        except requests.exceptions.RequestException:
            raise
        except ValueError:
            _log_master_failure(diagnostic_prefix, source, _MALFORMED_PAYLOAD)
            continue

        successful_masters += 1
        servers.extend(source_servers)

    if master_list and not successful_masters:
        raise AllMastersFailedError()

    return servers
