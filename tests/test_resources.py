import base64
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from importlib.resources.abc import Traversable
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]

GAME_IDS = {
    "alienarena",
    "csgo",
    "cstrike",
    "dod",
    "doom3",
    "et",
    "freeminer",
    "garrysmod",
    "gesource",
    "hl1mp",
    "hl2mp",
    "jediacademy",
    "jedioutcast",
    "left4dead2",
    "minetest",
    "openarena",
    "openttd",
    "portal2",
    "q2",
    "q3a",
    "q4",
    "qw",
    "rigsofrods",
    "rtcw",
    "stef1",
    "tf",
    "turtlearena",
    "unvanquished",
    "urbanterror",
    "warsow",
    "wop",
    "xonotic",
}

QSTAT_GAME_IDS = {
    "alienarena",
    "csgo",
    "cstrike",
    "dod",
    "doom3",
    "et",
    "garrysmod",
    "gesource",
    "hl1mp",
    "hl2mp",
    "jediacademy",
    "jedioutcast",
    "left4dead2",
    "openarena",
    "openttd",
    "portal2",
    "q2",
    "q3a",
    "q4",
    "qw",
    "rtcw",
    "stef1",
    "tf",
    "turtlearena",
    "unvanquished",
    "urbanterror",
    "warsow",
    "wop",
    "xonotic",
}

GAME_ICON_FILES = {
    "alienarena.png",
    "csgo.png",
    "cstrike.png",
    "dod.png",
    "doom3.png",
    "et.png",
    "freeminer.svg",
    "garrysmod.png",
    "gesource.png",
    "hl1mp.png",
    "hl2mp.png",
    "image-missing.png",
    "jediacademy.png",
    "jedioutcast.png",
    "left4dead2.png",
    "minetest.svg",
    "openarena.png",
    "openttd.png",
    "portal2.png",
    "q2.png",
    "q3a.png",
    "q4.png",
    "qw.png",
    "rigsofrods.png",
    "rtcw.png",
    "stef1.png",
    "tf.png",
    "turtlearena.png",
    "unvanquished.png",
    "urbanterror.png",
    "warsow.png",
    "wop.png",
    "xonotic.png",
}

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

# The two hicolor application icons: original GPL-3.0-only geometry built
# solely from basic vector primitives, with the full icon's 3:1 aspect and
# the short icon's 1:1 aspect fixed by their viewBox proportions.
APPLICATION_ICON_ASPECTS = {
    "obozrenie.svg": 3.0,
    "obozrenie-short.svg": 1.0,
}

# Only plain geometric containers/shapes are allowed: no text, no embedded
# metadata/fonts, no external or scripted content.
ALLOWED_SVG_ELEMENTS = {"svg", "g", "circle", "rect", "path"}
FORBIDDEN_SVG_ELEMENTS = {
    "text",
    "metadata",
    "defs",
    "image",
    "script",
    "style",
    "use",
    "a",
}
FORBIDDEN_SVG_ATTRIBUTES = {"href", "style"}
FORBIDDEN_ATTRIBUTION_MARKERS = (b"creativecommons", b"neometric", b"andres")


def local_element_name(qualified_name):
    """Strip a Clark-notation namespace, e.g. '{...}svg' -> 'svg'."""
    return qualified_name.rsplit("}", 1)[-1]


def resource_module():
    from obozrenie import resources

    return resources


def relative_files(root, prefix=()):
    files = set()
    for child in root.iterdir():
        parts = (*prefix, child.name)
        if child.is_dir():
            files.update(relative_files(child, parts))
        elif child.is_file():
            files.add("/".join(parts))
    return files


class BuilderWidget:
    def __init__(self, value=None):
        self.value = value

    def set_property(self, name, value):
        setattr(self, name, value)

    def page_num(self, widget):
        return 0


class Builder:
    def __init__(self):
        self.objects = {}

    def get_object(self, object_id):
        return self.objects.setdefault(object_id, BuilderWidget(object_id))


