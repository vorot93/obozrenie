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

import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from obozrenie import helpers, i18n
from obozrenie.global_strings import LAUNCHER_MSG
from obozrenie.models import Endpoint


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """One complete, self-contained description of a client invocation.

    The working directory is part of the snapshot rather than something the
    executor derives, so a specification built on one thread launches exactly
    the same client from any other.
    """

    argv: tuple[str, ...]
    cwd: str
    env_overrides: Mapping[str, str] = field(default_factory=dict)
    sensitive_values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.cwd) is not str:
            raise TypeError("Launch working directory must be an exact str.")
        if not os.path.isabs(self.cwd):
            raise ValueError(
                "Launch working directory must be an absolute path."
            )
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(
            self,
            "env_overrides",
            MappingProxyType(dict(self.env_overrides)),
        )
        object.__setattr__(
            self,
            "sensitive_values",
            frozenset(self.sensitive_values),
        )

    def redacted_command(self) -> str:
        return " ".join(
            "***" if arg in self.sensitive_values else arg
            for arg in self.argv
        )


def _sanitize_diagnostic(
    error: Exception,
    sensitive_values: Iterable[str],
) -> str:
    diagnostic = str(error)
    for sensitive_value in sorted(
        filter(None, sensitive_values),
        key=len,
        reverse=True,
    ):
        diagnostic = diagnostic.replace(sensitive_value, "***")
    return diagnostic


def _absolute_path(base_directory: str, path: str) -> str:
    """Resolve one path against an absolute base, free of process state.

    `normpath` collapses `..` lexically, so a path crossing a symlinked
    component resolves differently from the kernel's own walk. That is the
    deliberate choice here: this text is what the child receives and what the
    redacted command and diagnostics show, so it stays readable and
    deterministic instead of depending on a filesystem this process may not be
    able to read.
    """
    return os.path.normpath(os.path.join(base_directory, path))


def _resolve_launch_context(
    executable: str,
    configured_workdir: str | None,
    activation_directory: str,
) -> tuple[str, str]:
    """Resolve one client's executable text and absolute working directory.

    A bare command keeps its exact text so `PATH` lookup still applies, and an
    absolute executable is used as written. A relative executable carrying a
    directory component is resolved exactly once here — against the configured
    working directory when there is one and against the activation directory
    otherwise — because the child would otherwise resolve it a second time
    against its own working directory.

    Both settings are read stripped, so a value that is only whitespace is the
    unset one the emptiness checks already take it for. Without that, a
    workdir of spaces becomes a directory of spaces and an executable of
    spaces becomes a command of spaces, each failing inside `Popen` with
    nothing the user could act on.
    """
    if not os.path.isabs(activation_directory):
        raise ValueError("Launch activation directory must be absolute.")

    workdir = None
    configured_workdir = (configured_workdir or "").strip()
    if configured_workdir:
        workdir = _absolute_path(
            activation_directory,
            os.path.expanduser(configured_workdir),
        )

    path = os.path.expanduser(executable.strip())
    if not os.path.dirname(path):
        return path, workdir or activation_directory
    if not os.path.isabs(path):
        path = _absolute_path(workdir or activation_directory, path)
    return path, workdir or os.path.dirname(path)


def steam_launch_pattern(
    path: str,
    cwd: str,
    endpoint: Endpoint,
    password: str,
    steam_app_id: str,
    **kwargs,
) -> LaunchSpec:
    launch_cmd = (
        path,
        "-applaunch",
        steam_app_id,
        "+connect",
        str(endpoint),
    )
    if password:
        launch_cmd += ("+password", password)

    return LaunchSpec(
        launch_cmd,
        cwd,
        sensitive_values=frozenset({password}) if password else frozenset(),
    )


def rigsofrods_launch_pattern(path: str, cwd: str, **kwargs) -> LaunchSpec:
    return LaunchSpec((path,), cwd)


def quake_launch_pattern(
    path: str,
    cwd: str,
    endpoint: Endpoint,
    password: str,
    **kwargs,
) -> LaunchSpec:
    launch_cmd = (path, "+connect", str(endpoint))
    if password:
        launch_cmd += ("+password", password)

    return LaunchSpec(
        launch_cmd,
        cwd,
        sensitive_values=frozenset({password}) if password else frozenset(),
    )


def hl2_launch_pattern(
    game: str,
    path: str,
    cwd: str,
    endpoint: Endpoint,
    password: str,
    **kwargs,
) -> LaunchSpec:
    ld_library_path = os.environ.get("LD_LIBRARY_PATH")
    env_overrides = {
        "LD_LIBRARY_PATH": (
            f"./bin:{ld_library_path}" if ld_library_path else "./bin"
        ),
    }
    launch_cmd = (
        path,
        "-game",
        game,
        "+connect",
        str(endpoint),
    )
    if password:
        launch_cmd += ("+password", password)

    return LaunchSpec(
        launch_cmd,
        cwd,
        env_overrides=env_overrides,
        sensitive_values=frozenset({password}) if password else frozenset(),
    )


