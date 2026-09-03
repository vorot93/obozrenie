#!/usr/bin/env python3
import gettext
from io import BytesIO
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from babel.messages import extract, mofile, pofile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "obozrenie"
DOMAIN = "me.vorotnikov.Obozrenie"
LOCALE_ROOT = PACKAGE_ROOT / "data" / "locale"
POT_PATH = LOCALE_ROOT / f"{DOMAIN}.pot"
PO_PATH = LOCALE_ROOT / "ru" / "LC_MESSAGES" / f"{DOMAIN}.po"
MO_PATH = LOCALE_ROOT / "ru" / "LC_MESSAGES" / f"{DOMAIN}.mo"
UI_PATHS = tuple(sorted((PACKAGE_ROOT / "data" / "ui").glob("*.ui")))
TRANSLATABLE_TAGS = {"attribute", "item", "property"}
REPRESENTATIVE_TRANSLATIONS = {
    "Connect": "Подключиться",
    "Terrain": "Карта",
    "About": "О программе",
    "Quit": "Выйти",
}
MNEMONIC_TRANSLATIONS = {
    "_Game Mod:": "_Мод:",
    "Game _Type:": "_Тип игры:",
    "Te_rrain:": "_Карта:",
    "Max _ping:": "Макс. _пинг:",
    "_Secure:": "_Защита:",
}


def python_messages():
    keywords = {**extract.DEFAULT_KEYWORDS, "N_": None}
    return {
        tuple(message) if isinstance(message, (list, tuple)) else message
        for _filename, _lineno, message, _comments, _context
        in extract.extract_from_dir(PACKAGE_ROOT, keywords=keywords)
    }


def ui_messages():
    messages = set()
    for path in UI_PATHS:
        root = ET.parse(path).getroot()
        for element in root.iter():
            if element.get("translatable") != "yes":
                continue
            if element.tag not in TRANSLATABLE_TAGS:
                raise ValueError(
                    f"unsupported translatable <{element.tag}> in {path}"
                )
            message = element.text or ""
            if not message.strip():
                raise ValueError(f"empty translatable <{element.tag}> in {path}")
            messages.add(message)
    return messages


def read_catalog(path, *, locale=None):
    with path.open(encoding="utf-8") as catalog_file:
        return pofile.read_po(
            catalog_file,
            locale=locale,
            abort_invalid=True,
        )


def active_messages(catalog):
    return {
        message.id: message
        for message in catalog
        if message.id
    }


def format_messages(messages):
    return ", ".join(sorted((repr(message) for message in messages)))


def compare_message_set(errors, label, actual, expected):
    missing = expected - actual
    extra = actual - expected
    if missing:
        errors.append(f"{label} is missing: {format_messages(missing)}")
    if extra:
        errors.append(f"{label} has stale entries: {format_messages(extra)}")


def check_catalog_ending(errors, path):
    data = path.read_bytes()
    if not data.endswith(b"\n") or data.endswith(b"\n\n"):
        errors.append(f"catalog must end with exactly one newline: {path}")


def message_is_translated(message):
    if isinstance(message.id, tuple):
        return (
            isinstance(message.string, (list, tuple))
            and bool(message.string)
            and all(message.string)
        )
    return isinstance(message.string, str) and bool(message.string)


def check_catalog_formats(errors, catalog):
    for message, validation_errors in catalog.check():
        for validation_error in validation_errors:
            errors.append(
                f"Russian PO format error for {message.id!r}: "
                f"{validation_error}"
            )


def compile_catalog(catalog):
    output = BytesIO()
    mofile.write_mo(output, catalog)
    return output.getvalue()


