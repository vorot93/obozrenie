import gettext
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "me.vorotnikov.Obozrenie"


def mo_bytes(messages):
    encoded = sorted(
        (
            message_id.encode("utf-8"),
            message.encode("utf-8"),
        )
        for message_id, message in messages.items()
    )
    count = len(encoded)
    original_table_offset = 28
    translated_table_offset = original_table_offset + count * 8
    original_strings_offset = translated_table_offset + count * 8

    original_strings = bytearray()
    original_descriptors = []
    for message_id, _ in encoded:
        original_descriptors.append(
            (len(message_id), original_strings_offset + len(original_strings))
        )
        original_strings.extend(message_id + b"\0")

    translated_strings_offset = original_strings_offset + len(original_strings)
    translated_strings = bytearray()
    translated_descriptors = []
    for _, message in encoded:
        translated_descriptors.append(
            (len(message), translated_strings_offset + len(translated_strings))
        )
        translated_strings.extend(message + b"\0")

    header = struct.pack(
        "<7I",
        0x950412DE,
        0,
        count,
        original_table_offset,
        translated_table_offset,
        0,
        0,
    )
    original_table = b"".join(
        struct.pack("<2I", *descriptor)
        for descriptor in original_descriptors
    )
    translated_table = b"".join(
        struct.pack("<2I", *descriptor)
        for descriptor in translated_descriptors
    )
    return b"".join((
        header,
        original_table,
        translated_table,
        original_strings,
        translated_strings,
    ))


