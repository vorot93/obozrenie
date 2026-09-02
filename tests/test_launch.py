import os
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

from obozrenie import core as core_module, helpers, launch
from obozrenie.core import Core
from obozrenie.models import Endpoint


class _SubclassedString(str):
    """A `str` subclass, to prove the cwd check demands the exact type."""


CORE_LAUNCH_CONFIG = {
    "g": {
        "name": "Game",
        "adapter": "fake",
        "launch_pattern": "minetest",
        "settings": ["path", "nickname", "steam_launch"],
    },
    "gs": {
        "name": "Game Steam",
        "adapter": "fake",
        "launch_pattern": "minetest",
        "steam_app_id": "4000",
        "settings": ["path", "steam_path", "nickname", "steam_launch"],
    },
}


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True
        self.target(*self.args)


class LaunchSpecTests(unittest.TestCase):
    def _launch_spec_type(self):
        self.assertTrue(
            hasattr(launch, "LaunchSpec"),
            "launch specifications must have a structured immutable type",
        )
        return launch.LaunchSpec

    def test_launch_spec_snapshots_mutable_inputs_and_is_read_only(self):
        launch_spec = self._launch_spec_type()
        argv = ["client", "secret"]
        env_overrides = {"TOKEN": "before"}
        sensitive_values = {"secret"}

        spec = launch_spec(argv, "/game", env_overrides, sensitive_values)
        argv[:] = ["other"]
        env_overrides["TOKEN"] = "after"
        sensitive_values.clear()

        self.assertEqual(spec.argv, ("client", "secret"))
        self.assertEqual(dict(spec.env_overrides), {"TOKEN": "before"})
        self.assertEqual(spec.sensitive_values, frozenset({"secret"}))
        with self.assertRaises(FrozenInstanceError):
            spec.cwd = "/other"
        with self.assertRaises(TypeError):
            spec.env_overrides["TOKEN"] = "changed"
        with self.assertRaises(TypeError):
            spec.argv[0] = "changed"
        with self.assertRaises(AttributeError):
            spec.sensitive_values.add("other")

    def test_launch_spec_requires_an_exact_absolute_working_directory(self):
        launch_spec = self._launch_spec_type()
        # Exact-type failures are `TypeError` and value failures `ValueError`,
        # the same split the records in `models` use.
        rejected = (
            ("", ValueError),
            ("relative/dir", ValueError),
            (None, TypeError),
            (b"/game", TypeError),
            (_SubclassedString("/game"), TypeError),
        )
        for cwd, error_type in rejected:
            with self.subTest(cwd=cwd):
                with self.assertRaises(error_type) as caught:
                    launch_spec(("client",), cwd)

                self.assertIs(type(caught.exception), error_type)

    def test_launch_pattern_table_cannot_be_rebound_at_runtime(self):
        # Dispatch is an explicit read-only mapping precisely so no import can
        # substitute a pattern; a plain dict would silently allow it.
        with self.assertRaises(TypeError):
            launch._LAUNCH_PATTERNS["quake"] = launch._LAUNCH_PATTERNS["steam"]

        self.assertIs(
            launch._LAUNCH_PATTERNS["quake"].build,
            launch.quake_launch_pattern,
        )

    def test_executable_setting_name_matches_each_pattern_and_rejects_unknown(
        self,
    ):
        # The single source `build_launch_spec` and `Core.start_game` both
        # resolve their executable setting through.
        self.assertEqual(launch.executable_setting_name("steam"), "steam_path")
        for pattern in ("rigsofrods", "quake", "hl2", "openttd", "minetest"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    launch.executable_setting_name(pattern), "path"
                )

        with self.assertRaises(ValueError):
            launch.executable_setting_name("no-such-pattern")

    def test_redacted_command_replaces_only_exact_sensitive_arguments(self):
        launch_spec = self._launch_spec_type()
        spec = launch_spec(
            ("client", "+password", "secret", "prefix-secret"),
            "/game",
            sensitive_values=frozenset({"secret"}),
        )

        self.assertEqual(
            spec.redacted_command(),
            "client +password *** prefix-secret",
        )

    def test_relative_executable_with_directory_resolves_once_from_activation_directory(self):
        spec = launch.build_launch_spec(
            "q3a",
            "quake",
            {"path": "clients/game", "workdir": ""},
            Endpoint("127.0.0.1", 27960),
            "",
            activation_directory="/start",
        )

        self.assertEqual(spec.argv[0], "/start/clients/game")
        self.assertEqual(spec.cwd, "/start/clients")
        # A child resolves a relative argv[0] against its own directory, so an
        # executable left relative here would be resolved a second time.
        self.assertEqual(
            os.path.join(spec.cwd, spec.argv[0]),
            "/start/clients/game",
        )

    def test_relative_executable_with_directory_resolves_from_effective_workdir(self):
        spec = launch.build_launch_spec(
            "q3a",
            "quake",
            {"path": "bin/game", "workdir": "games"},
            Endpoint("127.0.0.1", 27960),
            "",
            activation_directory="/start",
        )

        self.assertEqual(spec.argv[0], "/start/games/bin/game")
        self.assertEqual(spec.cwd, "/start/games")

    def test_bare_and_absolute_executables_preserve_lookup_policy(self):
        def quake_spec(path, workdir):
            return launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": path, "workdir": workdir},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="/start",
            )

        bare = quake_spec("quake", "")
        absolute = quake_spec("/games/quake/quake", "")
        configured = quake_spec("/games/quake/quake", "arena")

        # A bare command keeps its exact text so PATH lookup still applies.
        self.assertEqual(bare.argv[0], "quake")
        self.assertEqual(bare.cwd, "/start")
        self.assertEqual(absolute.argv[0], "/games/quake/quake")
        self.assertEqual(absolute.cwd, "/games/quake")
        self.assertEqual(configured.argv[0], "/games/quake/quake")
        self.assertEqual(configured.cwd, "/start/arena")

    @mock.patch.object(launch.helpers, "debug_msg")
    @mock.patch.object(launch.subprocess, "Popen")
    def test_steam_launch_ignores_the_configured_game_workdir(
        self,
        popen,
        _debug_msg,
    ):
        # The shipped defaults enable steam_launch together with a guessed
        # game workdir that need not exist; the spawned binary is the Steam
        # client, so a missing guess must not become the child's cwd.
        missing_workdir = "/nonexistent/steamapps/common/Team Fortress 2"
        self.assertFalse(os.path.exists(missing_workdir))

        bare = launch.build_launch_spec(
            "tf",
            "steam",
            {
                "path": f"{missing_workdir}/hl2_linux",
                "steam_path": "steam",
                "workdir": missing_workdir,
            },
            Endpoint("127.0.0.1", 27015),
            "",
            "440",
            activation_directory="/start",
        )
        installed = launch.build_launch_spec(
            "tf",
            "steam",
            {
                "path": f"{missing_workdir}/hl2_linux",
                "steam_path": "/opt/steam/steam",
                "workdir": missing_workdir,
            },
            Endpoint("127.0.0.1", 27015),
            "",
            "440",
            activation_directory="/start",
        )

        self.assertEqual(bare.argv[0], "steam")
        self.assertEqual(bare.cwd, "/start")
        self.assertEqual(installed.argv[0], "/opt/steam/steam")
        self.assertEqual(installed.cwd, "/opt/steam")

        self.assertIsNone(launch.do_launch(bare))
        self.assertEqual(popen.call_args.kwargs["cwd"], "/start")

    def test_hl2_child_pwd_equals_exact_cwd_for_every_path_form(self):
        cases = (
            ("hl2", "", "hl2", "/start"),
            ("/games/css/hl2_linux", "", "/games/css/hl2_linux", "/games/css"),
            ("bin/hl2_linux", "", "/start/bin/hl2_linux", "/start/bin"),
            (
                "bin/hl2_linux",
                "~/css",
                "/home/player/css/bin/hl2_linux",
                "/home/player/css",
            ),
        )

        for path, workdir, expected_argv, expected_cwd in cases:
            with self.subTest(path=path, workdir=workdir):
                with mock.patch.dict(
                    os.environ,
                    {"HOME": "/home/player"},
                    clear=True,
                ):
                    spec = launch.build_launch_spec(
                        "cstrike",
                        "hl2",
                        {"path": path, "workdir": workdir},
                        Endpoint("127.0.0.1", 27015),
                        "",
                        activation_directory="/start",
                    )

                with mock.patch.dict(
                    os.environ,
                    {"PWD": "/obozrenie"},
                    clear=True,
                ), mock.patch.object(
                    launch.subprocess, "Popen"
                ) as popen, mock.patch.object(launch.helpers, "debug_msg"):
                    self.assertIsNone(launch.do_launch(spec))

                self.assertEqual(spec.argv[0], expected_argv)
                self.assertEqual(spec.cwd, expected_cwd)
                self.assertTrue(os.path.isabs(spec.cwd))
                self.assertEqual(
                    popen.call_args.kwargs["env"]["PWD"],
                    spec.cwd,
                )

    def test_hl2_preserves_workdir_library_path_and_password_redaction(self):
        with mock.patch.dict(
            os.environ,
            {"HOME": "/home/player", "LD_LIBRARY_PATH": "/system/lib"},
            clear=True,
        ):
            spec = launch.build_launch_spec(
                "cstrike",
                "hl2",
                {"path": "/games/css/hl2_linux", "workdir": "~/css"},
                Endpoint("127.0.0.1", 27015),
                "secret",
                activation_directory="/start",
            )

        self.assertEqual(spec.cwd, "/home/player/css")
        self.assertEqual(
            spec.env_overrides,
            {"LD_LIBRARY_PATH": "./bin:/system/lib"},
        )
        self.assertEqual(
            spec.argv,
            (
                "/games/css/hl2_linux",
                "-game",
                "cstrike",
                "+connect",
                "127.0.0.1:27015",
                "+password",
                "secret",
            ),
        )
        self.assertNotIn("secret", spec.redacted_command())

    def test_quake_uses_expanded_configured_workdir(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/player"}, clear=True):
            spec = launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": "/games/quake/quake", "workdir": "~/quake"},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="/start",
            )

        self.assertEqual(spec.cwd, "/home/player/quake")

    def test_whitespace_only_settings_count_as_unset_not_as_configured(self):
        # The emptiness checks strip, so the resolution has to strip too, or
        # a padded setting reaches `Popen` as a directory or a command made
        # of spaces and fails there with nothing actionable to show.
        with mock.patch.dict(os.environ, {"HOME": "/home/player"}, clear=True):
            padded_workdir = launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": "quake", "workdir": "  "},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="/start",
            )
            padded_executable = launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": " /games/quake/quake ", "workdir": " ~/quake "},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="/start",
            )

        self.assertEqual(padded_workdir.cwd, "/start")
        self.assertEqual(padded_workdir.argv[0], "quake")
        self.assertEqual(padded_executable.argv[0], "/games/quake/quake")
        self.assertEqual(padded_executable.cwd, "/home/player/quake")

    def test_whitespace_only_executable_is_the_unconfigured_error(self):
        with self.assertRaises(ValueError) as caught:
            launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": "  ", "workdir": ""},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="/start",
            )

        self.assertEqual(
            str(caught.exception),
            "Please configure the executable path for q3a.",
        )

    def test_every_pattern_returns_tuple_argv_and_marks_password_sensitive(self):
        cases = (
            (
                "steam",
                "csgo",
                {"path": "csgo_linux", "steam_path": "steam", "workdir": ""},
                Endpoint("localhost", 27960),
                "12345",
                (
                    "steam",
                    "-applaunch",
                    "12345",
                    "+connect",
                    "localhost:27960",
                    "+password",
                    "abracadabra",
                ),
            ),
            (
                "rigsofrods",
                "rigsofrods",
                {"path": "ror"},
                Endpoint("localhost", 27960),
                None,
                ("ror",),
            ),
            (
                "quake",
                "q3a",
                {"path": "quake", "workdir": ""},
                Endpoint("localhost", 27960),
                None,
                (
                    "quake",
                    "+connect",
                    "localhost:27960",
                    "+password",
                    "abracadabra",
                ),
            ),
            (
                "hl2",
                "cstrike",
                {"path": "hl2", "workdir": ""},
                Endpoint("localhost", 27015),
                None,
                (
                    "hl2",
                    "-game",
                    "cstrike",
                    "+connect",
                    "localhost:27015",
                    "+password",
                    "abracadabra",
                ),
            ),
            (
                "openttd",
                "openttd",
                {"path": "openttd"},
                Endpoint("localhost", 3979),
                None,
                ("openttd", "-n", "localhost:3979"),
            ),
            (
                "minetest",
                "minetest",
                {"path": "minetest", "nickname": "player"},
                Endpoint("localhost", 30000),
                None,
                (
                    "minetest",
                    "--go",
                    "--address",
                    "localhost",
                    "--port",
                    "30000",
                    "--name",
                    "player",
                    "--password",
                    "abracadabra",
                ),
            ),
        )

        for (
            pattern,
            game,
            game_settings,
            endpoint,
            steam_app_id,
            expected_argv,
        ) in cases:
            with self.subTest(pattern=pattern):
                spec = launch.build_launch_spec(
                    game,
                    pattern,
                    game_settings,
                    endpoint,
                    "abracadabra",
                    steam_app_id,
                    activation_directory="/start",
                )

                self.assertIsInstance(spec.argv, tuple)
                self.assertEqual(spec.argv, expected_argv)
                self.assertEqual(spec.cwd, "/start")
                self.assertEqual(
                    spec.sensitive_values,
                    (
                        frozenset({"abracadabra"})
                        if "abracadabra" in expected_argv
                        else frozenset()
                    ),
                )

    def test_combined_endpoint_arguments_render_ipv6_canonically(self):
        endpoint = Endpoint("2001:db8::1", 27015)
        cases = (
            (
                "steam",
                "csgo",
                {"path": "csgo_linux", "steam_path": "steam", "workdir": ""},
                "12345",
            ),
            ("quake", "q3a", {"path": "quake", "workdir": ""}, None),
            ("hl2", "cstrike", {"path": "hl2", "workdir": ""}, None),
            ("openttd", "openttd", {"path": "openttd"}, None),
        )

        for pattern, game, game_settings, steam_app_id in cases:
            with self.subTest(pattern=pattern):
                spec = launch.build_launch_spec(
                    game,
                    pattern,
                    game_settings,
                    endpoint,
                    "",
                    steam_app_id,
                    activation_directory="/start",
                )

                self.assertIn("[2001:db8::1]:27015", spec.argv)

    def test_unknown_launch_pattern_is_rejected_without_reporting_details(self):
        with mock.patch.object(
            launch.helpers,
            "debug_msg",
        ) as debug_msg, mock.patch("builtins.print") as raw_print:
            with self.assertRaises(ValueError):
                launch.build_launch_spec(
                    "q3a",
                    "no-such-pattern",
                    {"path": "quake"},
                    Endpoint("127.0.0.1", 27960),
                    "pattern-secret",
                    activation_directory="/start",
                )

        debug_msg.assert_not_called()
        raw_print.assert_not_called()

    def test_relative_activation_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            launch.build_launch_spec(
                "q3a",
                "quake",
                {"path": "quake", "workdir": ""},
                Endpoint("127.0.0.1", 27960),
                "",
                activation_directory="start",
            )

    def test_unconfigured_executable_setting_names_the_game_to_configure(self):
        # The shipped defaults leave `gesource` with `path = ""`, so an
        # unconfigured game has to be rejected here rather than reaching the
        # executor with an empty argv[0] that only fails inside Popen.
        expected = launch.i18n._(
            "Please configure the executable path for %(game)s."
        ) % {"game": "gesource"}
        self.assertIn("gesource", expected)
        unconfigured = ("", " ", "\t\n", "   \t ")
        patterns = (("hl2", "path"), ("steam", "steam_path"))

        for executable in unconfigured:
            for pattern_name, setting in patterns:
                with self.subTest(executable=executable, pattern=pattern_name):
                    with self.assertRaises(ValueError) as caught:
                        launch.build_launch_spec(
                            "gesource",
                            pattern_name,
                            {
                                "path": "hl2",
                                "steam_path": "steam",
                                "workdir": "",
                                setting: executable,
                            },
                            Endpoint("127.0.0.1", 27015),
                            "executable-secret",
                            "12345",
                            activation_directory="/start",
                        )

                    self.assertEqual(str(caught.exception), expected)
                    self.assertNotIn(
                        "executable-secret",
                        str(caught.exception),
                    )

    @mock.patch.object(launch.helpers, "debug_msg")
    @mock.patch.object(launch.subprocess, "Popen")
    def test_executor_merges_environment_and_passes_declared_context(
        self,
        popen,
        debug_msg,
    ):
        launch_spec = self._launch_spec_type()
        spec = launch_spec(
            ("client", "+password", "secret"),
            "/game",
            {"X": "1"},
            frozenset({"secret"}),
        )

        with mock.patch.dict(os.environ, {"BASE": "present"}, clear=True):
            result = launch.do_launch(spec)

        self.assertIsNone(result)
        popen.assert_called_once_with(
            ("client", "+password", "secret"),
            cwd="/game",
            env={"BASE": "present", "X": "1", "PWD": "/game"},
            start_new_session=True,
        )
        self.assertNotIn("secret", str(debug_msg.call_args))
        self.assertIn("***", str(debug_msg.call_args))

    @mock.patch.object(launch.helpers, "debug_msg")
    @mock.patch.object(launch.subprocess, "Popen")
    def test_every_pattern_starts_its_child_with_pwd_naming_the_cwd(
        self,
        popen,
        _debug_msg,
    ):
        # `Popen(cwd=…)` does not rewrite PWD, so a child that reads it
        # instead of asking the kernel would otherwise see Obozrenie's own
        # directory whatever pattern started it.
        settings = {
            "path": "/games/client",
            "steam_path": "/games/steam",
            "workdir": "/games",
            "nickname": "player",
        }
        for pattern in launch._LAUNCH_PATTERNS:
            with self.subTest(pattern=pattern):
                popen.reset_mock()
                spec = launch.build_launch_spec(
                    "cstrike",
                    pattern,
                    settings,
                    Endpoint("127.0.0.1", 27015),
                    "",
                    steam_app_id="12345",
                    activation_directory="/start",
                )

                with mock.patch.dict(
                    os.environ,
                    {"PWD": "/obozrenie"},
                    clear=True,
                ):
                    self.assertIsNone(launch.do_launch(spec))

                self.assertEqual(
                    popen.call_args.kwargs["env"]["PWD"],
                    spec.cwd,
                )

    @mock.patch.object(launch.helpers, "debug_msg")
    @mock.patch.object(launch.subprocess, "Popen")
    def test_executor_uses_snapshotted_cwd_without_reading_process_cwd(
        self,
        popen,
        _debug_msg,
    ):
        spec = launch.build_launch_spec(
            "q3a",
            "quake",
            {"path": "quake", "workdir": ""},
            Endpoint("127.0.0.1", 27960),
            "",
            activation_directory="/start",
        )

        with mock.patch.object(launch.os, "getcwd") as getcwd:
            result = launch.do_launch(spec)

        self.assertIsNone(result)
        getcwd.assert_not_called()
        self.assertEqual(spec.cwd, "/start")
        self.assertEqual(popen.call_args.kwargs["cwd"], "/start")

    def test_specified_launch_returns_exact_executor_error(self):
        error = OSError("cannot launch")
        spec = launch.build_launch_spec(
            "q3a",
            "quake",
            {"path": "quake", "workdir": ""},
            Endpoint("127.0.0.1", 27960),
            "",
            activation_directory="/start",
        )

        with mock.patch.object(
            launch.subprocess,
            "Popen",
            side_effect=error,
        ), mock.patch.object(launch.helpers, "debug_msg"):
            result = launch.do_launch(spec)

        self.assertIs(result, error)

    def test_executor_failure_redacts_all_sensitive_values_and_preserves_error(self):
        error = OSError("failed with first-secret and second-secret")
        spec = launch.LaunchSpec(
            ("client", "first-secret", "second-secret"),
            "/game",
            sensitive_values=frozenset({"first-secret", "second-secret"}),
        )

        with mock.patch.object(
            launch.subprocess,
            "Popen",
            side_effect=error,
        ), mock.patch.object(launch.helpers, "debug_msg") as debug_msg:
            result = launch.do_launch(spec)

        self.assertIs(result, error)
        self.assertEqual(
            debug_msg.call_args_list[-1].args[0],
            [launch.LAUNCHER_MSG, "failed with *** and ***"],
        )