def check_mo_semantics(errors, current_ids, fresh_mo, tracked_mo):
    fresh = gettext.GNUTranslations(BytesIO(fresh_mo))
    tracked = gettext.GNUTranslations(BytesIO(tracked_mo))
    for message_id in current_ids:
        if isinstance(message_id, tuple):
            for count in range(7):
                fresh_message = fresh.ngettext(
                    message_id[0],
                    message_id[1],
                    count,
                )
                tracked_message = tracked.ngettext(
                    message_id[0],
                    message_id[1],
                    count,
                )
                if tracked_message != fresh_message:
                    errors.append(
                        f"tracked MO plural differs for {message_id!r}, n={count}"
                    )
        elif tracked.gettext(message_id) != fresh.gettext(message_id):
            errors.append(f"tracked MO translation differs for {message_id!r}")

    representatives = {
        **REPRESENTATIVE_TRANSLATIONS,
        **MNEMONIC_TRANSLATIONS,
    }
    for message_id, expected in representatives.items():
        for label, translations in (("fresh", fresh), ("tracked", tracked)):
            actual = translations.gettext(message_id)
            if actual != expected:
                errors.append(
                    f"{label} MO translates {message_id!r} as {actual!r}, "
                    f"expected {expected!r}"
                )

    mnemonic_keys = []
    for message_id in MNEMONIC_TRANSLATIONS:
        translated = tracked.gettext(message_id)
        if translated.count("_") != 1:
            errors.append(f"invalid mnemonic marker in {translated!r}")
            continue
        marker = translated.index("_") + 1
        if marker >= len(translated) or not translated[marker].isalpha():
            errors.append(f"invalid mnemonic key in {translated!r}")
            continue
        mnemonic_keys.append(translated[marker].casefold())
    if len(mnemonic_keys) != len(set(mnemonic_keys)):
        errors.append("Russian filter mnemonic keys are not unique")


def main():
    python_ids = python_messages()
    ui_ids = ui_messages()
    current_ids = python_ids | ui_ids
    template = read_catalog(POT_PATH)
    russian = read_catalog(PO_PATH, locale="ru")
    template_messages = active_messages(template)
    russian_messages = active_messages(russian)

    errors = []
    check_catalog_ending(errors, POT_PATH)
    check_catalog_ending(errors, PO_PATH)
    compare_message_set(
        errors,
        "POT",
        set(template_messages),
        current_ids,
    )
    compare_message_set(
        errors,
        "Russian PO",
        set(russian_messages),
        current_ids,
    )
    if template.obsolete:
        errors.append(
            f"POT has obsolete entries: {format_messages(template.obsolete)}"
        )
    if russian.obsolete:
        errors.append(
            "Russian PO has obsolete entries: "
            f"{format_messages(russian.obsolete)}"
        )

    fuzzy = {
        message_id
        for message_id, message in russian_messages.items()
        if "fuzzy" in message.flags
    }
    untranslated = {
        message_id
        for message_id, message in russian_messages.items()
        if not message_is_translated(message)
    }
    if fuzzy:
        errors.append(f"Russian PO has fuzzy entries: {format_messages(fuzzy)}")
    if untranslated:
        errors.append(
            f"Russian PO has untranslated entries: {format_messages(untranslated)}"
        )
    check_catalog_formats(errors, russian)

    if not errors:
        first_compile = compile_catalog(russian)
        second_compile = compile_catalog(read_catalog(PO_PATH, locale="ru"))
        if first_compile != second_compile:
            errors.append("repeated MO compilation is not deterministic")
        try:
            tracked_mo = MO_PATH.read_bytes()
        except FileNotFoundError:
            errors.append(f"tracked MO is missing: {MO_PATH}")
        else:
            if tracked_mo != first_compile:
                errors.append("tracked MO does not match a fresh PO compile")
            check_mo_semantics(errors, current_ids, first_compile, tracked_mo)

    if errors:
        for error in errors:
            print(f"translation check failed: {error}", file=sys.stderr)
        return 1

    overlap = python_ids & ui_ids
    print(
        "translation check passed: "
        f"{len(python_ids)} Python, {len(ui_ids)} UI, "
        f"{len(overlap)} shared, {len(current_ids)} catalog messages, "
        f"{len(russian_messages)} Russian translations, "
        f"{len(first_compile)} deterministic MO bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
