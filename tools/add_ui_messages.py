#!/usr/bin/env python3
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from babel.messages import pofile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "me.vorotnikov.Obozrenie"
POT_PATH = PROJECT_ROOT / "obozrenie" / "data" / "locale" / f"{DOMAIN}.pot"
UI_PATHS = (
    PROJECT_ROOT / "obozrenie" / "data" / "ui" / "obozrenie_gtk.ui",
    PROJECT_ROOT
    / "obozrenie"
    / "data"
    / "ui"
    / "obozrenie_gtk_appmenu.ui",
)
TRANSLATABLE_TAGS = {"attribute", "property"}


def ui_messages():
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
            yield message, path.relative_to(PROJECT_ROOT).as_posix()


def main():
    with POT_PATH.open(encoding="utf-8") as pot_file:
        catalog = pofile.read_po(pot_file, abort_invalid=True)
    for message, filename in ui_messages():
        catalog.add(message, locations=[(filename, None)])

    output = BytesIO()
    pofile.write_po(output, catalog)
    POT_PATH.write_bytes(output.getvalue().rstrip(b"\r\n") + b"\n")


if __name__ == "__main__":
    main()
