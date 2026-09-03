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

"""Helper functions for processing data."""

import contextlib
import copy
import os
import tempfile
import tomllib
import tomli_w
import time
from dataclasses import dataclass

from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    diagnostic_for_exception,
)
from obozrenie.global_settings import *
from obozrenie.global_strings import *

from obozrenie import i18n


# A non-cooperating writer can replace the user table while it is being
# recovered; recovery inspects at most this many snapshots before giving up.
_RECOVERY_SNAPSHOT_LIMIT = 3


class SettingsReadError(SafeDiagnosticError, OSError):
    """A user settings file exists but could not be read.

    The original names the file and carries `errno`/`strerror`, none of which
    may reach a log the payload-free policy governs; a signal handler cannot
    re-raise either, so this fixed text is the only thing the user is shown.
    It stays an `OSError` so callers keep reading these as the failure family
    they have always been.
    """

    def __init__(self) -> None:
        super().__init__(SafeDiagnostic("Settings file could not be read."))


class SettingsWriteError(SafeDiagnosticError, OSError):
    """A user settings file could not be written or replaced.

    Declared, suppressed and typed exactly as `SettingsReadError` is, and for
    the same reasons.
    """

    def __init__(self) -> None:
        super().__init__(SafeDiagnostic("Settings file could not be written."))


class SettingsBackupError(SafeDiagnosticError, OSError):
    """A malformed user settings file could not be archived beside itself.

    Declared, suppressed and typed exactly as `SettingsReadError` is, and for
    the same reasons: the original names the profile directory.
    """

    def __init__(self) -> None:
        super().__init__(SafeDiagnostic("Settings backup could not be written."))


def debug_msg_str(msg):
    return " | ".join([time.strftime('%F %T')] + [str(part) for part in msg])


def debug_msg(msg=None):
    if msg is not None:
        print(debug_msg_str(msg))


def log_safe_diagnostic(prefix: str, error: BaseException) -> None:
    """Log one exception as its fixed safe text, never as its stringification.

    Reporting is best effort. The failure being reported is often the log
    stream itself, and every caller has something to finish after this call
    — re-raising a primary failure, quitting the application — that a second
    failure out of the report would skip, so a report that cannot be written
    is dropped here rather than raised.
    """
    try:
        debug_msg([prefix, diagnostic_for_exception(error)])
    except Exception:
        pass


def enforce_array(entry_array):
    if not isinstance(entry_array, list):
        entry_array = [entry_array]
    return entry_array


def deepcopy(value):
    return copy.deepcopy(value)


def sort_dict_table(dict_table: list, sort_key) -> list:
    sorted_dict_list = sorted(dict_table, key=lambda k: k[sort_key])

    return sorted_dict_list


def dict_to_list(dict_table, key_list):
    list_table = []

    if dict_table is not None:
        for entry in dict_table:
            list_table.append([])

            for key in key_list:
                try:
                    list_table[-1].append(entry[key])
                except KeyError:
                    list_table[-1].append(None)

    return list_table


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """One complete read of a file with the identity it was read from."""

    payload: bytes
    identity: tuple[int, int, int, int, int]

    @staticmethod
    def identify(status: os.stat_result) -> tuple[int, int, int, int, int]:
        """Device, inode, mode, size, and nanosecond modification time."""
        return (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_size,
            status.st_mtime_ns,
        )


def _read_file_snapshot(path: str) -> _FileSnapshot | None:
    """Read one file completely, identifying it from its own descriptor."""
    try:
        with open(path, "rb") as table_open_object:
            # Identity is taken before the read, so a writer that changes the
            # file underneath it contradicts this snapshot instead of being
            # silently absorbed into it.
            identity = _FileSnapshot.identify(
                os.fstat(table_open_object.fileno())
            )
            payload = table_open_object.read()
    except FileNotFoundError:
        return None
    except OSError:
        # An unreadable file is a failure this run reports rather than
        # recovers, and the exception it fails with is declared so that the
        # report carries a reason instead of only a type name.
        raise SettingsReadError from None
    return _FileSnapshot(payload=payload, identity=identity)


def _snapshot_is_current(path: str, snapshot: _FileSnapshot) -> bool:
    """Report whether the live path still resolves to the snapshot's file."""
    try:
        status = os.stat(path)
    except OSError:
        return False
    return _FileSnapshot.identify(status) == snapshot.identity


