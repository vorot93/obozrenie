#!/usr/bin/env python3
from io import BytesIO
from pathlib import Path
import tomllib

from babel.messages import pofile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "me.vorotnikov.Obozrenie"
LOCALE_ROOT = PROJECT_ROOT / "obozrenie" / "data" / "locale"
CATALOGS = (
    (LOCALE_ROOT / f"{DOMAIN}.pot", None),
    (LOCALE_ROOT / "ru" / "LC_MESSAGES" / f"{DOMAIN}.po", "ru"),
)


def header_comment(metadata, locale):
    description = (
        "Russian translations"
        if locale == "ru"
        else "Translations template"
    )
    return "\n".join((
        f"# {description} for {metadata['project']}.",
        "# Copyright (C) "
        f"{metadata['copyright-years']} {metadata['copyright-holder']}",
        "# This file is distributed under the same license as the "
        f"{metadata['project']} project.",
        f"# {metadata['last-translator']}, {metadata['copyright-years']}.",
        "#",
    ))


def normalize_catalog(path, locale, metadata):
    with path.open(encoding="utf-8") as catalog_file:
        catalog = pofile.read_po(
            catalog_file,
            locale=locale,
            abort_invalid=True,
        )
    catalog.project = metadata["project"]
    catalog.version = metadata["version"]
    catalog.copyright_holder = metadata["copyright-holder"]
    catalog.msgid_bugs_address = metadata["msgid-bugs-address"]
    catalog.creation_date = metadata["pot-creation-date"]
    catalog.revision_date = metadata[
        "po-revision-date" if locale == "ru" else "pot-creation-date"
    ]
    catalog.last_translator = metadata["last-translator"]
    catalog.language_team = metadata[
        "russian-language-team"
        if locale == "ru"
        else "template-language-team"
    ]
    catalog.header_comment = header_comment(metadata, locale)

    output = BytesIO()
    pofile.write_po(output, catalog)
    path.write_bytes(output.getvalue().rstrip(b"\r\n") + b"\n")


def main():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        metadata = tomllib.load(project_file)["tool"]["obozrenie"][
            "translations"
        ]
    for path, locale in CATALOGS:
        normalize_catalog(path, locale, metadata)


if __name__ == "__main__":
    main()