def openttd_launch_pattern(
    path: str,
    cwd: str,
    endpoint: Endpoint,
    **kwargs,
) -> LaunchSpec:
    return LaunchSpec((path, "-n", str(endpoint)), cwd)


def minetest_launch_pattern(
    path: str,
    cwd: str,
    game_settings: Mapping[str, object],
    endpoint: Endpoint,
    password: str,
    **kwargs,
) -> LaunchSpec:
    nickname = str(game_settings["nickname"])
    launch_cmd = (
        path,
        "--go",
        "--address",
        endpoint.host,
        "--port",
        str(endpoint.port),
        "--name",
        nickname,
    )
    if password:
        launch_cmd += ("--password", password)

    return LaunchSpec(
        launch_cmd,
        cwd,
        sensitive_values=frozenset({password}) if password else frozenset(),
    )


@dataclass(frozen=True, slots=True)
class _LaunchPattern:
    """One supported pattern and how its launch context is resolved.

    `executable_setting` names the setting holding the binary the pattern
    actually spawns. `uses_configured_workdir` is false for a pattern that
    spawns something other than the game itself: the configured game workdir
    is a guess about the game's own installation, so making it the cwd of, in
    Steam's case, the Steam client would turn a wrong guess into a launch
    failure even though that pattern never needs the game directory.
    """

    build: Callable[..., LaunchSpec]
    executable_setting: str = "path"
    uses_configured_workdir: bool = True


_LAUNCH_PATTERNS: Mapping[str, _LaunchPattern] = MappingProxyType({
    "steam": _LaunchPattern(
        steam_launch_pattern,
        "steam_path",
        uses_configured_workdir=False,
    ),
    "rigsofrods": _LaunchPattern(rigsofrods_launch_pattern),
    "quake": _LaunchPattern(quake_launch_pattern),
    "hl2": _LaunchPattern(hl2_launch_pattern),
    "openttd": _LaunchPattern(openttd_launch_pattern),
    "minetest": _LaunchPattern(minetest_launch_pattern),
})


def _require_pattern(launch_pattern: str) -> _LaunchPattern:
    """Return one supported pattern, or the one failure every caller reports.

    Both the setting lookup and the specification build need exactly this, so
    the rejection of an unknown name — and its message — lives here alone.
    """
    pattern = _LAUNCH_PATTERNS.get(launch_pattern)
    if pattern is None:
        raise ValueError(
            " ".join([LAUNCHER_MSG, i18n._("Launch pattern does not exist")])
        )
    return pattern


def executable_setting_name(launch_pattern: str) -> str:
    """Name of the setting holding the executable `launch_pattern` spawns.

    `build_launch_spec` and any caller that needs to know before building a
    full specification — such as `Core.start_game`'s own unconfigured-
    executable check, which must name the game before a worker ever starts —
    both resolve it through this one function, so the pattern-to-setting
    mapping lives in exactly one place.
    """
    return _require_pattern(launch_pattern).executable_setting


def build_launch_spec(
    game: str,
    launch_pattern: str,
    game_settings: Mapping[str, object],
    endpoint: Endpoint,
    password: str,
    steam_app_id: str | None = None,
    *,
    activation_directory: str,
) -> LaunchSpec:
    """Build one complete launch specification for the selected pattern.

    The activation directory is the caller's own working directory: launch
    context is resolved here, while the caller still owns it, so no worker has
    to observe a process-wide value that may have changed by then.
    """
    pattern = _require_pattern(launch_pattern)
    executable = str(game_settings[pattern.executable_setting])
    if not executable.strip():
        # The shipped defaults leave some games without an executable, so this
        # is ordinary unconfigured input rather than a programmer error: it
        # gets an actionable translated message the error dialog can display.
        raise ValueError(
            i18n._("Please configure the executable path for %(game)s.")
            % {"game": game}
        )

    configured_workdir = None
    if pattern.uses_configured_workdir:
        configured_workdir = game_settings.get("workdir")
    path, cwd = _resolve_launch_context(
        executable,
        str(configured_workdir) if configured_workdir else None,
        activation_directory,
    )
    return pattern.build(
        game=game,
        path=path,
        cwd=cwd,
        game_settings=game_settings,
        endpoint=endpoint,
        password=password,
        steam_app_id=steam_app_id,
    )


def do_launch(spec: LaunchSpec) -> Exception | None:
    """Start one detached client from a complete specification."""
    try:
        launch_message = i18n._("Launching '%(launch_cmd)s'") % {
            "launch_cmd": spec.redacted_command(),
        }
        helpers.debug_msg([LAUNCHER_MSG, launch_message])
        env_dict = dict(os.environ)
        # `Popen(cwd=…)` moves the child without rewriting PWD, so a client
        # that reads PWD instead of asking the kernel — Source engine ones
        # among them — would otherwise be told Obozrenie's own directory.
        # A pattern's own overrides still win over this default.
        env_dict["PWD"] = spec.cwd
        env_dict.update(spec.env_overrides)
        subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=env_dict,
            start_new_session=True,
        )
        return None
    except OSError as error:
        helpers.debug_msg([
            LAUNCHER_MSG,
            _sanitize_diagnostic(error, spec.sensitive_values),
        ])
        return error
