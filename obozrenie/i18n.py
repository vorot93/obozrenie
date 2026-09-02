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

import gettext
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from obozrenie import resources
from obozrenie.global_settings import APPLICATION_ID


_SOURCE_LANGUAGES = {"C", "POSIX"}
# Standard gettext message-locale variables, highest priority first. The
# first nonempty one wins outright; its own colon-separated chain is used
# as-is and lower-priority variables are never consulted. LC_CTYPE plays no
# part in message-catalog selection.
_MESSAGE_LOCALE_ENV = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def _language_candidates(languages):
    seen = set()
    for language in languages:
        if not isinstance(language, str):
            continue
        language = language.strip()
        if not language:
            continue
        language = language.split("@", 1)[0].split(".", 1)[0]
        source_language = language.upper()
        if source_language in _SOURCE_LANGUAGES:
            yield source_language
            return
        parts = language.replace("-", "_").split("_")
        if (
            not parts[0].isalpha()
            or any(not re.fullmatch(r"[A-Za-z0-9]+", part) for part in parts)
        ):
            continue
        normalized = [parts[0].lower()]
        if len(parts) > 1:
            normalized.append(parts[1].upper())
            normalized.extend(parts[2:])
        for candidate in ("_".join(normalized), normalized[0]):
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _message_locale_languages(environment: Mapping[str, str]) -> tuple[str, ...]:
    for name in _MESSAGE_LOCALE_ENV:
        value = environment.get(name)
        if value:
            return tuple(value.split(":"))
    return ()


def translation(languages=None):
    if languages is None:
        languages = _message_locale_languages(os.environ)
    elif isinstance(languages, str):
        languages = (languages,)

    for language in _language_candidates(languages):
        if language in _SOURCE_LANGUAGES:
            return gettext.NullTranslations()
        catalog = resources.resource(
            "locale",
            language,
            "LC_MESSAGES",
            f"{APPLICATION_ID}.mo",
        )
        try:
            catalog_file = catalog.open("rb")
        except FileNotFoundError:
            continue
        with catalog_file:
            return gettext.GNUTranslations(catalog_file)
    return gettext.NullTranslations()


def translate_builder_xml(document, translations=None):
    """Translate marked builder text and prevent a second GTK translation."""
    if translations is None:
        translations = _translations
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(document, parser=parser)
    for element in root.iter():
        if element.attrib.pop("translatable", None) == "yes":
            if element.text:
                element.text = translations.gettext(element.text)
    return ET.tostring(root, encoding="unicode")


_translations = translation()
_ = _translations.gettext
