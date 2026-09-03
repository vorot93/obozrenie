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

"""Packaged icons as GTK 4 textures."""

import gi
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

from obozrenie import resources


GAME_ICON_FORMATS = ("png", "svg")
GAME_ICON_FALLBACK = "image-missing"


def normalize_icon_bytes(data: bytes, icon_format: str) -> bytes:
    """Drop the metadata some bundled SVGs place before their root element.

    gdk-pixbuf sniffs only the first kilobyte of a file to detect its format
    and its SVG decoder rejects a leading comment block, so everything before
    `<svg` is replaced by a plain XML declaration.
    """
    if icon_format == "svg":
        start = data.find(b"<svg")
        if start > 0:
            data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + data[start:]
    return data


def load_texture(icon, icon_format: str) -> Gdk.Texture:
    """Load one icon resource (a Traversable) as a texture at its own size."""
    data = normalize_icon_bytes(icon.read_bytes(), icon_format)
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))


def texture_for_entry(entry: str, icon_formats, icon_root) -> Gdk.Texture:
    """Load the first readable `<entry>.<format>` under the icon root.

    A missing file and an unreadable one are the same absence to the caller,
    so both end in `FileNotFoundError` once every format has been tried.
    """
    for icon_format in icon_formats:
        icon = icon_root.joinpath(entry.lower() + "." + icon_format)
        if not icon.is_file():
            continue
        try:
            return load_texture(icon, icon_format)
        except (GLib.Error, OSError):
            continue
    raise FileNotFoundError(entry)


def game_icon_textures(game_ids, icon_root=None) -> dict[str, Gdk.Texture]:
    """Map every game id to its icon texture, or to the one shared fallback."""
    if icon_root is None:
        icon_root = resources.resource("icons", "games")
    textures = {}
    fallback = None
    for game_id in game_ids:
        try:
            textures[game_id] = texture_for_entry(game_id, GAME_ICON_FORMATS, icon_root)
        except FileNotFoundError:
            if fallback is None:
                fallback = texture_for_entry(GAME_ICON_FALLBACK, GAME_ICON_FORMATS, icon_root)
            textures[game_id] = fallback
    return textures


def load_logo() -> Gdk.Texture:
    """The about-dialog logo, from the packaged short application icon."""
    return load_texture(
        resources.resource("icons", "hicolor", "scalable", "apps", "obozrenie-short.svg"),
        "svg",
    )