@contextlib.contextmanager
def _private_sibling_writer(directory: str, prefix: str):
    """Open a fresh, private ``0600`` sibling file in `directory` for writing.

    `mkstemp`'s descriptor is `fstat`'d (identifying the exact file this call
    owns, for a caller that needs an identity-checked cleanup), `fchmod`'d to
    0600, and wrapped for buffered binary writing before this yields
    ``(sibling_path, sibling_file, reservation)``. The caller writes through
    `sibling_file` and alone decides how to finalize a completed write (a
    rename onto a fixed path, or simply keeping the random name) and, on its
    own failure once writing has started, how to roll back — this function
    never removes the sibling for a failure the caller could itself react to.

    The one failure this *does* handle itself is one in this shared setup,
    before the caller ever sees a path to roll back with: it then removes the
    file `mkstemp` just made, matching the stronger of the two current
    callers' guarantees against a dangling temporary file. That removal is
    best effort, like every rollback in this module: it runs while the setup
    failure is propagating and must not replace it with its own.
    """
    descriptor, sibling_path = tempfile.mkstemp(dir=directory, prefix=prefix)
    entered = False
    try:
        reservation = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        sibling_file = os.fdopen(descriptor, "wb")
        descriptor = None
        entered = True
        with sibling_file:
            yield sibling_path, sibling_file, reservation
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not entered:
            try:
                os.unlink(sibling_path)
            except OSError:
                pass


def _remove_partial_backup(backup_path: str, reservation: os.stat_result) -> None:
    """Remove a partial archive while the name still holds this call's file.

    Removal is best effort: it runs while the write failure that left the
    archive partial is propagating, and a rollback that fails — the profile
    directory's permissions changed underneath it — must not replace that
    failure with its own.
    """
    try:
        partial = os.lstat(backup_path)
        if (partial.st_dev, partial.st_ino) == (
            reservation.st_dev,
            reservation.st_ino,
        ):
            os.unlink(backup_path)
    except OSError:
        pass


def _write_invalid_backup(path: str, payload: bytes) -> str:
    """Archive malformed bytes in a private sibling of their source.

    Every filesystem failure here is declared as `SettingsBackupError`, for
    the reason the read and save paths declare theirs: the original names
    the profile directory, and the fixed text is all a failed startup
    reports.
    """
    directory = os.path.dirname(path) or "."
    installed = False
    try:
        with _private_sibling_writer(
            directory, f"{os.path.basename(path)}.invalid-"
        ) as (backup_path, backup_open_object, reservation):
            try:
                backup_open_object.write(payload)
                backup_open_object.flush()
                os.fsync(backup_open_object.fileno())
                # The archive is durable from here on, so a failure in the
                # implicit close below must not take it away again.
                installed = True
            finally:
                if not installed:
                    _remove_partial_backup(backup_path, reservation)
    except OSError:
        raise SettingsBackupError from None
    return backup_path


def load_user_table(path: str | os.PathLike[str]) -> tuple[dict, str | None]:
    """Load a user TOML table, archiving malformed input beside it.

    Only one immutable snapshot is parsed, and malformed bytes are copied into
    a uniquely allocated private sibling: the live pathname is never renamed,
    unlinked, chmodded, or otherwise mutated, so a valid replacement another
    process installs survives and is loaded instead of being destroyed. A
    source that keeps changing is abandoned after a fixed snapshot budget
    rather than retried forever.
    """
    path = os.fspath(path)
    for _ in range(_RECOVERY_SNAPSHOT_LIMIT):
        snapshot = _read_file_snapshot(path)
        if snapshot is None:
            return {}, None
        try:
            return tomllib.loads(snapshot.payload.decode("utf-8")), None
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            pass

        if not _snapshot_is_current(path, snapshot):
            continue
        _write_invalid_backup(path, snapshot.payload)
        if not _snapshot_is_current(path, snapshot):
            # A replacement landed while those bytes were being archived. It
            # stays live and the next snapshot loads it.
            continue
        return {}, i18n._(
            "Invalid user settings were backed up and defaults were restored."
        )
    raise RuntimeError("User settings changed during bounded recovery.")


def save_table(path, data):
    """Write one settings table, publishing it with an atomic replacement.

    The private sibling is `fchmod`ded to `0600` through its own descriptor
    before anything is written, so the installed file carries that mode from
    the instant it appears under this name and no umask can widen it. Nothing
    then touches the live pathname but the replacement itself: a `chmod` on a
    name could only ever act on whatever occupies it at that moment — a file
    another process installed, or the target of a symlink it left there.

    Every filesystem failure here is declared as `SettingsWriteError` so the
    reason survives a log that may not print the exception's own text. The
    rollback below is a `finally`, so it still removes an unpublished sibling
    on the way out of a declared failure, and it is best effort, so a
    rollback that fails cannot replace that declared failure with its own.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path)

    temporary_path = None
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        else:
            directory = "."

        with _private_sibling_writer(
            directory, f".{os.path.basename(path)}."
        ) as (temporary_path, table_open_object, _reservation):
            tomli_w.dump(data, table_open_object)
            table_open_object.flush()
            os.fsync(table_open_object.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
    except OSError:
        # Every one of these names the path it failed on, so the declaration
        # replaces it rather than chaining it into a printable traceback.
        raise SettingsWriteError from None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                # A sibling that cannot be removed — the directory's
                # permissions changed underneath this save — is not a reason
                # to hide the failure propagating through this `finally`.
                pass