class ResourceTests(unittest.TestCase):
    def test_resource_helpers_read_real_packaged_data(self):
        resources = resource_module()

        game_lists = resources.resource(
            "settings", "internal", "game_lists.toml"
        )
        self.assertIsInstance(game_lists, Traversable)
        self.assertEqual(
            resources.load_toml(
                "settings", "internal", "game_lists.toml"
            )["garrysmod"]["steam_app_id"],
            "4000",
        )
        ui_text = resources.read_text("ui", "obozrenie_gtk.ui")
        self.assertEqual(ET.fromstring(ui_text).tag, "interface")
        self.assertEqual(
            resources.read_bytes("ui", "obozrenie_gtk.ui"),
            ui_text.encode("utf-8"),
        )

    def test_missing_resources_fail_instead_of_falling_back(self):
        resources = resource_module()

        with self.assertRaises(FileNotFoundError):
            resources.read_text("missing", "resource.txt")
        with self.assertRaises(FileNotFoundError):
            resources.read_bytes("missing", "resource.bin")
        with self.assertRaises(FileNotFoundError):
            resources.load_toml("missing", "resource.toml")

    def test_resources_are_independent_of_cwd_and_sys_prefix(self):
        script = """
import sys
sys.prefix = "/path/that/cannot/contain/obozrenie"
from obozrenie import resources
games = resources.load_toml("settings", "internal", "game_lists.toml")
assert games["garrysmod"]["steam_app_id"] == "4000"
assert resources.resource("ui", "obozrenie_gtk.ui").is_file()
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (str(PROJECT_ROOT), environment.get("PYTHONPATH")),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_zip_backed_resources_support_text_bytes_toml_ui_and_icons(self):
        resources = resource_module()
        from obozrenie import gtk_helpers

        entries = {
            "obozrenie/data/message.txt": "zip-backed text\n".encode(),
            "obozrenie/data/invalid.txt": b"invalid utf-8: \xff",
            "obozrenie/data/settings/example.toml": b'[entry]\nname = "Zip"\n',
            "obozrenie/data/ui/example.ui": b"<interface />\n",
            "obozrenie/data/icons/games/zip.png": ONE_PIXEL_PNG,
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "resources.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
            with zipfile.ZipFile(archive_path) as archive:
                zip_root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", zip_root):
                    self.assertEqual(
                        resources.read_text("message.txt"),
                        "zip-backed text\n",
                    )
                    self.assertEqual(
                        resources.read_bytes("icons", "games", "zip.png"),
                        ONE_PIXEL_PNG,
                    )
                    self.assertEqual(
                        resources.load_toml("settings", "example.toml"),
                        {"entry": {"name": "Zip"}},
                    )
                    self.assertEqual(
                        ET.fromstring(resources.read_text("ui", "example.ui")).tag,
                        "interface",
                    )
                    with self.assertRaises(UnicodeDecodeError):
                        resources.read_text("invalid.txt")
                    icon = gtk_helpers.get_icon_for_entry(
                        "zip",
                        ("png",),
                        resources.resource("icons", "games"),
                        24,
                        24,
                    )

        self.assertIsNotNone(icon)
        self.assertEqual((icon.get_width(), icon.get_height()), (24, 24))

    def test_packaged_game_config_and_icon_sets_are_exact(self):
        resources = resource_module()
        self.assertEqual(
            relative_files(resources.resource("icons", "games")),
            GAME_ICON_FILES,
        )

        game_config = resources.load_toml(
            "settings", "internal", "game_lists.toml"
        )
        default_games = resources.load_toml(
            "settings", "defaults", "games.toml"
        )
        qstat_config = resources.load_toml(
            "settings", "internal", "backends", "qstat.toml"
        )["game"]
        self.assertEqual(set(game_config), GAME_IDS)
        self.assertEqual(set(default_games), GAME_IDS)
        self.assertEqual(
            {
                game_id
                for game_id, entry in game_config.items()
                if entry["adapter"] == "qstat"
            },
            QSTAT_GAME_IDS,
        )
        self.assertEqual(set(qstat_config), QSTAT_GAME_IDS)
        self.assertEqual(
            {Path(filename).stem for filename in GAME_ICON_FILES}
            - {"image-missing"},
            GAME_IDS,
        )

    def test_both_ui_documents_parse_from_resource_text(self):
        resources = resource_module()

        for filename in ("obozrenie_gtk.ui", "obozrenie_gtk_appmenu.ui"):
            with self.subTest(filename=filename):
                root = ET.fromstring(resources.read_text("ui", filename))
                self.assertEqual(root.tag, "interface")

    def test_desktop_entry_and_application_icons_are_canonical_resources(self):
        resources = resource_module()

        desktop = resources.read_text(
            "applications", "me.vorotnikov.Obozrenie.desktop"
        )
        self.assertIn("\nExec=obozrenie-gtk\n", f"\n{desktop}")
        self.assertIn("\nIcon=obozrenie-short\n", f"\n{desktop}")
        for filename in ("obozrenie.svg", "obozrenie-short.svg"):
            with self.subTest(filename=filename):
                icon = resources.read_bytes(
                    "icons", "hicolor", "scalable", "apps", filename
                )
                self.assertIn(b"<svg", icon)

    def test_application_icons_use_only_internal_basic_geometry(self):
        resources = resource_module()

        for filename in APPLICATION_ICON_ASPECTS:
            with self.subTest(filename=filename):
                data = resources.read_bytes(
                    "icons", "hicolor", "scalable", "apps", filename
                )
                lowered = data.lower()
                for marker in FORBIDDEN_ATTRIBUTION_MARKERS:
                    self.assertNotIn(
                        marker,
                        lowered,
                        f"{filename} still carries {marker!r} attribution",
                    )

                root = ET.fromstring(data)
                for element in root.iter():
                    tag = local_element_name(element.tag)
                    self.assertNotIn(
                        tag,
                        FORBIDDEN_SVG_ELEMENTS,
                        f"{filename} contains forbidden element <{tag}>",
                    )
                    self.assertIn(
                        tag,
                        ALLOWED_SVG_ELEMENTS,
                        f"{filename} contains unrecognised element <{tag}>",
                    )
                    for attribute in element.attrib:
                        attribute_name = local_element_name(attribute).lower()
                        self.assertFalse(
                            attribute_name.startswith("font-"),
                            f"{filename} <{tag}> sets {attribute_name}",
                        )
                        self.assertNotIn(
                            attribute_name,
                            FORBIDDEN_SVG_ATTRIBUTES,
                            f"{filename} <{tag}> sets {attribute_name}",
                        )

    def test_application_icons_render_at_small_and_large_sizes(self):
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        resources = resource_module()

        for filename, aspect in APPLICATION_ICON_ASPECTS.items():
            icon_path = str(
                resources.resource(
                    "icons", "hicolor", "scalable", "apps", filename
                )
            )
            for size in (32, 256):
                with self.subTest(filename=filename, size=size):
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                        icon_path, size, size
                    )
                    self.assertGreater(pixbuf.get_width(), 0)
                    self.assertGreater(pixbuf.get_height(), 0)
                    self.assertAlmostEqual(
                        pixbuf.get_width() / pixbuf.get_height(),
                        aspect,
                        delta=0.15,
                    )

    def test_gui_actions_loads_about_logo_from_zip_resource_bytes(self):
        resources = resource_module()
        from obozrenie import core
        from obozrenie import gtk as gtk_module

        logo = b"""<svg xmlns="http://www.w3.org/2000/svg" width="7" height="5">
<rect width="7" height="5" fill="red"/>
</svg>
"""
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "images.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "obozrenie/data/icons/hicolor/scalable/apps/"
                    "obozrenie-short.svg",
                    logo,
                )
                archive.writestr(
                    "obozrenie/data/icons/games/image-missing.png",
                    ONE_PIXEL_PNG,
                )
            with zipfile.ZipFile(archive_path) as archive:
                root = zipfile.Path(archive, at="obozrenie/data/")
                with mock.patch.object(resources, "ROOT", root):
                    actions = gtk_module.GUIActions(
                        SimpleNamespace(),
                        Builder(),
                        SimpleNamespace(game_table=core.GameTable({})),
                    )

        self.assertEqual(
            (actions.logo.get_width(), actions.logo.get_height()),
            (7, 5),
        )

if __name__ == "__main__":
    unittest.main()
