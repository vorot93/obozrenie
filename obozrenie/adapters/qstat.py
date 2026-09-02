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


import math
import os
import re
import selectors
import shutil
import subprocess
import time
from collections.abc import Mapping
from xml.parsers.expat import ExpatError

import xmltodict

from obozrenie import helpers, i18n, resources
from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    diagnostic_for_exception,
)
from obozrenie.global_strings import BACKENDCAT_MSG
from obozrenie.models import Endpoint, Player, Server
from obozrenie.network_calls import validate_deadline
from obozrenie.qstat_processes import (
    QStatProcessSupervisor,
    get_qstat_process_supervisor,
)

QSTAT_MSG = BACKENDCAT_MSG + i18n._("QStat")
COLOR_CODE_PATTERN = re.compile(r"\^.")
SECURE_RULE_NAMES = {"punkbuster", "sv_punkbuster", "secure"}
PASSWORD_RULE_NAMES = {
    "g_needpass",
    "needpass",
    "si_usepass",
    "pswrd",
    "password",
}
FILTERED_SERVER_STATUSES = {"DOWN", "TIMEOUT", "ERROR"}
QSTAT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
QSTAT_PARENT_TIMEOUT = 60
# How long a failed child may take a SIGTERM before the SIGKILL follows. Only
# a deadline failure has already spent the operation's time, so every other
# failure stops its child on this bound instead of on whatever is left.
QSTAT_TERMINATE_GRACE_SECONDS = 2.0
_PIPE_CHUNK_BYTES = 64 * 1024


class QStatUnavailableError(SafeDiagnosticError, FileNotFoundError):
    """QStat is not installed; the text tells the user how to fix that."""


class QStatQueryError(SafeDiagnosticError, RuntimeError):
    """One QStat query failed for a reason the user can act on."""


def _query_error(text: str) -> QStatQueryError:
    """Declare one fixed, payload-free reason a QStat query failed.

    Core never stringifies an adapter failure, so a text that should reach
    the log has to be declared rather than passed to the exception alone.
    """
    return QStatQueryError(SafeDiagnostic(text))


def _log_cleanup_diagnostic(error: BaseException) -> None:
    """Report a cleanup failure that must never replace a primary one."""
    try:
        helpers.debug_msg([QSTAT_MSG, diagnostic_for_exception(error)])
    except Exception:
        # A query's own failure is propagating through the frame that calls
        # this, so a failing report is dropped rather than raised into it.
        pass


def find_qstat() -> str:
    for name in ("qstat", "quakestat"):
        path = shutil.which(name)
        if path:
            return path
    raise QStatUnavailableError(
        SafeDiagnostic("Install QStat (qstat or quakestat) to query this game.")
    )


def _stop_failed_child(
    supervisor: QStatProcessSupervisor,
    process: subprocess.Popen,
    deadline: float,
) -> None:
    """Stop one failed child with finite waits, never losing its handle.

    Termination escalates to a kill, and each wait may use only what is left
    of the operation deadline; the grace period before the kill is capped
    further, so a failure that has not already spent the deadline does not
    hand the child everything the deadline still has. A child that outlives
    that bound is handed to the supervisor's reaper rather than being waited
    on without a bound.
    """
    for stop, grace in (
        (process.terminate, QSTAT_TERMINATE_GRACE_SECONDS),
        (process.kill, math.inf),
    ):
        if process.poll() is not None:
            break
        try:
            stop()
        except ProcessLookupError:
            pass
        remaining = min(deadline - time.monotonic(), grace)
        try:
            process.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired:
            continue
        break

    if process.returncode is None:
        supervisor.defer_reap(process)
    else:
        supervisor.release_reaped(process)