class TranslationTests(unittest.TestCase):
    CATALOG = mo_bytes({
        "": (
            "Content-Type: text/plain; charset=UTF-8\n"
            "Language: ru\n"
        ),
        "Hello": "Привет",
    })

    def zip_root(self, archive, entries):
        for name, data in entries.items():
            archive.writestr(f"obozrenie/data/{name}", data)
        return zipfile.Path(archive, at="obozrenie/data/")

    def test_packaged_russian_catalog_exists(self):
        from obozrenie import resources

        catalog = resources.resource(
            "locale",
            "ru",
            "LC_MESSAGES",
            f"{DOMAIN}.mo",
        )

        self.assertTrue(catalog.is_file())

    def test_explicit_russian_translates_packaged_python_message(self):
        from obozrenie import i18n

        translations = i18n.translation(["ru_RU"])

        self.assertIsInstance(translations, gettext.GNUTranslations)
        self.assertEqual(translations.gettext("Connect"), "Подключиться")

    def test_explicit_locale_variants_select_packaged_language_catalog(self):
        from obozrenie import i18n, resources

        variants = (
            "ru",
            "ru_RU",
            "ru-RU",
            "ru_RU.UTF-8",
            "ru_RU.UTF-8@formal",
        )
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "locale.zip"
            with zipfile.ZipFile(archive_path, "w") as output:
                self.zip_root(output, {
                    f"locale/ru/LC_MESSAGES/{DOMAIN}.mo": self.CATALOG,
                })
            with zipfile.ZipFile(archive_path) as archive:
                root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", root):
                    for language in variants:
                        with self.subTest(language=language):
                            translations = i18n.translation([language])
                            self.assertIsInstance(
                                translations,
                                gettext.GNUTranslations,
                            )
                            self.assertEqual(
                                translations.gettext("Hello"),
                                "Привет",
                            )

    def test_explicit_language_order_skips_missing_catalogs_deterministically(self):
        from obozrenie import i18n, resources

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "locale.zip"
            with zipfile.ZipFile(archive_path, "w") as output:
                self.zip_root(output, {
                    f"locale/ru/LC_MESSAGES/{DOMAIN}.mo": self.CATALOG,
                })
            with zipfile.ZipFile(archive_path) as archive:
                root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", root):
                    translations = i18n.translation(
                        ["zz_ZZ", "ru-RU", "ru"]
                    )

        self.assertEqual(translations.gettext("Hello"), "Привет")

    def test_c_posix_none_and_missing_catalogs_return_null_translations(self):
        from obozrenie import i18n, resources

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "empty.zip"
            with zipfile.ZipFile(archive_path, "w") as output:
                self.zip_root(output, {
                    f"locale/c/LC_MESSAGES/{DOMAIN}.mo": self.CATALOG,
                    f"locale/posix/LC_MESSAGES/{DOMAIN}.mo": self.CATALOG,
                })
            with zipfile.ZipFile(archive_path) as archive:
                root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", root):
                    for languages in (
                        ["C"],
                        ["C.UTF-8"],
                        ["POSIX"],
                        ["POSIX.UTF-8@variant"],
                        [None, "C", "POSIX"],
                        ["zz_ZZ"],
                        [],
                    ):
                        with self.subTest(languages=languages):
                            self.assertIs(
                                type(i18n.translation(languages)),
                                gettext.NullTranslations,
                            )

    def test_c_posix_terminate_before_a_later_valid_catalog(self):
        from obozrenie import i18n, resources

        language_chains = (
            ("plain C", ["C", "ru"]),
            ("encoded C", ["zz_ZZ", "C.UTF-8", "ru_RU"]),
            ("modified C", ["C@variant", "ru-RU"]),
            ("plain POSIX", ["POSIX", "ru"]),
            ("encoded POSIX", ["zz_ZZ", "POSIX.UTF-8", "ru_RU"]),
            ("modified POSIX", ["POSIX@variant", "ru-RU"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "locale.zip"
            with zipfile.ZipFile(archive_path, "w") as output:
                self.zip_root(output, {
                    f"locale/ru/LC_MESSAGES/{DOMAIN}.mo": self.CATALOG,
                })
            with zipfile.ZipFile(archive_path) as archive:
                root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", root):
                    for label, languages in language_chains:
                        with self.subTest(case=label):
                            translations = i18n.translation(languages)
                            self.assertIs(
                                type(translations),
                                gettext.NullTranslations,
                            )
                            self.assertEqual(
                                translations.gettext("Hello"),
                                "Hello",
                            )

    def test_automatic_language_uses_language_lc_all_lc_messages_lang_precedence(
        self,
    ):
        from obozrenie import i18n

        resolves_to_russian = (
            ("LANGUAGE alone", {"LANGUAGE": "ru_RU"}),
            ("LC_ALL when LANGUAGE is unset", {"LC_ALL": "ru_RU"}),
            (
                "LC_MESSAGES when LANGUAGE and LC_ALL are empty",
                {"LANGUAGE": "", "LC_ALL": "", "LC_MESSAGES": "ru_RU"},
            ),
            (
                "LANG when every higher-priority variable is empty",
                {
                    "LANGUAGE": "",
                    "LC_ALL": "",
                    "LC_MESSAGES": "",
                    "LANG": "ru_RU",
                },
            ),
        )
        for label, environment in resolves_to_russian:
            with self.subTest(case=label):
                with mock.patch.dict(os.environ, environment, clear=True):
                    translations = i18n.translation()

                self.assertIsInstance(translations, gettext.GNUTranslations)
                self.assertEqual(translations.gettext("Connect"), "Подключиться")

        # A higher-priority variable wins outright once it is nonempty, even
        # when its own value never resolves to a packaged catalog: there is
        # no fallthrough to a lower-priority variable that would resolve.
        higher_priority_wins_even_when_unresolved = (
            (
                "LANGUAGE over a resolvable LC_ALL",
                {"LANGUAGE": "zz_ZZ", "LC_ALL": "ru_RU"},
            ),
            (
                "LC_ALL over a resolvable LC_MESSAGES",
                {"LANGUAGE": "", "LC_ALL": "zz_ZZ", "LC_MESSAGES": "ru_RU"},
            ),
            (
                "LC_MESSAGES over a resolvable LANG",
                {
                    "LANGUAGE": "",
                    "LC_ALL": "",
                    "LC_MESSAGES": "zz_ZZ",
                    "LANG": "ru_RU",
                },
            ),
        )
        for label, environment in higher_priority_wins_even_when_unresolved:
            with self.subTest(case=label):
                with mock.patch.dict(os.environ, environment, clear=True):
                    translations = i18n.translation()

                self.assertIs(type(translations), gettext.NullTranslations)

    def test_automatic_language_pins_literal_c_and_whitespace_only_language(
        self,
    ):
        """Pin two selection outcomes a real environment can produce.

        `LC_ALL=C` is a literal lower-priority value, not a request for source
        strings, so a nonempty `LANGUAGE` still wins outright. A
        whitespace-only `LANGUAGE` is nonempty too, so it also wins outright,
        and because it names no language it selects nothing and never falls
        through to the lower-priority variable that would have resolved. Both
        outcomes are deliberate — glibc treats a garbage `LANGUAGE` the same
        way — and are pinned here rather than left to be rediscovered.
        """
        from obozrenie import i18n

        with self.subTest(case="LANGUAGE over a literal LC_ALL=C"):
            with mock.patch.dict(
                os.environ,
                {"LANGUAGE": "ru_RU", "LC_ALL": "C"},
                clear=True,
            ):
                translations = i18n.translation()

            self.assertIsInstance(translations, gettext.GNUTranslations)
            self.assertEqual(translations.gettext("Connect"), "Подключиться")

        with self.subTest(case="whitespace-only LANGUAGE over a valid LC_ALL"):
            with mock.patch.dict(
                os.environ,
                {"LANGUAGE": " \t ", "LC_ALL": "ru_RU"},
                clear=True,
            ):
                translations = i18n.translation()

            self.assertIs(type(translations), gettext.NullTranslations)
            self.assertEqual(translations.gettext("Connect"), "Connect")

    def test_automatic_language_preserves_colon_chain_and_c_posix_termination(
        self,
    ):
        from obozrenie import i18n

        chain_resolves_to_russian = (
            ("LANGUAGE chain", {"LANGUAGE": "zz_ZZ:ru_RU"}),
            ("LANG chain", {"LANG": "zz_ZZ:ru_RU"}),
        )
        for label, environment in chain_resolves_to_russian:
            with self.subTest(case=label):
                with mock.patch.dict(os.environ, environment, clear=True):
                    translations = i18n.translation()

                self.assertIsInstance(translations, gettext.GNUTranslations)
                self.assertEqual(translations.gettext("Connect"), "Подключиться")

        chain_terminates_at_c_or_posix = (
            ("plain C first", "C:ru_RU"),
            ("encoded C mid-chain", "zz_ZZ:C.UTF-8:ru_RU"),
            ("plain POSIX first", "POSIX:ru_RU"),
            ("modified POSIX mid-chain", "zz_ZZ:POSIX@variant:ru_RU"),
        )
        for label, chain in chain_terminates_at_c_or_posix:
            with self.subTest(case=label):
                with mock.patch.dict(
                    os.environ,
                    {"LANGUAGE": chain},
                    clear=True,
                ):
                    translations = i18n.translation()

                self.assertIs(type(translations), gettext.NullTranslations)
                self.assertEqual(translations.gettext("Hello"), "Hello")

    def test_explicit_languages_never_read_message_locale_environment(self):
        from obozrenie import i18n

        poisoned_environment = {
            "LANGUAGE": "zz_ZZ",
            "LC_ALL": "zz_ZZ",
            "LC_MESSAGES": "zz_ZZ",
            "LANG": "zz_ZZ",
        }
        with mock.patch.dict(os.environ, poisoned_environment, clear=True):
            with mock.patch.object(
                i18n,
                "_message_locale_languages",
                side_effect=AssertionError(
                    "explicit languages must not read the environment"
                ),
            ):
                translations = i18n.translation(["ru_RU"])
                self.assertIsInstance(translations, gettext.GNUTranslations)
                self.assertEqual(translations.gettext("Connect"), "Подключиться")

                translations = i18n.translation("ru_RU")
                self.assertIsInstance(translations, gettext.GNUTranslations)
                self.assertEqual(translations.gettext("Connect"), "Подключиться")

                translations = i18n.translation([])
                self.assertIs(type(translations), gettext.NullTranslations)

    def test_source_first_import_uses_message_locale_when_ctype_differs(self):
        script = """
import gettext
from obozrenie import i18n
assert type(i18n._translations) is gettext.GNUTranslations, type(i18n._translations)
assert i18n._.__self__ is i18n._translations
assert i18n._("Connect") == "Подключиться"
"""
        environment = os.environ.copy()
        environment.update({
            "LC_CTYPE": "C",
            "LANGUAGE": "zz_ZZ:ru_RU",
            "LC_ALL": "C",
            "LC_MESSAGES": "C",
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(PROJECT_ROOT),
                        environment.get("PYTHONPATH"),
                    ),
                )
            ),
        })
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_c_and_posix_import_and_system_translation_are_null_safe(self):
        script = """
import gettext
from obozrenie import i18n
assert type(i18n.translation()) is gettext.NullTranslations
assert i18n._("untranslated") == "untranslated"
"""
        for locale_name in ("C", "POSIX"):
            with self.subTest(locale=locale_name):
                environment = os.environ.copy()
                environment.update({
                    "LANG": locale_name,
                    "LC_ALL": locale_name,
                    "LANGUAGE": "",
                    "PYTHONPATH": os.pathsep.join(
                        filter(
                            None,
                            (
                                str(PROJECT_ROOT),
                                environment.get("PYTHONPATH"),
                            ),
                        )
                    ),
                })
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=PROJECT_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_test_modules_import_without_babel(self):
        script = """
import importlib
import importlib.abc
import sys

class BlockBabel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "babel" or fullname.startswith("babel."):
            raise ModuleNotFoundError(f"blocked runtime import: {fullname}")
        return None

sys.meta_path.insert(0, BlockBabel())
importlib.import_module("tests.test_i18n")
importlib.import_module("tests.test_resources")
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (str(PROJECT_ROOT), environment.get("PYTHONPATH")),
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
