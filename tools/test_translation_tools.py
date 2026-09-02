import contextlib
import datetime
import gettext
import importlib.metadata
from io import BytesIO, StringIO
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import babel
from babel.messages import pofile

from tools import check_translations


DOMAIN = "me.vorotnikov.Obozrenie"
PROJECT_ROOT = check_translations.PROJECT_ROOT
CATALOG_PATHS = (
    PROJECT_ROOT / "obozrenie" / "data" / "locale" / f"{DOMAIN}.pot",
    PROJECT_ROOT
    / "obozrenie"
    / "data"
    / "locale"
    / "ru"
    / "LC_MESSAGES"
    / f"{DOMAIN}.po",
    PROJECT_ROOT
    / "obozrenie"
    / "data"
    / "locale"
    / "ru"
    / "LC_MESSAGES"
    / f"{DOMAIN}.mo",
)


class TranslationCheckerFormatTests(unittest.TestCase):
    def write_catalog(self, path, catalog):
        output = BytesIO()
        pofile.write_po(output, catalog)
        path.write_bytes(output.getvalue().rstrip(b"\r\n") + b"\n")

    def run_checker(self, template, russian, *, python_ids=None):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pot_path = directory / "messages.pot"
            po_path = directory / "messages.po"
            mo_path = directory / "messages.mo"
            self.write_catalog(pot_path, template)
            self.write_catalog(po_path, russian)
            mo_path.write_bytes(check_translations.compile_catalog(russian))

            stdout = StringIO()
            stderr = StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    check_translations,
                    "POT_PATH",
                    pot_path,
                ))
                stack.enter_context(mock.patch.object(
                    check_translations,
                    "PO_PATH",
                    po_path,
                ))
                stack.enter_context(mock.patch.object(
                    check_translations,
                    "MO_PATH",
                    mo_path,
                ))
                if python_ids is not None:
                    stack.enter_context(mock.patch.object(
                        check_translations,
                        "python_messages",
                        return_value=python_ids,
                    ))
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    status = check_translations.main()
            return status, stdout.getvalue(), stderr.getvalue()

    def test_rejects_mismatched_named_placeholder_in_current_translation(self):
        message_id = "Unable to launch the game: %(error)s"
        template = check_translations.read_catalog(check_translations.POT_PATH)
        russian = check_translations.read_catalog(
            check_translations.PO_PATH,
            locale="ru",
        )
        russian.get(message_id).string = (
            "Не удалось запустить игру: %(cause)s"
        )

        status, _stdout, stderr = self.run_checker(template, russian)

        self.assertEqual(status, 1)
        self.assertIn(repr(message_id), stderr)
        self.assertIn("unknown named placeholder 'cause'", stderr)

    def test_rejects_mismatched_named_placeholder_in_plural_form(self):
        message_id = (
            "%(count)d server",
            "%(count)d servers",
        )
        template = check_translations.read_catalog(check_translations.POT_PATH)
        russian = check_translations.read_catalog(
            check_translations.PO_PATH,
            locale="ru",
        )
        template.add(
            message_id,
            ("", ""),
            flags={"python-format"},
        )
        russian.add(
            message_id,
            (
                "%(count)d сервер",
                "%(total)d сервера",
                "%(count)d серверов",
            ),
            flags={"python-format"},
        )
        python_ids = check_translations.python_messages() | {message_id}

        status, _stdout, stderr = self.run_checker(
            template,
            russian,
            python_ids=python_ids,
        )

        self.assertEqual(status, 1)
        self.assertIn(repr(message_id), stderr)
        self.assertIn("unknown named placeholder 'total'", stderr)