def _run_qstat(
    command: list[str],
    input_data: bytes,
    *,
    supervisor: QStatProcessSupervisor,
    deadline: float,
    max_output_bytes: int = QSTAT_MAX_OUTPUT_BYTES,
) -> bytes:
    """Run one supervised QStat child against one absolute deadline.

    Stdin is fed while stdout is drained, retained output is bounded, and
    every wait on the child is bounded by what remains of `deadline`.
    """
    if max_output_bytes < 0:
        raise ValueError("QStat output limit cannot be negative")
    deadline = validate_deadline(deadline)
    timeout = max(0.0, deadline - time.monotonic())

    output = bytearray()
    input_view = memoryview(input_data)
    input_offset = 0
    selector = selectors.DefaultSelector()
    process = None
    stdin_pipe = None
    stdout_pipe = None
    released = False
    try:
        process = supervisor.spawn(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        stdin_pipe = process.stdin
        stdout_pipe = process.stdout
        assert stdin_pipe is not None and stdout_pipe is not None

        os.set_blocking(stdin_pipe.fileno(), False)
        os.set_blocking(stdout_pipe.fileno(), False)
        selector.register(stdout_pipe, selectors.EVENT_READ)
        if input_view:
            selector.register(stdin_pipe, selectors.EVENT_WRITE)
        else:
            stdin_pipe.close()
            stdin_pipe = None

        while stdout_pipe is not None or stdin_pipe is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, timeout)

            for key, _ in events:
                if key.fileobj is stdout_pipe:
                    read_size = min(
                        _PIPE_CHUNK_BYTES,
                        max_output_bytes + 1 - len(output),
                    )
                    try:
                        chunk = os.read(stdout_pipe.fileno(), read_size)
                    except BlockingIOError:
                        continue
                    if chunk:
                        if len(output) + len(chunk) > max_output_bytes:
                            raise _query_error(
                                f"QStat output exceeds {max_output_bytes} bytes."
                            )
                        output.extend(chunk)
                    else:
                        selector.unregister(stdout_pipe)
                        stdout_pipe.close()
                        stdout_pipe = None

                elif stdin_pipe is not None and key.fileobj is stdin_pipe:
                    try:
                        input_end = input_offset + _PIPE_CHUNK_BYTES
                        written = os.write(
                            stdin_pipe.fileno(),
                            input_view[input_offset:input_end],
                        )
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        written = len(input_view) - input_offset
                    input_offset += written
                    if input_offset == len(input_view):
                        selector.unregister(stdin_pipe)
                        stdin_pipe.close()
                        stdin_pipe = None

        remaining = deadline - time.monotonic()
        if remaining <= 0 and process.poll() is None:
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            returncode = process.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(command, timeout) from None

        supervisor.release_reaped(process)
        released = True
        if returncode:
            raise subprocess.CalledProcessError(returncode, command)
        return bytes(output)
    finally:
        try:
            # Any exit that left the child unreaped, including an
            # interruption, stops it here before the pipes it holds are
            # closed. The child stays registered however that goes, so the
            # supervisor still owns it.
            if process is not None and not released:
                _stop_failed_child(supervisor, process, deadline)
        except Exception as error:
            # This only ever runs while the query's own failure is
            # propagating: a failing stop is reported, never allowed to
            # replace it, and neither is a failing report.
            _log_cleanup_diagnostic(error)
        finally:
            # Closing the pipes is unconditional: a stop that failed must not
            # leave this process holding the descriptors as well.
            selector.close()
            if stdin_pipe is not None:
                stdin_pipe.close()
            if stdout_pipe is not None:
                stdout_pipe.close()


def debug_msg(game_name: str, msg: str | None = None) -> None:
    if msg is not None:
        helpers.debug_msg([QSTAT_MSG, game_name, msg])


def adapt_master_entry(qstat_entry, game) -> None:
    return None


def _parse_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("QStat XML text must be a string")
    return value


def _parse_rule_flag(value: str) -> bool:
    try:
        return bool(int(value))
    except ValueError:
        return False


