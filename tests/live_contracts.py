import sys
import time
from collections.abc import Callable, Mapping

from obozrenie import http_client, network_calls, resources
from obozrenie.adapters import minetest, rigsofrods
from obozrenie.models import Server


SCRIPT_DEADLINE_SECONDS = 120.0
ATTEMPT_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3
_PARSER_ERRORS = (KeyError, TypeError, ValueError)
_MINETEST_PARSER_ERRORS = (KeyError, OverflowError, TypeError, ValueError)


class LiveContractError(RuntimeError):
    pass


class ScriptDeadlineExceeded(LiveContractError):
    pass


def _parse_entries(
    label: str,
    game_id: str,
    entries: list,
    parser: Callable[[str, Mapping], Server],
    parser_errors: tuple[type[Exception], ...],
) -> list[Server]:
    servers = []
    for entry in entries:
        try:
            servers.append(parser(game_id, entry))
        except parser_errors:
            continue
    if not servers:
        raise LiveContractError(
            f"{label} response contained no valid production server records"
        )
    return servers


def validate_minetest(payload: object) -> list[Server]:
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("list"), list
    ):
        raise LiveContractError(
            "Minetest response must be a mapping containing a server list"
        )
    return _parse_entries(
        "Minetest",
        "minetest",
        payload["list"],
        minetest.parse_json_entry,
        _MINETEST_PARSER_ERRORS,
    )


def validate_rigsofrods(payload: object) -> list[Server]:
    if not isinstance(payload, list):
        raise LiveContractError(
            "Rigs of Rods response must be a top-level server list"
        )
    return _parse_entries(
        "Rigs of Rods",
        "rigsofrods",
        payload,
        rigsofrods.parse_server_entry,
        _PARSER_ERRORS,
    )


def _is_transient(error: Exception) -> bool:
    failure = http_client.describe_http_failure(error)
    if failure is None:
        return False
    if failure.kind is http_client.HttpFailureKind.STATUS:
        return failure.status_code in {429, 500, 502, 503, 504}
    return failure.kind in {
        http_client.HttpFailureKind.TIMEOUT,
        http_client.HttpFailureKind.CONNECTION,
        http_client.HttpFailureKind.RESPONSE_INTEGRITY,
        http_client.HttpFailureKind.DECODE,
    }


def _deadline_error(label: str) -> ScriptDeadlineExceeded:
    return ScriptDeadlineExceeded(
        f"{label} live-contract script deadline expired"
    )


def fetch_validated(
    label: str,
    url: str,
    validator: Callable[[object], list[Server]],
    script_deadline: float,
) -> list[Server]:
    http_client.validate_https_url(url)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        now = time.monotonic()
        if now >= script_deadline:
            raise _deadline_error(label)
        attempt_deadline = min(
            script_deadline,
            now + ATTEMPT_TIMEOUT_SECONDS,
        )
        try:
            payload = http_client.get_json(url, deadline=attempt_deadline)
        except Exception as error:
            if not _is_transient(error):
                raise
            if time.monotonic() >= script_deadline:
                raise _deadline_error(label) from error
            if attempt == MAX_ATTEMPTS:
                break
            print(
                f"{label} transient failure on attempt {attempt}; retrying.",
                file=sys.stderr,
            )
            continue
        servers = validator(payload)
        if time.monotonic() >= script_deadline:
            raise _deadline_error(label)
        return servers
    raise LiveContractError(
        f"{label} live contract failed after {MAX_ATTEMPTS} attempts."
    ) from None


def _master_urls(configuration: Mapping, game_id: str) -> list[str]:
    try:
        urls = configuration[game_id]["master_uri"]
    except (KeyError, TypeError):
        raise LiveContractError(
            f"packaged {game_id} settings do not define master_uri"
        ) from None
    if not isinstance(urls, list) or not urls or any(
        type(url) is not str or not url for url in urls
    ):
        raise LiveContractError(
            f"packaged {game_id} master_uri must be a nonempty list of URLs"
        )
    return urls


def configured_endpoints():
    configuration = resources.load_toml(
        "settings",
        "defaults",
        "games.toml",
    )
    endpoints = []
    for url in _master_urls(configuration, "minetest"):
        endpoints.append(("minetest", "Minetest", url, validate_minetest))
    for url in _master_urls(configuration, "rigsofrods"):
        endpoints.append((
            "rigsofrods",
            "Rigs of Rods",
            rigsofrods.master_request_url(url),
            validate_rigsofrods,
        ))
    for _game_id, _label, url, _validator in endpoints:
        http_client.validate_https_url(url)
    return tuple(endpoints)


def run_contracts() -> dict[str, list[Server]]:
    script_deadline = time.monotonic() + SCRIPT_DEADLINE_SECONDS
    results = {"minetest": [], "rigsofrods": []}
    for game_id, label, url, validator in configured_endpoints():
        results[game_id].extend(
            fetch_validated(label, url, validator, script_deadline)
        )
    return results


def main() -> dict[str, list[Server]]:
    body_completed = False
    try:
        results = run_contracts()
        for game_id, servers in results.items():
            print(f"{game_id}: {len(servers)} valid server record(s)")
        body_completed = True
        return results
    finally:
        try:
            network_calls.close_network_call_pools()
        except BaseException:
            if body_completed:
                raise


if __name__ == "__main__":
    main()