class CatalogRegenerationTests(unittest.TestCase):
    def test_catalogs_use_exact_pinned_babel_producer(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            dev_dependencies = tomllib.load(project_file)["dependency-groups"][
                "dev"
            ]
        babel_dependencies = [
            dependency
            for dependency in dev_dependencies
            if re.match(
                r"babel(?=$|[\s\[<>=!~@;])",
                dependency,
                re.IGNORECASE,
            )
        ]
        self.assertEqual(len(babel_dependencies), 1)
        exact_pin = re.fullmatch(
            r"babel==([^,;*<>=!~\s]+)",
            babel_dependencies[0],
            re.IGNORECASE,
        )
        self.assertIsNotNone(
            exact_pin,
            "the Babel catalog producer must use one exact dev dependency pin",
        )
        pinned_version = exact_pin.group(1)
        self.assertEqual(babel.__version__, pinned_version)
        self.assertEqual(importlib.metadata.version("Babel"), pinned_version)

        expected_producer = f"Babel {pinned_version}"
        for path in CATALOG_PATHS[:2]:
            with self.subTest(path=path.name):
                header_block, separator, _messages = path.read_text(
                    encoding="utf-8"
                ).partition("\n\n")
                self.assertEqual(separator, "\n\n")
                generated_by = re.findall(
                    r'^"Generated-By: ([^"\\\r\n]+)\\n"$',
                    header_block,
                    re.MULTILINE,
                )
                self.assertEqual(generated_by, [expected_producer])
        with CATALOG_PATHS[2].open("rb") as mo_file:
            generated_by = gettext.GNUTranslations(mo_file).info()[
                "generated-by"
            ]
        self.assertEqual(generated_by.strip(), expected_producer)

    def run_update_compile_cycle(self, cwd, timezone):
        environment = os.environ.copy()
        environment["TZ"] = timezone
        for script in ("update_translations.sh", "compile_translations.sh"):
            subprocess.run(
                [str(PROJECT_ROOT / script)],
                cwd=cwd,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        return {path: path.read_bytes() for path in CATALOG_PATHS}

    def test_update_compile_cycle_is_byte_idempotent_across_timezones(self):
        original = {path: path.read_bytes() for path in CATALOG_PATHS}
        try:
            with tempfile.TemporaryDirectory() as external_cwd:
                utc = self.run_update_compile_cycle(external_cwd, "UTC")
                honolulu = self.run_update_compile_cycle(
                    external_cwd,
                    "Pacific/Honolulu",
                )
        finally:
            for path, data in original.items():
                path.write_bytes(data)

        for path in CATALOG_PATHS:
            with self.subTest(path=path.name, cycle="UTC"):
                self.assertEqual(utc[path], original[path])
            with self.subTest(path=path.name, cycle="Pacific/Honolulu"):
                self.assertEqual(honolulu[path], original[path])

    def test_generated_catalog_metadata_is_current_and_project_specific(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            metadata = tomllib.load(project_file)["tool"]["obozrenie"][
                "translations"
            ]
        self.assertEqual(
            {
                key: metadata[key]
                for key in (
                    "project",
                    "version",
                    "copyright-holder",
                    "copyright-years",
                    "msgid-bugs-address",
                    "last-translator",
                    "template-language-team",
                    "russian-language-team",
                )
            },
            {
                "project": "Obozrenie",
                "version": "1",
                "copyright-holder": "Artem Vorotnikov",
                "copyright-years": "2016-2026",
                "msgid-bugs-address": (
                    "https://github.com/obozrenie/obozrenie/issues"
                ),
                "last-translator": (
                    "Artem Vorotnikov <artem@vorotnikov.me>"
                ),
                "template-language-team": (
                    "Obozrenie translators <artem@vorotnikov.me>"
                ),
                "russian-language-team": (
                    "Russian <artem@vorotnikov.me>"
                ),
            },
        )
        for field in ("pot-creation-date", "po-revision-date"):
            with self.subTest(metadata_date=field):
                self.assertEqual(
                    metadata[field].utcoffset(),
                    datetime.timedelta(0),
                )
                self.assertGreaterEqual(
                    metadata[field],
                    datetime.datetime(
                        2026,
                        9,
                        1,
                        tzinfo=datetime.timezone.utc,
                    ),
                )

        template = check_translations.read_catalog(CATALOG_PATHS[0])
        russian = check_translations.read_catalog(CATALOG_PATHS[1], locale="ru")
        actual = {
            "template project": f"{template.project} {template.version}",
            "Russian project": f"{russian.project} {russian.version}",
            "template bugs": template.msgid_bugs_address,
            "Russian bugs": russian.msgid_bugs_address,
            "template creation": template.creation_date.strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian creation": russian.creation_date.strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian revision": russian.revision_date.strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian translator": russian.last_translator,
            "Russian team": russian.language_team,
        }
        self.assertEqual(actual, {
            "template project": "Obozrenie 1",
            "Russian project": "Obozrenie 1",
            "template bugs": "https://github.com/obozrenie/obozrenie/issues",
            "Russian bugs": "https://github.com/obozrenie/obozrenie/issues",
            "template creation": metadata["pot-creation-date"].strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian creation": metadata["pot-creation-date"].strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian revision": metadata["po-revision-date"].strftime(
                "%Y-%m-%d %H:%M%z"
            ),
            "Russian translator": "Artem Vorotnikov <artem@vorotnikov.me>",
            "Russian team": "Russian <artem@vorotnikov.me>",
        })

        catalog_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in CATALOG_PATHS[:2]
        )
        for invalid_metadata in (
            "PROJECT",
            "ORGANIZATION",
            "EMAIL@ADDRESS",
            "YEAR-MO-DA",
            "2016-02-23",
        ):
            with self.subTest(invalid_metadata=invalid_metadata):
                self.assertNotIn(invalid_metadata, catalog_text)
        self.assertEqual(
            catalog_text.count(
                "Copyright (C) 2016-2026 Artem Vorotnikov"
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