def _parse_required_capacity(value: object) -> int:
    capacity = int(_parse_text(value))
    if capacity < 0:
        raise ValueError("QStat capacity cannot be negative")
    return capacity


def parse_player_entry(player: Mapping) -> Player:
    """Parses player entry returned by QStat"""
    name = COLOR_CODE_PATTERN.sub("", _parse_text(player["name"]))
    try:
        score = int(player["score"])
    except (KeyError, TypeError, ValueError):
        score = None
    try:
        player_ping = int(player["ping"])
    except (KeyError, TypeError, ValueError):
        player_ping = 9999

    return Player(name=name, score=score, ping=player_ping)


def adapt_server_entry(qstat_entry, game) -> Server | None:
    """Parses server entry returned by QStat"""
    status = qstat_entry["@status"]
    if status in FILTERED_SERVER_STATUSES:
        return None
    if status != "UP":
        raise ValueError("Unsupported QStat server status")

    name = COLOR_CODE_PATTERN.sub("", _parse_text(qstat_entry["name"]))
    game_type = COLOR_CODE_PATTERN.sub("", _parse_text(qstat_entry["gametype"]))
    player_count = _parse_required_capacity(qstat_entry["numplayers"])
    player_limit = _parse_required_capacity(qstat_entry["maxplayers"])
    try:
        server_ping = int(qstat_entry["ping"])
    except (KeyError, TypeError):
        server_ping = 9999

    password = False
    secure = False
    game_mod = ""
    rules = {}
    players = []

    rules_section = qstat_entry.get("rules")
    if rules_section is not None:
        rules_list = helpers.enforce_array(rules_section["rule"])
        for rule in rules_list:
            if any(key != "#text" and not key.startswith("@") for key in rule):
                raise TypeError("QStat rule text must be scalar")
            rule_name = rule["@name"]
            rule_text = _parse_text(rule.get("#text"))
            rules[rule_name] = rule_text

            if rule_name in SECURE_RULE_NAMES:
                secure = _parse_rule_flag(rule_text)
            elif rule_name == "game":
                game_mod = rule_text
            elif rule_name in PASSWORD_RULE_NAMES:
                password = _parse_rule_flag(rule_text)

    players_section = qstat_entry.get("players")
    if players_section is not None:
        player_list = helpers.enforce_array(players_section["player"])
        players = [parse_player_entry(player) for player in player_list]

    return Server(
        endpoint=Endpoint.parse(str(qstat_entry["@address"])),
        game_id=game,
        name=name,
        password=password,
        player_count=player_count,
        player_limit=player_limit,
        ping=server_ping,
        secure=secure,
        game_mod=game_mod,
        game_type=game_type,
        terrain=_parse_text(qstat_entry["map"]),
        players=tuple(players),
        rules=rules,
    )


def adapt_qstat_entry(qstat_entry, game, master_type, server_type) -> Server | None:
    if qstat_entry["@type"] == master_type:
        return adapt_master_entry(qstat_entry, game)
    if qstat_entry["@type"] == server_type:
        return adapt_server_entry(qstat_entry, game)
    raise ValueError("Unsupported QStat entry type")