class CoreLaunchTests(unittest.TestCase):
    def setUp(self):
        self.core = self.new_core()
        self.core.game_table.set_game_setting("g", "path", "minetest")
        self.core.game_table.set_game_setting("g", "nickname", "player")
        self.core.game_table.set_game_setting("g", "steam_launch", False)
        self.threads = []

    def new_core(self):
        """Build a real Core over this module's launch configuration.

        Core initialises its lifecycle state in `__init__`, so a fixture that
        bypassed the constructor would break the moment launching consults it.
        """
        with (
            mock.patch.object(
                core_module.resources,
                "load_toml",
                return_value=CORE_LAUNCH_CONFIG,
            ),
            mock.patch.object(
                core_module.geoip, "find_database", return_value=None
            ),
            # Construction reports the absent database; these tests assert
            # their own diagnostics, so a passing run prints nothing.
            mock.patch.object(helpers, "debug_msg"),
        ):
            return Core()

    def make_thread(self, *args, **kwargs):
        thread = ImmediateThread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def test_launch_fixture_core_carries_its_lifecycle_state(self):
        # Launching shares Core with the query lifecycle, whose state exists
        # only when `Core.__init__` ran, so this fixture must construct one.
        self.assertIs(self.core.is_closing, False)

    @mock.patch.object(core_module.threading, "Thread")
    def test_invalid_endpoints_raise_before_thread_construction(self, thread):
        for endpoint in ("", "example.org"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self.core.start_game("g", endpoint, "")

        thread.assert_not_called()

    @mock.patch.object(core_module.threading, "Thread")
    def test_unconfigured_executable_raises_before_thread_construction(
        self,
        thread,
    ):
        expected = launch.i18n._(
            "Please configure the executable path for %(game)s."
        ) % {"game": "Game"}
        for executable in ("", " ", "\t"):
            with self.subTest(executable=executable):
                self.core.game_table.set_game_setting("g", "path", executable)

                with self.assertRaises(ValueError) as caught:
                    self.core.start_game("g", "example.org:30000", "")

                # The GTK layer renders a synchronous ValueError in its error
                # dialog, and it must name the game by the display name the
                # user recognizes ("Game"), not its internal id ("g").
                self.assertEqual(str(caught.exception), expected)

        thread.assert_not_called()

    @mock.patch.object(core_module.threading, "Thread")
    def test_unconfigured_steam_path_raises_before_thread_construction(
        self,
        thread,
    ):
        # "gs" carries a steam_app_id, so once steam_launch is set, the
        # resolved pattern is "steam" and the executable setting the
        # pre-check must consult is "steam_path", not "path".
        self.core.game_table.set_game_setting("gs", "steam_launch", True)
        expected = launch.i18n._(
            "Please configure the executable path for %(game)s."
        ) % {"game": "Game Steam"}
        for executable in ("", " ", "\t"):
            with self.subTest(executable=executable):
                self.core.game_table.set_game_setting(
                    "gs", "steam_path", executable
                )

                with self.assertRaises(ValueError) as caught:
                    self.core.start_game("gs", "example.org:30000", "")

                self.assertEqual(str(caught.exception), expected)

        thread.assert_not_called()

    def test_configured_steam_path_ignores_an_empty_path_setting(self):
        # Steam games launch through the Steam client, not the game's own
        # binary, so an empty "path" must not trip the pre-check once
        # "steam_path" is the setting actually in play.
        self.core.game_table.set_game_setting("gs", "steam_launch", True)
        self.core.game_table.set_game_setting("gs", "steam_path", "steam")
        self.core.game_table.set_game_setting("gs", "path", "")

        with mock.patch.object(
            core_module.threading,
            "Thread",
            side_effect=self.make_thread,
        ), mock.patch.object(
            launch,
            "do_launch",
            return_value=None,
        ):
            admitted = self.core.start_game("gs", "example.org:30000", "")

        self.assertIs(admitted, True)
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].started)

    def test_start_game_builds_complete_spec_before_thread_construction(self):
        events = []
        build_launch_spec = launch.build_launch_spec

        def record_build(*args, **kwargs):
            spec = build_launch_spec(*args, **kwargs)
            events.append(("build", kwargs["activation_directory"], spec))
            return spec

        def record_thread(*args, **kwargs):
            events.append(("thread", None, None))
            return self.make_thread(*args, **kwargs)

        def record_launch(spec):
            events.append(("launch", None, spec))
            return None

        with mock.patch.object(
            core_module.launch,
            "build_launch_spec",
            side_effect=record_build,
        ), mock.patch.object(
            core_module.threading,
            "Thread",
            side_effect=record_thread,
        ), mock.patch.object(
            core_module.launch,
            "do_launch",
            side_effect=record_launch,
        ):
            admitted = self.core.start_game("g", "example.org:30000", "")

        self.assertIs(admitted, True)
        self.assertEqual(
            [stage for stage, _, _ in events],
            ["build", "thread", "launch"],
        )
        build, _thread, launched = events
        # The activation directory belongs to the calling thread, so the whole
        # specification exists before a worker could observe anything else.
        self.assertEqual(build[1], os.getcwd())
        self.assertEqual(build[2].cwd, os.getcwd())
        self.assertEqual(build[2].argv[0], "minetest")
        self.assertIs(launched[2], build[2])

    def test_bracketed_ipv6_reaches_pattern_as_unbracketed_host_and_port(self):
        specs = []
        with mock.patch.object(
            core_module.threading,
            "Thread",
            side_effect=self.make_thread,
        ), mock.patch.object(
            launch,
            "do_launch",
            side_effect=lambda spec: specs.append(spec),
        ):
            admitted = self.core.start_game(
                "g",
                "[2001:db8::1]:30000",
                "",
            )

        self.assertEqual(
            specs[0].argv[2:7],
            ("--address", "2001:db8::1", "--port", "30000", "--name"),
        )
        self.assertIs(admitted, True)
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].daemon)
        self.assertTrue(self.threads[0].started)

    def test_returned_launch_exception_reaches_callback_once(self):
        error = OSError("client missing")
        callbacks = []
        with mock.patch.object(
            core_module.threading,
            "Thread",
            side_effect=self.make_thread,
        ), mock.patch.object(
            core_module.launch,
            "do_launch",
            return_value=error,
        ):
            admitted = self.core.start_game(
                "g",
                "example.org:30000",
                "",
                callbacks.append,
            )

        self.assertIs(admitted, True)
        self.assertEqual(len(callbacks), 1)
        self.assertIs(callbacks[0], error)

    def test_raised_launch_exception_reaches_callback_once(self):
        error = RuntimeError("executor failed")
        callbacks = []
        with mock.patch.object(
            core_module.threading,
            "Thread",
            side_effect=self.make_thread,
        ), mock.patch.object(
            core_module.launch,
            "do_launch",
            side_effect=error,
        ):
            admitted = self.core.start_game(
                "g",
                "example.org:30000",
                "",
                callbacks.append,
            )

        self.assertIs(admitted, True)
        self.assertEqual(len(callbacks), 1)
        self.assertIs(callbacks[0], error)


class LaunchConfigurationTests(unittest.TestCase):
    def test_garrys_mod_launch_uses_steam_app_4000(self):
        from obozrenie import resources

        game_config = resources.load_toml(
            "settings", "internal", "game_lists.toml"
        )
        spec = launch.build_launch_spec(
            "garrysmod",
            "steam",
            {"path": "hl2", "steam_path": "steam", "workdir": ""},
            Endpoint("127.0.0.1", 27015),
            "",
            str(game_config["garrysmod"]["steam_app_id"]),
            activation_directory="/start",
        )

        self.assertEqual(spec.argv[2], "4000")


if __name__ == "__main__":
    unittest.main()
