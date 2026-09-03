import contextlib
import io
import os
import stat
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import filelock
import tomli_w

from obozrenie import diagnostics, game_table, helpers, resources
from obozrenie import settings as settings_module


def toml_bytes(data):
    output = io.BytesIO()
    tomli_w.dump(data, output)
    return output.getvalue()


def toml_resource_loader(entries):
    def load_resource(*parts):
        with io.BytesIO(entries[tuple(parts)]) as table_file:
            return tomllib.load(table_file)

    return load_resource


class SettingsPersistenceTests(unittest.TestCase):
    DEFAULT_COMMON = {
        "common": {
            "selected-game-connect": "",
            "selected-game-browser": "game",
            "server-host": "",
            "game-listview": True,
        },
    }
    LOADED_COMMON = {
        "common": {
            "selected-game-connect": "game",
            "selected-game-browser": "game",
            "server-host": "",
            "game-listview": True,
        },
    }
    DEFAULT_GAMES = {
        "game": {
            "path": "default-game",
            "master_uri": ["master://default.example:27950"],
        },
    }

    @staticmethod
    def _write_toml(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as table_file:
            tomli_w.dump(data, table_file)

    def _make_settings(
        self,
        root,
        *,
        default_common_bytes=None,
        default_game_bytes=None,
    ):
        root = Path(root)
        if default_common_bytes is None:
            default_common_bytes = toml_bytes(self.DEFAULT_COMMON)
        if default_game_bytes is None:
            default_game_bytes = toml_bytes(self.DEFAULT_GAMES)
        load_resource = toml_resource_loader({
            ("settings", "defaults", "settings.toml"): default_common_bytes,
            ("settings", "defaults", "games.toml"): default_game_bytes,
        })

        table = game_table.GameTable({
            "game": {
                "name": "Game",
                "adapter": "qstat",
                "launch_pattern": "quake",
                "settings": ["path", "master_uri"],
            },
        })
        settings = settings_module.Settings(
            table,
            str(root / "profile"),
            resource_loader=load_resource,
        )
        return settings

    def test_save_table_installs_replacement_with_exact_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_bytes(b'[old]\nvalue = "preserved only on failure"\n')
            os.chmod(path, 0o666)
            original_inode = path.stat().st_ino

            previous_umask = os.umask(0o022)
            try:
                helpers.save_table(path, {"new": {"value": "installed"}})
            finally:
                os.umask(previous_umask)

            self.assertEqual(
                tomllib.loads(path.read_text(encoding="utf-8")),
                {"new": {"value": "installed"}},
            )
            self.assertNotEqual(path.stat().st_ino, original_inode)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual({entry.name for entry in path.parent.iterdir()}, {
                path.name,
            })

    def test_save_table_never_changes_the_mode_of_a_live_pathname(self):
        # The sibling is already `fchmod`ded through its own descriptor, so a
        # `chmod` on the installed name could only act on a file another
        # process put there in the same instant — following a symlink it left
        # behind, at that.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            previous_umask = os.umask(0o022)
            try:
                with mock.patch.object(
                    helpers.os,
                    "chmod",
                    side_effect=AssertionError(
                        "save_table changed the mode of a live pathname"
                    ),
                ):
                    helpers.save_table(path, {"table": {"value": 1}})
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_save_table_accepts_filename_in_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                helpers.save_table("settings.toml", {"table": {"value": 1}})
                with open("settings.toml", "rb") as table_file:
                    self.assertEqual(
                        tomllib.load(table_file),
                        {"table": {"value": 1}},
                    )
                self.assertEqual(
                    stat.S_IMODE(os.stat("settings.toml").st_mode),
                    0o600,
                )
            finally:
                os.chdir(previous_directory)

    def test_save_table_rolls_back_partial_serialization_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            original = b'[original]\nvalue = "keep me"\n'
            path.write_bytes(original)
            os.chmod(path, 0o640)
            original_stat = path.stat()
            failure = RuntimeError("serializer failed")

            def fail_after_partial_write(data, table_file):
                table_file.write(b"[partial")
                raise failure

            with mock.patch.object(
                helpers.tomli_w,
                "dump",
                side_effect=fail_after_partial_write,
            ):
                with self.assertRaises(RuntimeError) as caught:
                    helpers.save_table(path, {"replacement": {"value": 2}})

            self.assertIs(caught.exception, failure)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_ino, original_stat.st_ino)
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                stat.S_IMODE(original_stat.st_mode),
            )
            self.assertEqual({entry.name for entry in path.parent.iterdir()}, {
                path.name,
            })

    def test_strict_toml_loading_keeps_malformed_input_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('[common]\nsecret = "unterminated\n', encoding="utf-8")

            with self.assertRaises(tomllib.TOMLDecodeError):
                with open(path, "rb") as table_file:
                    tomllib.load(table_file)

            self.assertTrue(path.exists())
            self.assertEqual(list(path.parent.glob(f"{path.name}.invalid-*")), [])

    def test_load_user_table_treats_a_missing_file_as_normal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.toml"

            self.assertEqual(helpers.load_user_table(path), ({}, None))
            self.assertEqual(list(path.parent.iterdir()), [])

    def test_load_user_table_backs_up_exact_snapshot_without_mutating_live_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            target = path.with_name("malformed-target.toml")
            payload = b'[common]\nsecret-recovery-payload = "unterminated\n'
            target.write_bytes(payload)
            os.chmod(target, 0o644)
            path.symlink_to(target.name)
            before = path.stat()

            table, warning = helpers.load_user_table(path)

            self.assertEqual(table, {})
            self.assertIsInstance(warning, str)
            self.assertTrue(warning)
            self.assertNotIn("secret-recovery-payload", warning)

            after = path.stat()
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), target.name)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(
                (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)),
                (before.st_dev, before.st_ino, 0o644),
            )

            backups = list(path.parent.glob(f"{path.name}.invalid-*"))
            self.assertEqual(len(backups), 1)
            self.assertFalse(backups[0].is_symlink())
            self.assertEqual(backups[0].read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name, target.name, backups[0].name},
            )

    def test_load_user_table_recovers_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nvalue = "invalid-\xff"\n'
            path.write_bytes(payload)

            try:
                table, warning = helpers.load_user_table(path)
            except UnicodeDecodeError as error:
                self.fail(f"invalid UTF-8 user TOML must recover: {error}")

            self.assertEqual(table, {})
            self.assertIsInstance(warning, str)
            self.assertEqual(path.read_bytes(), payload)
            backups = list(path.parent.glob(f"{path.name}.invalid-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), payload)

    def test_recovered_legacy_password_backup_has_exact_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            password = "legacy-password"
            payload = (
                f'[common]\nserver-pass = "{password}"\n'
                'broken = "unterminated\n'
            ).encode()
            path.write_bytes(payload)
            os.chmod(path, 0o644)

            table, warning = helpers.load_user_table(path)

            self.assertEqual(table, {})
            self.assertNotIn(password, warning)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            backups = list(path.parent.glob(f"{path.name}.invalid-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_load_user_table_reloads_valid_replacement_installed_during_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            malformed = b'[common]\nsecret-replaced-payload = "unterminated\n'
            replacement = b'[common]\nserver-host = "replacement.example"\n'
            path.write_bytes(malformed)
            archive_snapshot = helpers._write_invalid_backup
            archived = []

            def install_replacement_after_archiving(source, snapshot_payload):
                backup_path = archive_snapshot(source, snapshot_payload)
                archived.append(Path(backup_path))
                spare = path.with_name("replacement.toml")
                spare.write_bytes(replacement)
                os.replace(spare, path)
                return backup_path

            with mock.patch.object(
                helpers,
                "_write_invalid_backup",
                side_effect=install_replacement_after_archiving,
            ):
                table, warning = helpers.load_user_table(path)

            self.assertEqual(
                table,
                {"common": {"server-host": "replacement.example"}},
            )
            self.assertIsNone(warning)
            self.assertEqual(path.read_bytes(), replacement)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), malformed)
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name, archived[0].name},
            )

    def test_load_user_table_stops_after_three_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payloads = [
                f'[common]\nsecret-churn-payload-{index} = "unterminated\n'.encode()
                for index in range(4)
            ]
            path.write_bytes(payloads[0])
            read_snapshot = helpers._read_file_snapshot
            reads = []

            def replace_source_after_reading(source):
                snapshot = read_snapshot(source)
                reads.append(snapshot)
                spare = path.with_name(f"churn-{len(reads)}.toml")
                spare.write_bytes(payloads[len(reads)])
                os.replace(spare, path)
                return snapshot

            with mock.patch.object(
                helpers,
                "_read_file_snapshot",
                side_effect=replace_source_after_reading,
            ):
                with self.assertRaises(RuntimeError) as caught:
                    helpers.load_user_table(path)

            self.assertEqual(
                str(caught.exception),
                "User settings changed during bounded recovery.",
            )
            self.assertEqual(len(reads), 3)
            self.assertEqual(path.read_bytes(), payloads[3])
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name},
            )

    def test_load_user_table_declares_mkstemp_exhaustion_without_touching_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nsecret-exhaustion-payload = "unterminated\n'
            path.write_bytes(payload)
            os.chmod(path, 0o640)
            before = path.stat()
            failure = FileExistsError("No usable temporary file name found")

            with mock.patch.object(
                helpers.tempfile,
                "mkstemp",
                side_effect=failure,
            ):
                with self.assertRaises(helpers.SettingsBackupError) as caught:
                    helpers.load_user_table(path)

            # The archive path declares its failure exactly as the read and
            # save paths do: the original, which names the directory, is
            # suppressed rather than chained.
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            after = path.stat()
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(
                (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode)),
                (before.st_dev, before.st_ino, 0o640),
            )
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name},
            )

    def test_load_user_table_removes_partial_owned_backup_on_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nsecret-partial-payload = "unterminated\n'
            path.write_bytes(payload)
            before = path.stat()
            failure = OSError("no space left on device")
            open_descriptor = helpers.os.fdopen
            partial_writes = []

            class PartialWriteFile:
                """Commit half a payload, then fail like a full filesystem."""

                def __init__(self, wrapped):
                    self._wrapped = wrapped

                def __enter__(self):
                    return self

                def __exit__(self, *exception):
                    self._wrapped.close()
                    return False

                def write(self, data):
                    partial_writes.append(
                        self._wrapped.write(data[:len(data) // 2])
                    )
                    self._wrapped.flush()
                    raise failure

            def open_partial_writer(descriptor, mode):
                return PartialWriteFile(open_descriptor(descriptor, mode))

            with mock.patch.object(
                helpers.os,
                "fdopen",
                side_effect=open_partial_writer,
            ):
                with self.assertRaises(helpers.SettingsBackupError) as caught:
                    helpers.load_user_table(path)

            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(partial_writes, [len(payload) // 2])
            after = path.stat()
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name},
            )

    def test_load_user_table_keeps_a_durable_backup_when_close_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nsecret-close-payload = "unterminated\n'
            path.write_bytes(payload)
            failure = OSError("close failed after the payload was durable")
            open_descriptor = helpers.os.fdopen

            class FailingCloseFile:
                """Write, flush and fsync normally, then fail in `close()`."""

                def __init__(self, wrapped):
                    self._wrapped = wrapped

                def __enter__(self):
                    return self

                def __exit__(self, *exception):
                    self._wrapped.close()
                    raise failure

                def write(self, data):
                    return self._wrapped.write(data)

                def flush(self):
                    self._wrapped.flush()

                def fileno(self):
                    return self._wrapped.fileno()

            def open_failing_closer(descriptor, mode):
                return FailingCloseFile(open_descriptor(descriptor, mode))

            with mock.patch.object(
                helpers.os,
                "fdopen",
                side_effect=open_failing_closer,
            ):
                with self.assertRaises(helpers.SettingsBackupError) as caught:
                    helpers.load_user_table(path)

            self.assertIsNone(caught.exception.__cause__)
            # The payload was written, flushed and fsynced before the close
            # failed, so the archive is already durable and must survive.
            backups = list(path.parent.glob(f"{path.name}.invalid-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), payload)
            self.assertEqual(path.read_bytes(), payload)

    def test_load_user_table_declares_a_read_failure_without_the_path(self):
        # The handlers no longer re-raise, so this diagnostic is all a user
        # with an unreadable profile is shown: it has to name the failure
        # while keeping the path and the errno text `PermissionError` carries
        # out of the log.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('[common]\nvalue = 1\n', encoding="utf-8")
            os.chmod(path, 0o000)
            try:
                with open(path, "rb"):
                    pass
            except PermissionError:
                pass
            else:
                self.skipTest("this user can read a 0000 settings file")

            with self.assertRaises(helpers.SettingsReadError) as caught:
                helpers.load_user_table(path)

        error = caught.exception
        # Existing callers and tests treat these as the OSError family they
        # have always been.
        self.assertIsInstance(error, OSError)
        self.assertEqual(
            diagnostics.diagnostic_for_exception(error),
            diagnostics.SafeDiagnostic("Settings file could not be read."),
        )
        self.assertEqual(str(error), "Settings file could not be read.")
        # The original names the file and carries `errno`/`strerror`, so it
        # is suppressed rather than chained into a printable traceback.
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn(str(path), str(error))

    def test_load_user_table_declares_a_backup_failure_without_the_path(self):
        # A malformed profile whose archive cannot be written is the one
        # settings filesystem failure that used to reach the user as a bare
        # type name; it is declared like the read and save failures are.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nsecret-backup-payload = "unterminated\n'
            path.write_bytes(payload)
            failure = PermissionError(13, "Permission denied", str(path))

            with mock.patch.object(
                helpers.tempfile, "mkstemp", side_effect=failure
            ):
                with self.assertRaises(helpers.SettingsBackupError) as caught:
                    helpers.load_user_table(path)

            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(
                {entry.name for entry in path.parent.iterdir()},
                {path.name},
            )

        error = caught.exception
        self.assertIsInstance(error, OSError)
        self.assertEqual(
            diagnostics.diagnostic_for_exception(error),
            diagnostics.SafeDiagnostic("Settings backup could not be written."),
        )
        self.assertEqual(str(error), "Settings backup could not be written.")
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertNotIn(str(path), str(error))

    def test_backup_rollback_failure_never_masks_the_declared_error(self):
        # A partial archive whose removal fails as well — a permission change
        # on the profile directory in the meantime — still reports the write
        # failure it was rolling back from, never the rollback's own.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            payload = b'[common]\nsecret-rollback-payload = "unterminated\n'
            path.write_bytes(payload)
            write_failure = OSError(28, "No space left on device")
            rollback_failure = PermissionError(13, "Permission denied")

            with (
                mock.patch.object(
                    helpers.os, "fsync", side_effect=write_failure
                ),
                mock.patch.object(
                    helpers.os, "unlink", side_effect=rollback_failure
                ) as unlink,
                self.assertRaises(helpers.SettingsBackupError) as caught,
            ):
                helpers.load_user_table(path)

            unlink.assert_called_once()
            self.assertIsNone(caught.exception.__cause__)
            # The declaration suppresses the write failure it was rolling
            # back from — not the rollback's own, which a rollback that did
            # not tolerate it would have put in its place.
            self.assertIs(caught.exception.__context__, write_failure)
            self.assertEqual(path.read_bytes(), payload)

    def test_save_table_rollback_failure_never_masks_the_declared_error(self):
        # A permission change on the profile directory between the temporary
        # write and the rollback makes the unlink fail as well; the caller
        # still receives the declared write failure, not the rollback's raw
        # PermissionError.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            write_failure = OSError(28, "No space left on device")
            rollback_failure = PermissionError(13, "Permission denied")

            with (
                mock.patch.object(
                    helpers.os, "fsync", side_effect=write_failure
                ),
                mock.patch.object(
                    helpers.os, "unlink", side_effect=rollback_failure
                ) as unlink,
                self.assertRaises(helpers.SettingsWriteError) as caught,
            ):
                helpers.save_table(path, {"table": {"value": 1}})

            unlink.assert_called_once()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIs(caught.exception.__context__, write_failure)

    def test_save_table_declares_a_write_failure_without_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            failure = OSError(28, "No space left on device")

            with mock.patch.object(
                helpers.os, "fsync", side_effect=failure
            ):
                with self.assertRaises(helpers.SettingsWriteError) as caught:
                    helpers.save_table(path, {"table": {"value": 1}})

            # The rollback the declaration wraps is unchanged: nothing was
            # installed and no temporary sibling was left behind.
            self.assertEqual(
                {entry.name for entry in Path(directory).iterdir()},
                set(),
            )

        error = caught.exception
        self.assertIsInstance(error, OSError)
        self.assertEqual(
            diagnostics.diagnostic_for_exception(error),
            diagnostics.SafeDiagnostic("Settings file could not be written."),
        )
        self.assertEqual(str(error), "Settings file could not be written.")
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_load_user_table_does_not_treat_an_unreadable_path_as_missing(self):
        # Only a genuinely absent file is normal input; anything else the open
        # rejects is the declared read failure, not an empty table.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(helpers.SettingsReadError):
                helpers.load_user_table(directory)

    def test_settings_load_missing_user_files_uses_defaults_without_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)

            warnings = settings.load()

            self.assertEqual(warnings, [])
            self.assertEqual(settings.settings_table, self.LOADED_COMMON)
            self.assertEqual(
                settings.game_table.get_game_settings("game"),
                self.DEFAULT_GAMES["game"],
            )

    def test_settings_load_merges_valid_user_tables_and_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            self._write_toml(
                settings.user_common_settings_path,
                {"common": {"server-host": "game.example", "game-listview": False}},
            )
            self._write_toml(
                settings.user_game_settings_path,
                {"game": {"path": "user-game"}},
            )

            warnings = settings.load()

            self.assertFalse(settings._profile_lock.is_locked)
            self.assertEqual(warnings, [])
            self.assertEqual(settings.settings_table, {
                "common": {
                    "selected-game-connect": "game",
                    "selected-game-browser": "game",
                    "server-host": "game.example",
                    "game-listview": False,
                },
            })
            self.assertEqual(
                settings.game_table.get_game_settings("game"),
                {
                    "path": "user-game",
                    "master_uri": ["master://default.example:27950"],
                },
            )

    def test_settings_load_recovers_each_malformed_user_table(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            common_path = Path(settings.user_common_settings_path)
            game_path = Path(settings.user_game_settings_path)
            common_path.parent.mkdir(parents=True, exist_ok=True)
            common_payload = b'[common]\nsecret-common-payload = "unterminated\n'
            game_payload = b'[game]\nsecret-game-payload = "unterminated\n'
            common_path.write_bytes(common_payload)
            game_path.write_bytes(game_payload)

            warnings = settings.load()

            self.assertEqual(len(warnings), 2)
            for warning in warnings:
                self.assertNotIn("secret-common-payload", warning)
                self.assertNotIn("secret-game-payload", warning)
            self.assertEqual(settings.settings_table, self.LOADED_COMMON)
            self.assertEqual(
                settings.game_table.get_game_settings("game"),
                self.DEFAULT_GAMES["game"],
            )
            for path, payload in (
                (common_path, common_payload),
                (game_path, game_payload),
            ):
                self.assertEqual(path.read_bytes(), payload)
                backups = list(path.parent.glob(f"{path.name}.invalid-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), payload)

    def test_settings_load_keeps_malformed_bundled_toml_fatal(self):
        for table_name in ("common", "game"):
            with self.subTest(table=table_name), tempfile.TemporaryDirectory() as directory:
                payload = (
                    f'[{table_name}]\n'
                    'secret-bundled-payload = "unterminated\n'
                ).encode()
                settings = self._make_settings(
                    directory,
                    default_common_bytes=(
                        payload if table_name == "common" else None
                    ),
                    default_game_bytes=(
                        payload if table_name == "game" else None
                    ),
                )

                with self.assertRaises(tomllib.TOMLDecodeError) as caught:
                    settings.load()

                self.assertNotIn("secret-bundled-payload", str(caught.exception))
                self.assertEqual(
                    list(Path(directory).rglob("*.invalid-*")),
                    [],
                )

    def test_settings_load_keeps_invalid_utf8_bundled_toml_fatal(self):
        for table_name in ("common", "game"):
            with self.subTest(table=table_name), tempfile.TemporaryDirectory() as directory:
                payload = f'[{table_name}]\nvalue = "'.encode() + b'\xff"\n'
                settings = self._make_settings(
                    directory,
                    default_common_bytes=(
                        payload if table_name == "common" else None
                    ),
                    default_game_bytes=(
                        payload if table_name == "game" else None
                    ),
                )

                with self.assertRaises(UnicodeDecodeError):
                    settings.load()

                self.assertEqual(
                    list(Path(directory).rglob("*.invalid-*")),
                    [],
                )

    def test_settings_load_and_save_serialize_across_instances_on_profile_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            loader = self._make_settings(directory)
            saver = self._make_settings(directory)
            self.assertEqual(
                loader._profile_lock.lock_file,
                saver._profile_lock.lock_file,
            )
            # Saving whitelists the loaded table, so the saver needs one
            # completed load before it can write anything.
            saver.load()

            events = []
            failures = []
            loader_holds = threading.Event()
            saver_acquiring = threading.Event()
            release_loader = threading.Event()
            released = []

            def instrument(settings, label, entering=None, holding=None):
                hold_profile_lock = settings._hold_profile_lock

                @contextlib.contextmanager
                def instrumented_hold():
                    if entering is not None:
                        entering()
                    with hold_profile_lock():
                        events.append(f"{label}-enter")
                        try:
                            if holding is not None:
                                holding()
                            yield
                        finally:
                            events.append(f"{label}-exit")

                settings._hold_profile_lock = instrumented_hold

            def hold_until_released():
                loader_holds.set()
                released.append(release_loader.wait(1))

            def run(action):
                try:
                    action()
                except BaseException as error:
                    failures.append(error)

            instrument(loader, "load", holding=hold_until_released)
            instrument(saver, "save", entering=saver_acquiring.set)

            load_thread = threading.Thread(
                target=run, args=(loader.load,), daemon=True)
            save_thread = threading.Thread(
                target=run, args=(saver.save,), daemon=True)
            try:
                load_thread.start()
                self.assertTrue(loader_holds.wait(1))
                save_thread.start()
                self.assertTrue(saver_acquiring.wait(1))
            finally:
                release_loader.set()
            load_thread.join(1)
            save_thread.join(1)

            self.assertFalse(load_thread.is_alive())
            self.assertFalse(save_thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(released, [True])
            self.assertEqual(
                events,
                ["load-enter", "load-exit", "save-enter", "save-exit"],
            )

    def test_profile_lock_uses_one_path_and_five_second_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            profile_lock = settings._profile_lock

            self.assertEqual(
                profile_lock.lock_file,
                str(Path(directory) / "profile.lock"),
            )
            self.assertEqual(profile_lock.timeout, 5.0)
            self.assertEqual(profile_lock.mode, 0o600)

            hold_profile_lock = settings._hold_profile_lock
            held = []

            @contextlib.contextmanager
            def recording_hold():
                held.append(settings._profile_lock)
                with hold_profile_lock():
                    yield

            settings._hold_profile_lock = recording_hold
            settings.load()
            settings.save()

            self.assertEqual(held, [profile_lock, profile_lock])
            self.assertEqual(
                stat.S_IMODE(os.stat(profile_lock.lock_file).st_mode),
                0o600,
            )

    def test_profile_lock_timeout_becomes_a_sanitized_settings_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            # Saving whitelists the loaded table, so both calls below need one
            # completed load before the lock is contended.
            settings.load()
            settings._profile_lock.timeout = 0
            blocker = filelock.FileLock(
                settings._profile_lock.lock_file,
                timeout=0,
                mode=0o600,
            )

            blocker.acquire()
            try:
                with self.assertRaises(settings_module.SettingsLockError) as caught:
                    settings.load()
                with self.assertRaises(settings_module.SettingsLockError):
                    settings.save()
            finally:
                blocker.release()

            self.assertEqual(
                caught.exception.diagnostic,
                diagnostics.SafeDiagnostic(
                    "Another process holds the settings profile lock."
                ),
            )
            # `filelock`'s own timeout message names the lock file, so the
            # cause is suppressed and an uncaught traceback stays free of the
            # profile path.
            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(
                diagnostics.diagnostic_for_exception(caught.exception),
                caught.exception.diagnostic,
            )

    def test_packaged_common_defaults_match_the_declared_common_options(self):
        packaged = resources.load_toml("settings", "defaults", "settings.toml")
        declared = settings_module.COMMON_OPTIONS

        self.assertEqual(set(packaged), set(declared))
        for group, options in declared.items():
            self.assertEqual(set(packaged[group]), set(options))
        self.assertNotIn("nickname", packaged["common"])


class SettingsNormalizationTests(unittest.TestCase):
    LEGACY_DEFAULT_COMMON = {
        "common": {
            "selected-game-connect": "",
            "selected-game-browser": "",
            "server-host": "",
            "server-pass": "",
            "game-listview": True,
        },
    }
    GAME_CONFIG = {
        "z-alpha": {
            "name": "Alpha",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": [],
        },
        "y-alpha": {
            "name": "alpha",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": [],
        },
        "a-zulu": {
            "name": "Zulu",
            "adapter": "qstat",
            "launch_pattern": "quake",
            "settings": [],
        },
    }

    @staticmethod
    def _write_toml(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as table_file:
            tomli_w.dump(data, table_file)

    def _make_settings(self, root, game_config=None, default_common=None):
        root = Path(root)
        if game_config is None:
            game_config = self.GAME_CONFIG
        if default_common is None:
            default_common = self.LEGACY_DEFAULT_COMMON

        load_resource = toml_resource_loader({
            ("settings", "defaults", "settings.toml"): toml_bytes(
                default_common
            ),
            ("settings", "defaults", "games.toml"): toml_bytes({
                game_id: {} for game_id in game_config
            }),
        })

        settings = settings_module.Settings(
            game_table.GameTable(game_config),
            str(root / "profile"),
            resource_loader=load_resource,
        )
        return settings

    def test_legacy_password_and_unknown_keys_are_omitted_on_save(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            self._write_toml(
                settings.user_common_settings_path,
                {
                    "common": {
                        "selected-game-connect": "z-alpha",
                        "selected-game-browser": "z-alpha",
                        "server-host": "example.org:27960",
                        "server-pass": "secret",
                        "future-option": "ignored",
                    },
                },
            )

            settings.load()
            settings.save()

            expected = {
                "common": {
                    "selected-game-connect": "z-alpha",
                    "selected-game-browser": "z-alpha",
                    "server-host": "example.org:27960",
                    "game-listview": True,
                },
            }
            self.assertEqual(settings.settings_table, expected)
            with open(settings.user_common_settings_path, "rb") as table_file:
                self.assertEqual(tomllib.load(table_file), expected)

    def test_save_whitelists_live_table_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(directory)
            settings.load()
            settings.settings_table["common"]["server-pass"] = "secret"
            settings.settings_table["common"]["future-option"] = "ignored"

            settings.save()

            self.assertEqual(settings.settings_table, {
                "common": {
                    "selected-game-connect": "y-alpha",
                    "selected-game-browser": "y-alpha",
                    "server-host": "",
                    "game-listview": True,
                    "server-pass": "secret",
                    "future-option": "ignored",
                },
            })
            with open(settings.user_common_settings_path, "rb") as table_file:
                self.assertEqual(
                    tomllib.load(table_file),
                    {
                        "common": {
                            "selected-game-connect": "y-alpha",
                            "selected-game-browser": "y-alpha",
                            "server-host": "",
                            "game-listview": True,
                        },
                    },
                )

    def test_missing_empty_and_unknown_selectors_normalize_on_load(self):
        user_values = (
            ("missing", {}),
            (
                "empty",
                {
                    "selected-game-connect": "",
                    "selected-game-browser": "",
                },
            ),
            (
                "unknown",
                {
                    "selected-game-connect": "retired-connect",
                    "selected-game-browser": "retired-browser",
                },
            ),
        )
        for label, common_values in user_values:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                settings = self._make_settings(directory)
                if common_values:
                    self._write_toml(
                        settings.user_common_settings_path,
                        {"common": common_values},
                    )
                settings.load()

                self.assertEqual(
                    settings.settings_table["common"]["selected-game-browser"],
                    "y-alpha",
                )
                self.assertEqual(
                    settings.settings_table["common"]["selected-game-connect"],
                    "y-alpha",
                )

    def test_empty_game_set_normalizes_both_selectors_to_empty(self):
        default_common = {
            "common": {
                "selected-game-connect": "retired-connect",
                "selected-game-browser": "retired-browser",
                "server-host": "",
                "server-pass": "",
                "game-listview": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            settings = self._make_settings(
                directory,
                game_config={},
                default_common=default_common,
            )

            settings.load()

            self.assertEqual(
                settings.settings_table["common"]["selected-game-browser"],
                "",
            )
            self.assertEqual(
                settings.settings_table["common"]["selected-game-connect"],
                "",
            )

    def test_fresh_profile_uses_an_empty_server_host(self):
        from obozrenie import resources

        game_config = resources.load_toml(
            "settings", "internal", "game_lists.toml"
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_module.Settings(
                game_table.GameTable(game_config),
                directory,
            )

            settings.load()

            self.assertEqual(
                settings.settings_table["common"]["server-host"],
                "",
            )


if __name__ == "__main__":
    unittest.main()