def adapt_server_list(
    qstat_string,
    game,
    game_name,
    qstat_master_type,
    qstat_server_type,
    server_game_name,
    server_game_type,
) -> list[Server]:
    server_table = []
    malformed_count = 0
    schema_valid_count = 0

    try:
        document = xmltodict.parse(qstat_string)
    except ExpatError:
        raise _query_error("QStat returned malformed XML.") from None
    if "qstat" not in document:
        raise ValueError("QStat response must contain a qstat root")
    qstat_root = document["qstat"]
    if qstat_root is None:
        return []
    if not isinstance(qstat_root, Mapping):
        raise ValueError("QStat response must contain a qstat root")

    raw_entries = qstat_root.get("server", [])
    if isinstance(raw_entries, Mapping):
        entries = [raw_entries]
    elif isinstance(raw_entries, list):
        entries = raw_entries
    elif raw_entries is None:
        entries = []
    else:
        raise ValueError("QStat response server entries must be a mapping or list")

    for qstat_entry in entries:
        try:
            server = adapt_qstat_entry(
                qstat_entry, game, qstat_master_type, qstat_server_type
            )
        except (KeyError, TypeError, ValueError):
            malformed_count += 1
            continue

        schema_valid_count += 1
        if server is None:
            continue
        for expected, actual in (
            (server_game_name, server.rules.get("gamename")),
            (server_game_type, server.game_type),
        ):
            if expected is not None and actual != expected:
                break
        else:
            server_table.append(server)

    if malformed_count:
        debug_msg(
            game_name,
            i18n._("Skipped malformed QStat server entries: %(count)i.") % {
                "count": malformed_count,
            },
        )
    if entries and not schema_valid_count:
        raise ValueError("QStat response contained no valid server entries.")

    return server_table


def build_host_list(master_list: list):
    """Strip protocol prefixes from master URIs and de-duplicate, preserving order.

    Order is significant: qstat returns zero servers if the *last* master in its
    input list times out, so the configured order (which lists reachable masters
    last) must be preserved. Using a set here shuffled the order per process and
    intermittently moved a dead master last, yielding empty Quake III lists.
    """
    hosts = []
    for entry in master_list:
        entry = entry.strip()
        if '://' in entry:
            entry = entry.split('://', 1)[1]
        hosts.append(entry)
    return list(dict.fromkeys(hosts))


def stat_master(game: str, game_info: dict, master_list: list) -> list[Server]:
    game_name = game_info["name"]
    backend_config = resources.load_toml(
        "settings", "internal", "backends", "qstat.toml"
    )["game"][game]
    qstat_master_type = backend_config["master_type"]
    qstat_server_type = backend_config["server_type"]
    server_game_name = backend_config.get("server_gamename")
    server_game_type = backend_config.get("server_gametype")

    qstat_cmd = [
        find_qstat(),
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
    ]

    qstat_stdin_descriptor = qstat_master_type
    if server_game_type is not None:
        qstat_stdin_descriptor += ",game=" + server_game_type
    qstat_stdin = "\n".join(
        f"{qstat_stdin_descriptor} {host}"
        for host in build_host_list(master_list)
    ).encode("utf-8")

    debug_msg(game_name, i18n._("Requesting server info."))
    stat_start_time = time.time()
    # One absolute deadline covers the whole operation: pumping the pipes,
    # waiting for a normal exit, and stopping a child that failed.
    deadline = time.monotonic() + QSTAT_PARENT_TIMEOUT
    try:
        qstat_output_bytes = _run_qstat(
            qstat_cmd,
            qstat_stdin,
            supervisor=get_qstat_process_supervisor(),
            deadline=deadline,
            max_output_bytes=QSTAT_MAX_OUTPUT_BYTES,
        )
    except subprocess.TimeoutExpired:
        raise _query_error("QStat query timed out.") from None
    except subprocess.CalledProcessError:
        raise _query_error("QStat executable exited unsuccessfully.") from None

    try:
        qstat_output = qstat_output_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _query_error("QStat output is not valid UTF-8.") from None
    stat_total_time = time.time() - stat_start_time
    debug_msg(
        game_name,
        i18n._("Received server info. Elapsed time: %(stat_time)s s.") % {
            "stat_time": round(stat_total_time, 2),
        },
    )

    parse_start_time = time.time()
    server_table = adapt_server_list(
        qstat_output,
        game,
        game_name,
        qstat_master_type,
        qstat_server_type,
        server_game_name,
        server_game_type,
    )
    parse_time = round((time.time() - parse_start_time) * 1000, 2)
    debug_msg(
        game_name,
        i18n._("Parsed QStat response. Elapsed time: %(parse_time)s ms") % {
            "parse_time": parse_time,
        },
    )

    return server_table
