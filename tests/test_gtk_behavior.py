import errno
import gettext
import io
import os
import tempfile
import threading
import time
import tomllib
import unittest
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

from obozrenie import game_table
from obozrenie import gtk as gtk_module
from obozrenie import gtk_geoip, gtk_preferences, gtk_window
from obozrenie import i18n
from obozrenie import models
from obozrenie import settings as settings_module
from obozrenie.diagnostics import SafeDiagnostic
from obozrenie.models import Endpoint, Player, Server


class LaunchCoreStub:
    def __init__(self):
        self.callback = None

    def start_game(self, game, server, password, callback=None):
        self.callback = callback
        return True


class Widget:
    def __init__(self, value=None):
        self.value = value
        self.properties = {}

    def set_property(self, name, value):
        self.properties[name] = value


class NotebookWidget(Widget):
    def page_num(self, widget):
        return 0


class Builder:
    def __init__(self):
        self.objects = {}

    def get_object(self, object_id):
        if object_id not in self.objects:
            widget_type = (
                NotebookWidget
                if object_id == "ServerList_Notebook"
                else Widget
            )
            self.objects[object_id] = widget_type(object_id)
        return self.objects[object_id]


class XMLRecordingBuilder:
    def __init__(self):
        self.documents = []

    def add_from_string(self, document):
        self.documents.append(ET.fromstring(document))


class RecordingCore:
    def __init__(self, game_table):
        self.game_table = game_table
        self.launches = []
        self.refreshes = []

    def start_game(self, game, server, password, callback=None):
        self.launches.append((game, server, password, callback))
        return True

    def update_server_list(self, game, stat_callback=None):
        self.refreshes.append((game, stat_callback))
        return self.game_table.begin_query(game) is not None


class AccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from obozrenie import resources

        root = ET.fromstring(
            resources.read_text("ui", "obozrenie_gtk.ui")
        )
        cls.objects = {
            element.get("id"): element
            for element in root.findall(".//object")
        }

    def get_property(self, object_id, property_name):
        widget = self.objects[object_id]
        normalized_name = property_name.replace("-", "_")
        for element in widget.findall("property"):
            if element.get("name", "").replace("-", "_") == normalized_name:
                return (element.text or "").strip()
        return None

    def assert_true_property(self, object_id, property_name):
        value = self.get_property(object_id, property_name)
        self.assertIsNotNone(value, f"{object_id}.{property_name} is missing")
        self.assertEqual(value.casefold(), "true")

    def test_primary_selectors_and_secure_filter_are_keyboard_focusable(self):
        for object_id in (
            "Game_TreeView",
            "Game_ComboBox",
            "server-connect-game",
            "filter-secure-comboboxtext",
        ):
            with self.subTest(object_id=object_id):
                self.assert_true_property(object_id, "can_focus")

    def test_tree_search_uses_the_displayed_name_columns(self):
        for object_id, expected_column in (
            ("Game_TreeView", "1"),
            ("ServerList_View", "7"),
        ):
            with self.subTest(object_id=object_id):
                self.assertEqual(
                    self.get_property(object_id, "search_column"),
                    expected_column,
                )

    def test_filter_labels_have_focusable_marked_mnemonic_targets(self):
        relationships = {
            "filter-mod-label": "filter-mod-entry",
            "filter-type-label": "filter-type-entry",
            "filter-terrain-label": "filter-terrain-entry",
            "filter-ping-label": "filter-ping-spinbutton",
            "filter-secure-label": "filter-secure-comboboxtext",
        }
        for label_id, target_id in relationships.items():
            with self.subTest(label_id=label_id):
                self.assertEqual(
                    self.get_property(label_id, "mnemonic_widget"),
                    target_id,
                )
                self.assert_true_property(label_id, "use_underline")
                self.assertIn(target_id, self.objects)
                self.assert_true_property(target_id, "can_focus")
                self.assertIn(
                    "_",
                    gtk_window.GTK_STRING_TABLE[label_id]["label"],
                    "the runtime label needs a mnemonic marker",
                )


class BuilderVisibilityTests(unittest.TestCase):
    """The delivered UI document, not `show_all()`, decides visibility.

    Activation only presents the toplevel, so every widget inside the main
    window appears solely because this document declares it visible.
    """

    @staticmethod
    def widget_class_names(class_names):
        """Return the subset of these GtkBuilder class names that are widgets."""
        from gi.repository import GObject

        widgets = set()
        for class_name in class_names:
            # Touch the Python wrapper first: gi registers a class with the
            # GObject type system only once that wrapper has been looked up.
            getattr(gtk_module.Gtk, class_name.removeprefix("Gtk"), None)
            gtype = GObject.type_from_name(class_name)
            if gtype.is_a(gtk_module.Gtk.Widget.__gtype__):
                widgets.add(class_name)
        return widgets

    @staticmethod
    def declares_visible(element):
        for candidate in element.findall("property"):
            if candidate.get("name", "").replace("-", "_") == "visible":
                return True
        return False

    @staticmethod
    def reachable_objects(document, root):
        """Every object this window shows, including those defined outside it.

        A widget a property names by id — a button's `image`, say — belongs to
        this window even though the document defines it beside the window
        rather than inside it, so a walk of XML descendants alone would let a
        future edit drop `visible` from one of them unnoticed. Only top-level
        definitions are followed, which is where GtkBuilder puts what a
        property can reference and which keeps a label whose text happens to
        repeat a nested id out of the walk.
        """
        top_level = {
            element.get("id"): element
            for element in document.findall("./object")
            if element.get("id") is not None
        }
        collected = {}
        pending = root.findall(".//object")
        while pending:
            element = pending.pop()
            if id(element) in collected:
                continue
            collected[id(element)] = element
            for candidate in element.findall("property"):
                target = top_level.get((candidate.text or "").strip())
                if target is not None:
                    pending.append(target)
                    pending.extend(target.findall(".//object"))
        return list(collected.values())

    def test_every_main_window_widget_declares_visible(self):
        from obozrenie import resources

        document = ET.fromstring(resources.read_text("ui", "obozrenie_gtk.ui"))
        main_window = document.find("./object[@id='Main_Window']")
        self.assertIsNotNone(main_window, "the main window object is missing")

        descendants = self.reachable_objects(document, main_window)
        widget_classes = self.widget_class_names(
            {element.get("class") for element in descendants})
        widgets = [
            element
            for element in descendants
            if element.get("class") in widget_classes
        ]

        # The walk has to reach the deepest widgets, the ones only a property
        # names, and to leave cell renderers, models, and columns out, or it
        # would pass vacuously.
        widget_ids = {element.get("id") for element in widgets}
        self.assertLess(len(widgets), len(descendants))
        for object_id in (
            "Game_View_ToggleButton",
            "Game_TreeView",
            "ServerList_View",
            "filter-secure-comboboxtext",
            "find-icon",
            "refresh-icon",
            "info-icon",
            "connect-icon",
            "list-icon",
            "preferences_desktop_image",
        ):
            self.assertIn(object_id, widget_ids)
        self.assertNotIn("GameIcon_CellRenderer", widget_ids)
        self.assertNotIn("server-list-store", widget_ids)

        undeclared = sorted(
            (element.get("class"), element.get("id"))
            for element in widgets
            if not self.declares_visible(element)
        )
        self.assertEqual(undeclared, [])


    def test_serverinfo_dialog_declares_its_deliberate_visibilities(self):
        """The dialog's own visibility decisions live in the document too.

        Its game-id row exists only for the dialog's connect handler to read
        the selected server's game back, so it is declared hidden rather than
        left to a default; the player view is declared visible because no
        code shows it.
        """
        from obozrenie import resources

        document = ET.fromstring(resources.read_text("ui", "obozrenie_gtk.ui"))
        dialog = document.find("./object[@id='serverinfo-dialog']")
        self.assertIsNotNone(dialog, "the server information dialog is gone")
        declared = {}
        for element in dialog.findall(".//object"):
            for candidate in element.findall("property"):
                if candidate.get("name", "").replace("-", "_") == "visible":
                    declared[element.get("id")] = candidate.text
        self.assertEqual(
            {
                name: declared.get(name)
                for name in (
                    "serverinfo-gameid-label",
                    "serverinfo-gameid-data",
                    "serverinfo-players-scrolledview",
                )
            },
            {
                "serverinfo-gameid-label": "False",
                "serverinfo-gameid-data": "False",
                "serverinfo-players-scrolledview": "True",
            },
        )


class BuilderLocalizationTests(unittest.TestCase):
    def test_source_markers_cover_only_visible_builder_copy(self):
        from obozrenie import resources

        expected = {
            "obozrenie_gtk.ui": {
                ("property", "Obozrenie"),
                ("property", "Servers"),
                ("property", "Welcome"),
                ("property", "Loading"),
                ("property", "Error"),
            },
            "obozrenie_gtk_appmenu.ui": {
                ("attribute", "About"),
                ("attribute", "Quit"),
            },
        }
        for filename, expected_messages in expected.items():
            with self.subTest(filename=filename):
                root = ET.fromstring(resources.read_text("ui", filename))
                actual_messages = {
                    (element.tag, element.text)
                    for element in root.iter()
                    if element.get("translatable") == "yes"
                }
                self.assertEqual(actual_messages, expected_messages)

    def load_documents(self, translations):
        builder = XMLRecordingBuilder()
        gtk_module.load_builder_resources(builder, translations)
        self.assertEqual(len(builder.documents), 2)
        return builder.documents

    def property_text(self, documents, object_id, property_name):
        for document in documents:
            widget = document.find(f".//object[@id='{object_id}']")
            if widget is None:
                continue
            prop = widget.find(f"property[@name='{property_name}']")
            if prop is not None:
                return prop.text
        self.fail(f"missing {object_id}.{property_name}")

    def appmenu_labels(self, documents):
        return {
            attribute.text
            for document in documents
            for attribute in document.findall(
                ".//attribute[@name='label']"
            )
        }

    def assert_translation_markers_removed(self, documents):
        marked = [
            element
            for document in documents
            for element in document.iter()
            if "translatable" in element.attrib
        ]
        self.assertEqual(marked, [])

    def test_explicit_russian_is_applied_before_main_and_appmenu_load(self):
        from obozrenie import i18n

        documents = self.load_documents(i18n.translation(["ru_RU"]))

        self.assertEqual(
            self.property_text(
                documents,
                "ServerList_Main_Label",
                "label",
            ),
            "Серверы",
        )
        self.assertIn("О программе", self.appmenu_labels(documents))
        self.assert_translation_markers_removed(documents)

    def test_no_argument_builder_uses_the_selected_python_catalog(self):
        from obozrenie import i18n

        class SelectedCatalog:
            def __init__(self, catalog):
                self.catalog = catalog
                self.messages = []

            def gettext(self, message):
                self.messages.append(message)
                return self.catalog.gettext(message)

        original_catalog = i18n._translations
        original_gettext = i18n._
        selected = SelectedCatalog(i18n.translation(["ru_RU"]))
        with (
            mock.patch.object(i18n, "_translations", selected),
            mock.patch.object(i18n, "_", selected.gettext),
        ):
            self.assertIs(i18n._.__self__, i18n._translations)
            builder = XMLRecordingBuilder()
            gtk_module.load_builder_resources(builder)
            documents = builder.documents

            self.assertEqual(len(documents), 2)
            self.assertEqual(
                self.property_text(
                    documents,
                    "ServerList_Main_Label",
                    "label",
                ),
                "Серверы",
            )
            self.assertIn("О программе", self.appmenu_labels(documents))
            self.assertEqual(i18n._("Connect"), "Подключиться")
            self.assert_translation_markers_removed(documents)
            self.assertTrue(
                {"Servers", "About", "Connect"}.issubset(selected.messages)
            )

        self.assertIs(i18n._translations, original_catalog)
        self.assertIs(i18n._, original_gettext)

    def test_c_translation_keeps_source_builder_strings(self):
        documents = self.load_documents(gettext.NullTranslations())

        self.assertEqual(
            self.property_text(
                documents,
                "ServerList_Main_Label",
                "label",
            ),
            "Servers",
        )
        self.assertIn("About", self.appmenu_labels(documents))
        self.assert_translation_markers_removed(documents)


class _StoreOnlyBuilder(gtk_module.Gtk.Builder):
    """A real GtkBuilder that instantiates only the delivered list stores.

    The delivered document also declares the main window, so building every
    object in it would construct a toplevel. Naming the two stores keeps the
    real GtkListStore implementation and builds nothing else.
    """

    STORE_IDS = ("player-list-store", "server-list-store")

    def add_from_string(self, document):
        return self.add_objects_from_string(document, list(self.STORE_IDS))


class GtkIntegerModelTests(unittest.TestCase):
    """The delivered `gint` columns hold every value the models admit.

    `Player` and `Server` reject rather than clamp, so their declared domain
    has to be exactly what the packaged GtkListStore columns can store. These
    tests read the domain off the models and prove it against the real stores.
    A `None` score is the one admitted value a `gint` column cannot represent
    as itself; GTK stores it as 0, which is pinned below rather than assumed.
    """

    def gint_bounds(self):
        """Return the model domain the delivered `gint` columns must hold."""
        bounds = (models._GINT_MIN, models._GINT_MAX)
        self.assertEqual(bounds, (-2147483648, 2147483647))
        return bounds

    def packaged_stores(self):
        """Return the delivered player and server stores, and their builder."""
        builder = _StoreOnlyBuilder()
        gtk_module.load_builder_resources(builder)
        player_store, server_store = (
            builder.get_object(store_id)
            for store_id in _StoreOnlyBuilder.STORE_IDS
        )
        # The record fields live in these leading columns, so pin the
        # delivered types the rows below are written against.
        self.assertEqual(
            self.column_types(player_store)[:3],
            ["gchararray", "gint", "gint"],
        )
        self.assertEqual(
            self.column_types(server_store)[:5],
            ["gchararray", "gboolean", "gint", "gint", "gint"],
        )
        return builder, (player_store, server_store)

    @staticmethod
    def column_types(store):
        """Return the delivered column type names, in column order."""
        return [
            store.get_column_type(index).name
            for index in range(store.get_n_columns())
        ]

    @classmethod
    def neutral_row(cls, store):
        """Build a full-width in-domain row for one delivered store."""
        defaults = {"gchararray": "", "gboolean": False, "gint": 0}
        return [defaults.get(name) for name in cls.column_types(store)]

    def test_packaged_builder_list_stores_accept_model_boundary_rows(self):
        floor, ceiling = self.gint_bounds()
        builder, (player_store, server_store) = self.packaged_stores()

        players = (
            Player("floor", score=floor, ping=floor),
            Player("ceiling", score=ceiling, ping=ceiling),
            # An unscored player is what the fill path really delivers to the
            # `gint` score column; GTK has no null there and stores it as 0.
            Player("unscored", score=None, ping=0),
        )
        # Counts stay nonnegative, so only ping reaches the signed floor.
        servers = (
            Server(
                endpoint=Endpoint("floor.example", 1),
                game_id="g",
                player_count=0,
                player_limit=0,
                ping=floor,
            ),
            Server(
                endpoint=Endpoint("ceiling.example", 2),
                game_id="g",
                player_count=ceiling,
                player_limit=ceiling,
                ping=ceiling,
            ),
        )

        for player in players:
            player_store.append([player.name, player.score, player.ping])
        for server in servers:
            # Only the record's own columns matter here; the icon and filter
            # columns the UI derives keep their in-domain defaults.
            row = self.neutral_row(server_store)
            row[0] = str(server.endpoint)
            row[1] = server.password
            row[2] = server.player_count
            row[3] = server.player_limit
            row[4] = server.ping
            server_store.append(row)

        self.assertEqual(
            [tuple(row)[:3] for row in player_store],
            [
                ("floor", floor, floor),
                ("ceiling", ceiling, ceiling),
                ("unscored", 0, 0),
            ],
        )
        self.assertEqual(
            [tuple(row)[2:5] for row in server_store],
            [(0, 0, floor), (ceiling, ceiling, ceiling)],
        )
        # The stores are the only objects built, so no window exists to show.
        self.assertEqual(
            {type(built) for built in builder.get_objects()},
            {gtk_module.Gtk.ListStore},
        )
        self.assertIsNone(builder.get_object("Main_Window"))

    def test_packaged_builder_list_stores_reject_values_the_models_reject(self):
        floor, ceiling = self.gint_bounds()
        _builder, (player_store, server_store) = self.packaged_stores()
        cases = (
            ("player score", player_store, 1),
            ("player ping", player_store, 2),
            ("server player_count", server_store, 2),
            ("server player_limit", server_store, 3),
            ("server ping", server_store, 4),
        )

        for label, store, column in cases:
            for value in (floor - 1, ceiling + 1):
                with self.subTest(label=label, value=value):
                    row = self.neutral_row(store)
                    row[column] = value
                    with self.assertRaises(OverflowError):
                        store.append(row)

        # Every rejection above left both stores exactly as they were.
        self.assertEqual(len(player_store), 0)
        self.assertEqual(len(server_store), 0)


class LaunchErrorTests(unittest.TestCase):
    def make_actions(self, core):
        actions = gtk_window.MainWindow.__new__(gtk_window.MainWindow)
        actions.core = core
        actions.gtk_widgets = {"main-window": object()}
        return actions

    def test_worker_failure_schedules_sanitized_dialog_on_main_loop(self):
        password = "open sesame"
        core = LaunchCoreStub()
        actions = self.make_actions(core)
        actions.show_error_dialog = mock.Mock()

        with mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            actions.cb_server_connect(
                "g",
                "example.org:30000",
                password,
            )
            self.assertIsNotNone(
                core.callback,
                "GTK must give Core a launch completion callback",
            )
            core.callback(OSError(f"client rejected password {password}"))

        actions.show_error_dialog.assert_not_called()
        idle_add.assert_called_once()
        scheduled, message = idle_add.call_args.args
        self.assertIs(scheduled, actions.show_error_dialog)
        self.assertNotIn(password, message)
        self.assertIn("***", message)

        scheduled(message)
        actions.show_error_dialog.assert_called_once_with(message)

    def test_successful_worker_callback_does_not_schedule_a_dialog(self):
        core = LaunchCoreStub()
        actions = self.make_actions(core)
        actions.show_error_dialog = mock.Mock()

        with mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            actions.cb_server_connect("g", "example.org:30000", "")
            self.assertIsNotNone(
                core.callback,
                "GTK must give Core a launch completion callback",
            )
            core.callback(None)

        idle_add.assert_not_called()
        actions.show_error_dialog.assert_not_called()

    def test_synchronous_validation_failure_uses_scheduled_error_path(self):
        password = "open sesame"
        error = ValueError(f"invalid endpoint containing {password}")
        core = mock.Mock()
        core.start_game.side_effect = error
        actions = self.make_actions(core)
        actions.show_error_dialog = mock.Mock()

        with mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            try:
                actions.cb_server_connect("g", "", password)
            except ValueError:
                self.fail("GTK must render synchronous launch validation errors")

        actions.show_error_dialog.assert_not_called()
        idle_add.assert_called_once()
        scheduled, message = idle_add.call_args.args
        self.assertIs(scheduled, actions.show_error_dialog)
        self.assertNotIn(password, message)

    def test_error_dialog_is_reusable_modal_and_parented(self):
        self.assertTrue(
            hasattr(gtk_window.MainWindow, "show_error_dialog"),
            "MainWindow must expose one reusable error-dialog method",
        )
        actions = self.make_actions(LaunchCoreStub())
        dialog = mock.Mock()

        with mock.patch.object(
            gtk_module.Gtk,
            "MessageDialog",
            return_value=dialog,
        ) as message_dialog:
            keep_source = actions.show_error_dialog("Launch failed")

        kwargs = message_dialog.call_args.kwargs
        self.assertIs(
            kwargs["transient_for"],
            actions.gtk_widgets["main-window"],
        )
        self.assertIs(kwargs["modal"], True)
        self.assertEqual(kwargs["message_type"], gtk_module.Gtk.MessageType.ERROR)
        self.assertEqual(kwargs["buttons"], gtk_module.Gtk.ButtonsType.CLOSE)
        self.assertEqual(kwargs["text"], "Launch failed")
        dialog.run.assert_called_once_with()
        dialog.destroy.assert_called_once_with()
        self.assertIs(keep_source, False)


class AboutDialogTests(unittest.TestCase):
    def test_about_dialog_always_uses_the_packaged_logo_pixbuf(self):
        actions = gtk_window.MainWindow.__new__(gtk_window.MainWindow)
        actions.logo = object()
        dialog = mock.Mock()

        with mock.patch.object(
            gtk_module.Gtk,
            "AboutDialog",
            return_value=dialog,
        ) as about_dialog:
            actions.cb_about(None, None, object())

        self.assertIs(about_dialog.call_args.kwargs["logo"], actions.logo)
        self.assertNotIn("logo_icon_name", about_dialog.call_args.kwargs)
        dialog.run.assert_called_once_with()
        dialog.destroy.assert_called_once_with()


class ActionSensitivityTests(unittest.TestCase):
    @staticmethod
    def make_game_table(servers=()):
        table = game_table.GameTable({
            "browser": {
                "name": "Browser Game",
                "adapter": "qstat",
                "launch_pattern": "quake",
                "settings": [],
            },
            "connect": {
                "name": "Connect Game",
                "adapter": "qstat",
                "launch_pattern": "quake",
                "settings": [],
            },
        })
        servers = list(servers)
        if servers:
            token = table.begin_query("connect")
            table.finish_query("connect", token, servers)
        return table

    def derive(self, **state):
        return gtk_window.derive_action_sensitivity(**state)

    def test_empty_and_independent_selections_gate_their_own_actions(self):
        cases = (
            (
                "empty",
                {
                    "browser_game_valid": False,
                    "connect_game_valid": False,
                    "endpoint_valid": False,
                    "query_status": None,
                    "selected_server_present": False,
                },
                {
                    "preferences": False,
                    "refresh": False,
                    "connect": False,
                    "info": False,
                },
            ),
            (
                "browser only",
                {
                    "browser_game_valid": True,
                    "connect_game_valid": False,
                    "endpoint_valid": False,
                    "query_status": game_table.QueryStatus.EMPTY,
                    "selected_server_present": False,
                },
                {
                    "preferences": True,
                    "refresh": True,
                    "connect": False,
                    "info": False,
                },
            ),
            (
                "connect only",
                {
                    "browser_game_valid": False,
                    "connect_game_valid": True,
                    "endpoint_valid": True,
                    "query_status": None,
                    "selected_server_present": True,
                },
                {
                    "preferences": False,
                    "refresh": False,
                    "connect": True,
                    "info": True,
                },
            ),
        )
        for label, state, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(self.derive(**state), expected)

    def test_working_query_disables_only_refresh_for_otherwise_valid_state(self):
        self.assertEqual(
            self.derive(
                browser_game_valid=True,
                connect_game_valid=True,
                endpoint_valid=True,
                query_status=game_table.QueryStatus.WORKING,
                selected_server_present=True,
            ),
            {
                "preferences": True,
                "refresh": False,
                "connect": True,
                "info": True,
            },
        )

    def test_info_requires_a_matching_server_but_connect_does_not(self):
        self.assertEqual(
            self.derive(
                browser_game_valid=True,
                connect_game_valid=True,
                endpoint_valid=True,
                query_status=game_table.QueryStatus.READY,
                selected_server_present=False,
            ),
            {
                "preferences": True,
                "refresh": True,
                "connect": True,
                "info": False,
            },
        )

    def make_actions(self, host="example.org:30000", servers=()):
        table = self.make_game_table(servers)
        actions = gtk_window.MainWindow.__new__(gtk_window.MainWindow)
        actions.core = RecordingCore(table)
        actions.settings = SimpleNamespace(settings_table={
            "common": {
                "selected-game-browser": "browser",
                "selected-game-connect": "connect",
                "server-host": host,
                "game-listview": True,
            },
        })
        actions.gtk_widgets = {
            "main-window": Widget(),
            "game-preferences-button": Widget(),
            "action-update-button": Widget(),
            "action-connect-button": Widget(),
            "action-info-button": Widget(),
            "server-connect-game": Widget("connect"),
            "server-connect-host": Widget(host),
            "server-connect-pass": Widget(),
        }
        return actions

    def test_controller_applies_all_actions_and_uses_canonical_endpoint_rules(self):
        server = Server(
            endpoint=Endpoint("example.org", 30000),
            game_id="connect",
        )
        actions = self.make_actions(host="example.org", servers=[server])

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            sensitivity = actions.update_action_sensitivity()

        self.assertEqual(sensitivity, {
            "preferences": True,
            "refresh": True,
            "connect": False,
            "info": False,
        })
        self.assertEqual(
            {
                name: actions.gtk_widgets[widget].properties["sensitive"]
                for name, widget in {
                    "preferences": "game-preferences-button",
                    "refresh": "action-update-button",
                    "connect": "action-connect-button",
                    "info": "action-info-button",
                }.items()
            },
            sensitivity,
        )

        actions.gtk_widgets["server-connect-host"].value = str(server.endpoint)
        actions.core.game_table.begin_query("browser")
        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            sensitivity = actions.update_action_sensitivity()

        self.assertEqual(sensitivity, {
            "preferences": True,
            "refresh": False,
            "connect": True,
            "info": True,
        })

    def test_both_connect_buttons_read_the_current_transient_password(self):
        server = Server(
            endpoint=Endpoint("example.org", 30000),
            game_id="connect",
        )
        actions = self.make_actions(servers=[server])
        actions.gtk_widgets.update({
            "serverinfo-gameid": Widget("connect"),
            "serverinfo-host": Widget(str(server.endpoint)),
        })

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            actions.gtk_widgets["server-connect-pass"].value = "first secret"
            actions.cb_connect_button_clicked()
            actions.gtk_widgets["server-connect-pass"].value = "second secret"
            actions.cb_serverinfo_connect_button_clicked()

        self.assertEqual(
            [launch[:3] for launch in actions.core.launches],
            [
                ("connect", "example.org:30000", "first secret"),
                ("connect", "example.org:30000", "second secret"),
            ],
        )
        self.assertEqual(actions.settings.settings_table["common"], {
            "selected-game-browser": "browser",
            "selected-game-connect": "connect",
            "server-host": "example.org:30000",
            "game-listview": True,
        })

    def test_connect_handlers_reject_invalid_endpoint_and_stale_server(self):
        actions = self.make_actions(host="example.org")
        actions.gtk_widgets.update({
            "serverinfo-gameid": Widget("connect"),
            "serverinfo-host": Widget("example.org:30000"),
        })

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            actions.cb_connect_button_clicked()
            actions.cb_serverinfo_connect_button_clicked()
            actions.settings.settings_table["common"][
                "selected-game-connect"
            ] = "retired"
            actions.settings.settings_table["common"][
                "server-host"
            ] = "example.org:30000"
            actions.cb_connect_button_clicked()

        self.assertEqual(actions.core.launches, [])

    def test_browser_handlers_reject_invalid_game_and_working_refresh(self):
        actions = self.make_actions()
        events = []
        actions.set_loading_state = lambda state: events.append(("loading", state))
        actions.set_game_state = lambda game, state: events.append(
            ("game", game, state)
        )

        actions.settings.settings_table["common"][
            "selected-game-browser"
        ] = "retired"
        with mock.patch.object(
            gtk_window.gtk_preferences, "PreferencesDialog"
        ) as dialog:
            actions.cb_game_preferences_button_clicked()
            actions.cb_update_button_clicked()

        dialog.assert_not_called()
        self.assertEqual(actions.core.refreshes, [])
        self.assertEqual(events, [])

        actions.settings.settings_table["common"][
            "selected-game-browser"
        ] = "browser"
        actions.core.game_table.begin_query("browser")
        actions.gtk_widgets["action-update-button"].properties[
            "sensitive"
        ] = True
        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            actions.cb_update_button_clicked()

        self.assertEqual(actions.core.refreshes, [])
        self.assertEqual(events, [])
        self.assertIs(
            actions.gtk_widgets["action-update-button"].properties["sensitive"],
            False,
        )

    def test_info_handler_rejects_an_invalid_game(self):
        actions = self.make_actions()
        actions.settings.settings_table["common"][
            "selected-game-connect"
        ] = "retired"
        actions.gtk_widgets["server-connect-game"].value = "retired"
        dialog = Widget()
        dialog.run = lambda: self.fail("invalid game must not open info")
        actions.gtk_widgets["serverinfo-dialog"] = dialog

        actions.cb_info_button_clicked()

    def make_info_actions(self, players):
        """Build an info handler with everything that dialog touches."""
        server = Server(
            endpoint=Endpoint("example.org", 30000),
            game_id="connect",
            players=players,
        )
        actions = self.make_actions(host="example.org:30000", servers=[server])
        actions.player_list_model_format = ("name", "score", "ping")
        rows = []
        model = SimpleNamespace(clear=rows.clear, append=rows.append)
        dialog = Widget()
        dialog.run = lambda: None
        dialog.hide = lambda: None
        actions.gtk_widgets.update({
            "serverinfo-dialog": dialog,
            "player-list-model": model,
        })
        actions.gtk_widgets.update({
            name: Widget()
            for name in (
                "serverinfo-name",
                "serverinfo-host",
                "serverinfo-game",
                "serverinfo-gameid",
                "serverinfo-terrain",
                "serverinfo-players",
                "serverinfo-ping",
            )
        })
        return actions, model, rows

    def test_info_fills_every_player_including_one_without_a_score(self):
        actions, _model, rows = self.make_info_actions(
            (Player("first", 7, 20), Player("second", None, 30))
        )

        actions.cb_info_button_clicked()

        self.assertEqual(rows, [["first", 7, 20], ["second", None, 30]])

    def test_info_player_fill_does_not_hide_a_programming_error(self):
        # A guard around the fill would turn a wrong widget id or a changed
        # model format into a silently empty player list rather than a
        # failure anyone could see.
        actions, model, _rows = self.make_info_actions((Player("first"),))
        broken = RuntimeError("player model rejected the row")
        model.append = mock.Mock(side_effect=broken)

        with self.assertRaises(RuntimeError) as raised:
            actions.cb_info_button_clicked()

        self.assertIs(raised.exception, broken)


class SettingsBindingTests(unittest.TestCase):
    def make_window(self):
        window = gtk_window.MainWindow.__new__(gtk_window.MainWindow)
        window.settings = SimpleNamespace(settings_table={
            "common": {
                "selected-game-connect": "connect",
                "selected-game-browser": "browser",
                "server-host": "example.org:30000",
                "game-listview": True,
            },
        })
        window.gtk_widgets = {
            "server-connect-game": Widget("connect"),
            "game-treeview": Widget(("browser", "Browser Game", None, None)),
            "server-connect-host": Widget("example.org:30000"),
            "game-view-togglebutton": Widget(True),
        }
        return window

    def test_bind_settings_shows_every_common_option_and_follows_changes(self):
        window = self.make_window()
        shown = []
        bound = []
        with mock.patch.object(
            gtk_window.gtk_helpers, "set_widget_value",
            side_effect=lambda widget, value, treeview_colnum=0: shown.append(
                (widget.value, value, treeview_colnum)),
        ), mock.patch.object(
            gtk_window.gtk_helpers, "bind_widget_to_callback",
            side_effect=lambda widget, callback: bound.append((widget.value, callback)),
        ):
            window.bind_settings()

        self.assertEqual(shown, [
            ("connect", "connect", 0),
            (("browser", "Browser Game", None, None), "browser", 0),
            ("example.org:30000", "example.org:30000", 0),
            (True, True, 0),
        ])
        self.assertEqual(
            [callback for _value, callback in bound],
            [window.update_settings_table] * 4,
        )

    def test_update_settings_table_stores_widget_values_as_text(self):
        window = self.make_window()
        with mock.patch.object(
            gtk_window.gtk_helpers, "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            window.update_settings_table()

        self.assertEqual(window.settings.settings_table["common"], {
            "selected-game-connect": "connect",
            "selected-game-browser": "browser",
            "server-host": "example.org:30000",
            "game-listview": "True",
        })

    def test_update_settings_table_records_an_empty_tree_selection_as_none(self):
        window = self.make_window()
        window.gtk_widgets["game-treeview"].value = None
        with mock.patch.object(
            gtk_window.gtk_helpers, "get_widget_value",
            side_effect=lambda widget: widget.value,
        ):
            window.update_settings_table()

        self.assertEqual(
            window.settings.settings_table["common"]["selected-game-browser"],
            "None",
        )

    def test_widget_table_and_persisted_option_table_cover_the_same_keys(self):
        """`bind_settings` looks up every `COMMON_OPTIONS` key in
        `COMMON_OPTION_WIDGETS`; a key present in only one table is either an
        unshown persisted option or a `KeyError` in `bind_settings`."""
        self.assertEqual(
            set(gtk_window.COMMON_OPTION_WIDGETS),
            set(settings_module.COMMON_OPTIONS["common"]),
        )


class ServerFilterTests(unittest.TestCase):
    COLUMNS = gtk_window.SERVER_LIST_COLUMNS

    def make_window(self, **filters):
        window = gtk_window.MainWindow.__new__(gtk_window.MainWindow)
        window._filter_values = filters
        return window

    def row(self, **fields):
        values = dict.fromkeys(self.COLUMNS)
        values.update(fields)
        return [values[column] for column in self.COLUMNS]

    def matches(self, window, **fields):
        row = self.row(**fields)
        return window.server_filter_func({0: row}, 0)

    def test_unset_filters_pass_every_row(self):
        window = self.make_window(game_mod=None, game_type="", terrain="None", ping=0.0, secure="None", full=False, empty=False, password=False)
        self.assertTrue(self.matches(window, game_mod=None, ping=9999, secure=False, full=True, empty=True, password=True))

    def test_substring_filters_require_the_text_and_reject_missing_values(self):
        window = self.make_window(game_mod="ctf")
        self.assertTrue(self.matches(window, game_mod="instactf"))
        self.assertFalse(self.matches(window, game_mod="dm"))
        self.assertFalse(self.matches(window, game_mod=None))

    def test_ping_ceiling_applies_only_when_nonzero(self):
        self.assertTrue(self.matches(self.make_window(ping=50.0), ping=50))
        self.assertFalse(self.matches(self.make_window(ping=50.0), ping=51))
        self.assertTrue(self.matches(self.make_window(ping=0.0), ping=9999))

    def test_secure_filter_matches_the_chosen_boolean_exactly(self):
        self.assertTrue(self.matches(self.make_window(secure="True"), secure=True))
        self.assertFalse(self.matches(self.make_window(secure="True"), secure=False))
        self.assertTrue(self.matches(self.make_window(secure="False"), secure=False))

    def test_checked_hide_filters_reject_only_true_rows(self):
        window = self.make_window(full=True, empty=True, password=True)
        self.assertTrue(self.matches(window, full=False, empty=False, password=None))
        self.assertFalse(self.matches(window, full=True))
        self.assertFalse(self.matches(window, empty=True))
        self.assertFalse(self.matches(window, password=True))


class PreferencesSchemaTests(unittest.TestCase):
    def test_packaged_game_defaults_keep_every_declared_nickname(self):
        from obozrenie import resources

        declared = gtk_preferences.game_options()

        self.assertIn("nickname", declared)
        self.assertEqual(declared["nickname"]["name"], i18n._("Nickname"))
        self.assertEqual(
            declared["nickname"]["description"],
            i18n._("Your nickname."),
        )

        game_lists = resources.load_toml(
            "settings", "internal", "game_lists.toml"
        )
        packaged = resources.load_toml("settings", "defaults", "games.toml")
        nickname_games = {
            game
            for game, info in game_lists.items()
            if "nickname" in info["settings"]
        }

        self.assertTrue(nickname_games)
        for game in nickname_games:
            self.assertIn("nickname", packaged[game])


class QueryStateUITests(unittest.TestCase):
    @staticmethod
    def make_actions():
        actions = ActionSensitivityTests().make_actions()
        actions.game_list_model_format = (
            "game_id",
            "name",
            "game_icon",
            "status_icon",
        )
        actions.gtk_widgets.update({
            "server-list-sort": Widget(),
            "serverlist-view": Widget(),
            "serverlist-notebook": Widget(),
            "error-message-content-label": Widget(),
            "game-list-model": [
                ["browser", "Browser Game", None, "browser-idle"],
                ["connect", "Connect Game", None, "connect-idle"],
            ],
        })
        actions.gtk_widgets["serverlist-notebook"].properties["page"] = (
            "current-page"
        )
        actions.gtk_widgets["error-message-content-label"].properties[
            "label"
        ] = "existing error"
        actions.serverlist_notebook_pages = {
            "servers": "servers-page",
            "welcome": "welcome-page",
            "loading": "loading-page",
            "error": "error-page",
        }
        return actions

    @staticmethod
    def widget_values(widget):
        return widget.value

    def test_admitted_working_query_disables_refresh_and_renders_working(self):
        actions = self.make_actions()

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=self.widget_values,
        ):
            actions.cb_update_button_clicked()

        self.assertIs(
            actions.core.game_table.get_query_status("browser"),
            game_table.QueryStatus.WORKING,
        )
        self.assertEqual(
            actions.gtk_widgets["serverlist-notebook"].properties["page"],
            "loading-page",
        )
        self.assertEqual(
            actions.gtk_widgets["game-list-model"][0][3],
            "emblem-synchronizing-symbolic",
        )
        self.assertIs(
            actions.gtk_widgets["action-update-button"].properties[
                "sensitive"
            ],
            False,
        )

    def test_rejected_racing_duplicate_preserves_presentation_and_recomputes_sensitivity(self):
        actions = self.make_actions()

        class RacingDuplicateCore(RecordingCore):
            def update_server_list(self, game, stat_callback=None):
                self.refreshes.append((game, stat_callback))
                self.game_table.begin_query(game)
                return False

        actions.core = RacingDuplicateCore(actions.core.game_table)
        stale_sensitivities = {
            "game-preferences-button": False,
            "action-update-button": True,
            "action-connect-button": False,
            "action-info-button": True,
        }
        for widget_id, sensitive in stale_sensitivities.items():
            actions.gtk_widgets[widget_id].properties["sensitive"] = sensitive

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=self.widget_values,
        ):
            actions.cb_update_button_clicked()

        self.assertEqual(len(actions.core.refreshes), 1)
        self.assertIs(
            actions.core.game_table.get_query_status("browser"),
            game_table.QueryStatus.WORKING,
        )
        self.assertEqual(
            actions.gtk_widgets["serverlist-notebook"].properties["page"],
            "current-page",
        )
        self.assertEqual(
            actions.gtk_widgets["game-list-model"][0][3],
            "browser-idle",
        )
        self.assertEqual(
            {
                widget_id: actions.gtk_widgets[widget_id].properties[
                    "sensitive"
                ]
                for widget_id in stale_sensitivities
            },
            {
                "game-preferences-button": True,
                "action-update-button": False,
                "action-connect-button": True,
                "action-info-button": False,
            },
        )

    def test_synchronous_terminal_admission_never_renders_working(self):
        actions = self.make_actions()
        safe_error = "Safe synchronous query failure."

        class SynchronousFailureCore(RecordingCore):
            def update_server_list(self, game, stat_callback=None):
                self.refreshes.append((game, stat_callback))
                token = self.game_table.begin_query(game)
                if token is not None:
                    self.game_table.fail_query(game, token, safe_error)
                    if stat_callback is not None:
                        stat_callback(game)
                return token is not None

        actions.core = SynchronousFailureCore(actions.core.game_table)
        actions.gtk_widgets["action-update-button"].properties[
            "sensitive"
        ] = False

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=self.widget_values,
        ), mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            actions.cb_update_button_clicked()

        idle_add.assert_called_once_with(actions.show_game_page, "browser")
        self.assertIs(
            actions.core.game_table.get_query_status("browser"),
            game_table.QueryStatus.ERROR,
        )
        self.assertEqual(
            actions.gtk_widgets["serverlist-notebook"].properties["page"],
            "current-page",
        )
        self.assertEqual(
            actions.gtk_widgets["game-list-model"][0][3],
            "browser-idle",
        )
        self.assertIs(
            actions.gtk_widgets["action-update-button"].properties[
                "sensitive"
            ],
            True,
        )

    def test_worker_completion_only_schedules_exact_error_rendering(self):
        actions = self.make_actions()
        safe_error = "Safe stored query failure."
        token = actions.core.game_table.begin_query("browser")
        actions.core.game_table.fail_query("browser", token, safe_error)
        actions.gtk_widgets["action-update-button"].properties[
            "sensitive"
        ] = False

        with mock.patch.object(
            gtk_module.gtk_helpers,
            "get_widget_value",
            side_effect=self.widget_values,
        ), mock.patch.object(gtk_module.GLib, "idle_add") as idle_add:
            actions.cb_update_server_list("browser")

            self.assertEqual(
                actions.gtk_widgets["serverlist-notebook"].properties["page"],
                "current-page",
            )
            self.assertEqual(
                actions.gtk_widgets["error-message-content-label"].properties[
                    "label"
                ],
                "existing error",
            )
            self.assertIs(
                actions.gtk_widgets["action-update-button"].properties[
                    "sensitive"
                ],
                False,
            )

            idle_add.assert_called_once_with(actions.show_game_page, "browser")
            scheduled, game = idle_add.call_args.args
            scheduled(game)

        self.assertEqual(
            actions.gtk_widgets["error-message-content-label"].properties[
                "label"
            ],
            safe_error,
        )
        self.assertEqual(
            actions.gtk_widgets["serverlist-notebook"].properties["page"],
            "error-page",
        )
        self.assertIs(
            actions.gtk_widgets["action-update-button"].properties[
                "sensitive"
            ],
            True,
        )

    def test_controller_maps_the_shipped_error_content_label(self):
        builder = Builder()
        table = game_table.GameTable({})
        actions = gtk_window.MainWindow(
            SimpleNamespace(),
            builder,
            SimpleNamespace(game_table=table),
            SimpleNamespace(settings_table={"common": {}}),
        )

        self.assertIn("error-message-content-label", actions.gtk_widgets)
        self.assertIs(
            actions.gtk_widgets["error-message-content-label"],
            builder.get_object("Error_Message_Content_Label"),
        )


class StartupWarningTests(unittest.TestCase):
    @staticmethod
    def make_settings(root):
        root = Path(root)
        bundled = {
            ("settings", "defaults", "settings.toml"): b"""[common]
selected-game-connect = "game"
selected-game-browser = "game"
server-host = ""
game-listview = true
""",
            ("settings", "defaults", "games.toml"): b"""[game]
path = "client"
master_uri = ["master://example.test:27950"]
""",
        }

        def load_resource(*parts):
            with io.BytesIO(bundled[tuple(parts)]) as table_file:
                return tomllib.load(table_file)

        table = game_table.GameTable({
            "game": {
                "name": "Game",
                "adapter": "qstat",
                "launch_pattern": "quake",
                "settings": ["path", "master_uri"],
            },
        })
        profile = root / "profile"
        profile.mkdir()
        settings = settings_module.Settings(
            table,
            str(profile),
            resource_loader=load_resource,
        )
        Path(settings.user_common_settings_path).write_text(
            '[common]\nsecret = "unterminated\n',
            encoding="utf-8",
        )
        Path(settings.user_game_settings_path).write_text(
            '[game]\nsecret = "unterminated\n',
            encoding="utf-8",
        )
        return settings

    @staticmethod
    def make_startup_app(settings):
        """One `on_startup` stand-in carrying the App's own recorder.

        The recorder is the method under test, so the stand-in binds the real
        one rather than a stub of it.
        """
        quits = []
        app = SimpleNamespace(
            window=mock.Mock(),
            settings=settings,
            handler_failure=None,
            status="starting",
            _pending_settings_warnings=[],
            quit=lambda: quits.append("quit"),
        )
        app._record_handler_failure = MethodType(
            gtk_module.App._record_handler_failure, app)
        return app, quits

    def test_startup_quits_when_its_own_first_log_line_fails(self):
        # The failure this recorder exists for is a closed stdout, so the
        # report it writes fails the same way the recorded failure did.
        # Reporting is best effort precisely so that `app.quit()` still runs.
        broken = OSError(errno.EBADF, "Bad file descriptor")
        settings = SimpleNamespace(load=mock.Mock())
        app, quits = self.make_startup_app(settings)

        with mock.patch.object(
            gtk_module.helpers, "debug_msg", side_effect=broken
        ):
            gtk_module.App.on_startup(app, app)

        self.assertEqual(quits, ["quit"])
        self.assertEqual(
            app.handler_failure,
            SafeDiagnostic("Unexpected OSError; exception details omitted."),
        )
        # The line that failed is the first one in the handler, so nothing
        # after it — the settings load included — was reached.
        settings.load.assert_not_called()

    def test_startup_records_the_settings_failure_its_report_could_not_write(self):
        # A report that cannot be written may neither replace the recorded
        # failure nor keep the application from quitting.
        load_error = PermissionError(13, "Permission denied")
        settings = SimpleNamespace(load=mock.Mock(side_effect=load_error))
        app, quits = self.make_startup_app(settings)
        written = []

        def fail_after_the_first_line(message):
            written.append(message)
            if len(written) > 1:
                raise OSError(errno.EBADF, "Bad file descriptor")

        with mock.patch.object(
            gtk_module.helpers,
            "debug_msg",
            side_effect=fail_after_the_first_line,
        ):
            gtk_module.App.on_startup(app, app)

        self.assertEqual(quits, ["quit"])
        self.assertEqual(
            app.handler_failure,
            SafeDiagnostic(
                "Unexpected PermissionError; exception details omitted."
            ),
        )
        self.assertEqual(len(written), 2)

    def test_startup_records_the_declared_diagnostic_for_an_unreadable_profile(self):
        # Handlers no longer re-raise, so the recorded diagnostic is the only
        # thing a user with an unreadable profile ever sees; it names what
        # went wrong without carrying the path the traceback used to print.
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(directory)
            unreadable = Path(settings.user_common_settings_path)
            os.chmod(unreadable, 0o000)
            try:
                with open(unreadable, "rb"):
                    pass
            except PermissionError:
                pass
            else:
                self.skipTest("this user can read a 0000 settings file")

            app, quits = self.make_startup_app(settings)
            with mock.patch.object(gtk_module.helpers, "debug_msg") as logged:
                gtk_module.App.on_startup(app, app)

        recorded = SafeDiagnostic("Settings file could not be read.")
        self.assertEqual(quits, ["quit"])
        self.assertEqual(app.handler_failure, recorded)
        self.assertIn(
            mock.call([gtk_module.GTK_MSG, recorded]),
            logged.call_args_list,
        )

    def test_recovery_warnings_finish_before_one_geoip_offer_and_admission_waits_for_completion(self):
        self.enterContext(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        self.enterContext(mock.patch.object(gtk_module.helpers, "debug_msg"))
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(directory)
            loaded_warnings = []
            load_count = 0
            real_load = settings.load

            def recording_load(*args, **kwargs):
                nonlocal load_count
                load_count += 1
                warnings = real_load(*args, **kwargs)
                loaded_warnings.extend(warnings)
                return warnings

            settings.load = recording_load
            window = mock.Mock()
            controller = mock.Mock()
            controller.filter_secure_list = ()
            controller.gtk_widgets = {
                "server-list-filter": mock.Mock(),
                "game-combobox": mock.Mock(),
                "filter-secure": mock.Mock(),
                "main-window": window,
            }
            events = []
            offers = []

            class RecordingCoordinator:
                """Accept every offer and hold its completion callback."""

                def offer(self, parent, finished_callback):
                    events.append(("offer", parent))
                    offers.append(finished_callback)
                    return True

            app = SimpleNamespace(
                window=controller,
                settings=settings,
                services=SimpleNamespace(
                    geoip_downloads=RecordingCoordinator(),
                ),
                _startup_dialogs_pending=False,
                builder=mock.Mock(),
                add_window=mock.Mock(),
                add_action=mock.Mock(),
                set_app_menu=mock.Mock(),
                quit=mock.Mock(),
            )

            with mock.patch.object(gtk_module, "Gio"), mock.patch.object(
                gtk_module.gtk_helpers,
                "set_widget_value",
            ):
                gtk_module.App.on_startup(app, app)

            self.assertEqual(len(loaded_warnings), 2)

            context = gtk_module.GLib.MainContext.new()
            sources = []
            overlap_scheduled = False

            def attach_idle(
                callback,
                *args,
                priority=gtk_module.GLib.PRIORITY_DEFAULT_IDLE,
            ):
                source = gtk_module.GLib.idle_source_new()
                source.set_priority(priority)
                source.set_callback(callback, *args)
                source_id = source.attach(context)
                sources.append(source)
                return source_id

            def reactivate_during_warning():
                events.append(("overlap-activation", window))
                gtk_module.App.on_activate(app, app)
                return False

            def show_error_dialog(warning):
                nonlocal overlap_scheduled
                events.append(("warning-start", warning))
                nested_loop = gtk_module.GLib.MainLoop.new(context, False)

                def stop_nested_loop():
                    nested_loop.quit()
                    return False

                if not overlap_scheduled:
                    overlap_scheduled = True
                    attach_idle(reactivate_during_warning)
                attach_idle(
                    stop_nested_loop,
                    priority=gtk_module.GLib.PRIORITY_LOW,
                )
                nested_loop.run()
                events.append(("warning-end", warning))
                return False

            controller.show_error_dialog.side_effect = show_error_dialog

            with mock.patch.object(
                gtk_module.GLib,
                "idle_add",
                side_effect=attach_idle,
            ):
                gtk_module.App.on_activate(app, app)
                self.assertEqual(app._pending_settings_warnings, [])
                self.assertEqual(events, [])
                while context.pending():
                    context.iteration(False)

                # The offer is in flight, so startup admission is still held
                # and a further activation only presents the window.
                self.assertTrue(app._startup_dialogs_pending)
                self.assertEqual(len(offers), 1)
                gtk_module.App.on_activate(app, app)
                while context.pending():
                    context.iteration(False)
                self.assertEqual(len(offers), 1)

                # Completion, not the window, releases startup admission.
                offers[0]()
                self.assertFalse(app._startup_dialogs_pending)

                gtk_module.App.on_activate(app, app)
                while context.pending():
                    context.iteration(False)

            self.assertEqual(load_count, 1)
            self.assertEqual(window.present.call_count, 4)
            self.assertEqual(window.show_all.call_count, 0)
            self.assertEqual(
                events,
                [
                    ("warning-start", loaded_warnings[0]),
                    ("overlap-activation", window),
                    ("warning-end", loaded_warnings[0]),
                    ("warning-start", loaded_warnings[1]),
                    ("warning-end", loaded_warnings[1]),
                    ("offer", window),
                    ("offer", window),
                ],
            )

    def test_on_startup_fills_game_store_before_load_and_binds_settings_before_connect_signals(self):
        """Pin the spec's on_startup order: `fill_game_store`, then
        `settings.load`, then `bind_settings`, then `builder.connect_signals`
        — each step only starts once the one before it has finished."""
        self.enterContext(mock.patch.object(gtk_module.helpers, "debug_msg"))
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(directory)
            real_load = settings.load
            counts_at_load_start = []

            def recording_load(*args, **kwargs):
                counts_at_load_start.append((
                    controller.fill_game_store.call_count,
                    controller.bind_settings.call_count,
                ))
                return real_load(*args, **kwargs)

            settings.load = recording_load

            controller = mock.Mock()
            controller.filter_secure_list = ()
            controller.gtk_widgets = {
                "server-list-filter": mock.Mock(),
                "game-combobox": mock.Mock(),
                "filter-secure": mock.Mock(),
                "main-window": mock.Mock(),
            }
            bind_settings_count_at_connect = []

            def record_bind_settings_count(*_args, **_kwargs):
                bind_settings_count_at_connect.append(
                    controller.bind_settings.call_count)

            app = SimpleNamespace(
                window=controller,
                settings=settings,
                builder=mock.Mock(),
                add_window=mock.Mock(),
                add_action=mock.Mock(),
                set_app_menu=mock.Mock(),
                quit=mock.Mock(),
            )
            app.builder.connect_signals.side_effect = record_bind_settings_count

            with mock.patch.object(gtk_module, "Gio"), mock.patch.object(
                gtk_module.gtk_helpers, "set_widget_value",
            ):
                gtk_module.App.on_startup(app, app)

        # `fill_game_store` had already run once, and `bind_settings` had
        # not run at all, by the time `settings.load` started.
        self.assertEqual(counts_at_load_start, [(1, 0)])
        # `bind_settings` had already run — exactly once — by the time
        # `connect_signals` fired, and never again after.
        self.assertEqual(bind_settings_count_at_connect, [1])
        self.assertEqual(controller.bind_settings.call_count, 1)
        ordered_calls = [
            name for name, _args, _kwargs in controller.mock_calls
            if name in ("fill_game_store", "bind_settings")
        ]
        self.assertEqual(ordered_calls, ["fill_game_store", "bind_settings"])

    def test_refused_offer_releases_startup_admission_at_once(self):
        window = mock.Mock()
        controller = mock.Mock()
        controller.gtk_widgets = {"main-window": window}
        offers = []

        class RefusingCoordinator:
            """Refuse every offer, exactly as a closed or loaded one does."""

            def offer(self, parent, finished_callback):
                offers.append((parent, finished_callback))
                return False

        app = SimpleNamespace(
            window=controller,
            services=SimpleNamespace(geoip_downloads=RefusingCoordinator()),
            _pending_settings_warnings=[],
            _startup_dialogs_pending=False,
        )
        queued = []

        with mock.patch.object(
            gtk_module.GLib,
            "idle_add",
            side_effect=lambda callback, *args: queued.append(
                (callback, args)
            ),
        ):
            gtk_module.App.on_activate(app, app)
            self.assertTrue(app._startup_dialogs_pending)
            for callback, args in queued:
                self.assertIs(callback(*args), False)
            self.assertFalse(app._startup_dialogs_pending)

            gtk_module.App.on_activate(app, app)
            for callback, args in queued[1:]:
                self.assertIs(callback(*args), False)

        self.assertEqual(len(offers), 2)
        controller.show_error_dialog.assert_not_called()
        self.assertEqual(window.present.call_count, 2)
        self.assertEqual(window.show_all.call_count, 0)

    def test_every_activation_presents_without_showing_hidden_descendants(self):
        window = mock.Mock()
        controller = mock.Mock()
        controller.gtk_widgets = {"main-window": window}

        class RecordingCoordinator:
            """Fail the assertions if a held activation reaches for another offer."""

            def __init__(self):
                self.offers = []

            def offer(self, parent, finished_callback):
                self.offers.append((parent, finished_callback))
                return True

        coordinator = RecordingCoordinator()
        app = SimpleNamespace(
            window=controller,
            services=SimpleNamespace(geoip_downloads=coordinator),
            _pending_settings_warnings=["unseen warning"],
            _startup_dialogs_pending=True,
        )

        idle_calls = []
        with mock.patch.object(
            gtk_module.GLib,
            "idle_add",
            side_effect=lambda callback, *args: idle_calls.append(
                (callback, args)
            ),
        ):
            for _ in range(3):
                gtk_module.App.on_activate(app, app)

        # One present() per activation. GtkBuilder-declared visibility owns
        # every widget's initial state, and show_all() is the one call that
        # would recursively override it and reveal hidden descendants, so
        # asserting it is never called is exactly the contract this test's
        # name claims.
        self.assertEqual(window.present.call_count, 3)
        self.assertEqual(window.show_all.call_count, 0)
        # Admission was already held, so no activation adds a second idle
        # dispatcher, shows another warning dialog, or accepts a second
        # GeoIP offer.
        self.assertEqual(idle_calls, [])
        controller.show_error_dialog.assert_not_called()
        self.assertEqual(coordinator.offers, [])
        self.assertTrue(app._startup_dialogs_pending)
        self.assertEqual(app._pending_settings_warnings, ["unseen warning"])


class ApplicationShutdownTests(unittest.TestCase):
    def test_app_carries_a_status_before_startup_can_fail(self):
        # `on_shutdown` reads `status`, and `on_startup` can fail on its very
        # first line — a closed stdout is enough — so the attribute cannot be
        # created there.
        with mock.patch.object(gtk_module, "MainWindow"), \
                mock.patch.object(gtk_module, "load_builder_resources"):
            app = gtk_module.App(mock.Mock(), mock.Mock(), mock.Mock())

        self.assertEqual(app.status, "starting")
        self.assertIsNone(app.handler_failure)

        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            app.on_shutdown(app)

        self.assertEqual(app.status, "start failed")
        app.services.begin_close.assert_called_once_with()

    @staticmethod
    def make_app(status, save):
        events = []

        def begin_close():
            events.append("begin-close")

        app = SimpleNamespace(
            status=status,
            handler_failure=None,
            settings=SimpleNamespace(save=save),
            services=SimpleNamespace(begin_close=begin_close),
        )
        # The handler reports through the App's own recorder, so the stand-in
        # carries exactly that method rather than a copy of it.
        app._record_handler_failure = MethodType(
            gtk_module.App._record_handler_failure, app)
        return app, events

    def test_shutdown_begins_service_close_even_when_settings_save_raises(self):
        # PyGObject prints and swallows whatever this handler raises, so a
        # failed save is recorded for the launcher to report rather than
        # raised into a dispatch that would turn it into a successful run.
        error = RuntimeError("settings save failed")
        saves = []

        def save():
            saves.append("save")
            raise error

        app, close_events = self.make_app("up", save)

        with mock.patch.object(gtk_module.helpers, "debug_msg") as debug_msg:
            gtk_module.App.on_shutdown(app, app)

        recorded = SafeDiagnostic(
            "Unexpected RuntimeError; exception details omitted."
        )
        self.assertEqual(app.handler_failure, recorded)
        self.assertIn(
            mock.call([gtk_module.GTK_MSG, recorded]),
            debug_msg.call_args_list,
        )
        self.assertEqual(saves, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "up")

    def test_shutdown_records_only_the_first_failure_it_reports(self):
        app, close_events = self.make_app(
            "up",
            mock.Mock(side_effect=RuntimeError("settings save failed")),
        )
        app.handler_failure = SafeDiagnostic("startup already failed")

        with mock.patch.object(gtk_module.helpers, "debug_msg") as debug_msg:
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(
            app.handler_failure,
            SafeDiagnostic("startup already failed"),
        )
        self.assertIn(
            mock.call([
                gtk_module.GTK_MSG,
                SafeDiagnostic(
                    "Unexpected RuntimeError; exception details omitted."
                ),
            ]),
            debug_msg.call_args_list,
        )
        self.assertEqual(close_events, ["begin-close"])

    def test_successful_shutdown_saves_settings_then_begins_service_close(self):
        events = []
        app, close_events = self.make_app(
            "up",
            lambda: events.append("save"),
        )

        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(events, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "shutting down")

    def test_a_failing_log_line_after_a_successful_save_is_not_a_failure(self):
        # Only the save is the run's outcome. The "Shutting down" line failing
        # on a closed stdout is what PyGObject prints and swallows, so it is
        # deliberately outside the recorded block: nothing is recorded and
        # `main()` still exits 0, while the services are closed either way.
        broken = OSError(errno.EBADF, "Bad file descriptor")
        saves = []
        app, close_events = self.make_app("up", lambda: saves.append("save"))

        with mock.patch.object(
            gtk_module.helpers, "debug_msg", side_effect=broken
        ):
            with self.assertRaises(OSError) as caught:
                gtk_module.App.on_shutdown(app, app)

        self.assertIs(caught.exception, broken)
        self.assertIsNone(app.handler_failure)
        self.assertEqual(saves, ["save"])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "shutting down")

    def test_failed_startup_shutdown_begins_service_close_without_saving(self):
        events = []
        app, close_events = self.make_app(
            "starting",
            lambda: events.append("save"),
        )

        with mock.patch.object(gtk_module.helpers, "debug_msg"):
            gtk_module.App.on_shutdown(app, app)

        self.assertEqual(events, [])
        self.assertEqual(close_events, ["begin-close"])
        self.assertEqual(app.status, "start failed")


class _FakeButton:
    """Stand in for one dialog action button."""

    def __init__(self, label, response):
        self.label = label
        self.response = response
        self.sensitivity = []

    def set_sensitive(self, sensitive):
        self.sensitivity.append(sensitive)


class _FakeWidget:
    """Record every call the coordinator makes on one dialog widget."""

    def __init__(self, **properties):
        self.properties = properties
        self.calls = []
        self.visible = False

    def set_line_wrap(self, wrap):
        self.calls.append(("set_line_wrap", wrap))

    def set_text(self, text):
        self.calls.append(("set_text", text))

    def set_fraction(self, fraction):
        self.calls.append(("set_fraction", fraction))

    def pulse(self):
        self.calls.append(("pulse",))

    def show(self):
        self.visible = True
        self.calls.append(("show",))

    def hide(self):
        self.visible = False
        self.calls.append(("hide",))


class _FakeContentArea:
    """Stand in for the dialog content box the offer fills in code."""

    def __init__(self):
        self.children = []
        self.spacing = None
        self.border_width = None
        self.show_all_calls = 0

    def set_spacing(self, spacing):
        self.spacing = spacing

    def set_border_width(self, width):
        self.border_width = width

    def add(self, child):
        self.children.append(child)

    def show_all(self):
        self.show_all_calls += 1


class _FakeDialog:
    """Stand in for the offer dialog, replaying responses as GTK does."""

    def __init__(self, events, **kwargs):
        self.events = events
        self.kwargs = kwargs
        self.buttons = []
        self.handlers = []
        self.content = _FakeContentArea()
        self.response_sensitivity = []
        self.shown = 0
        self.hidden = 0
        self.destroyed = 0

    def add_button(self, label, response):
        button = _FakeButton(label, response)
        self.buttons.append(button)
        return button

    def get_content_area(self):
        return self.content

    def connect(self, signal, handler, *user_data):
        self.handlers.append((signal, handler, user_data))

    def set_response_sensitive(self, response, sensitive):
        self.response_sensitivity.append((response, sensitive))

    def show(self):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def destroy(self):
        self.destroyed += 1
        self.events.append(("destroy",))

    def respond(self, response):
        for signal, handler, user_data in self.handlers:
            if signal == "response":
                handler(self, response, *user_data)


class _FakeTransfer:
    """Stand in for one owned GeoIP transfer, without touching the network."""

    def __init__(
        self,
        destination,
        deadline,
        keywords,
        *,
        result=None,
        blocking=False,
        progress_before=(),
        progress_after=(),
    ):
        self.destination = destination
        self.deadline = deadline
        self.keywords = keywords
        self.result = result
        self.blocking = blocking
        self.progress_before = progress_before
        self.progress_after = progress_after
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.released = threading.Event()
        self.thread = None

    def cancel(self):
        # The real transfer closes the response it is reading, which returns
        # the blocked worker; releasing here reproduces exactly that.
        self.cancelled.set()
        self.released.set()

    def run(self, progress_cb=None):
        self.thread = threading.current_thread()
        self.started.set()
        for update in self.progress_before:
            progress_cb(*update)
        if self.blocking:
            self.released.wait(1)
        for update in self.progress_after:
            progress_cb(*update)
        if self.cancelled.is_set():
            return None
        return self.result


class _TransferFactory:
    """Replace `GeoIPDatabaseDownload` and record every transfer built."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.transfers = []

    def __call__(self, destination, deadline, **keywords):
        transfer = _FakeTransfer(
            destination,
            deadline,
            keywords,
            **self.behaviour,
        )
        self.transfers.append(transfer)
        return transfer

    def release_all(self):
        """Unblock and join every worker this factory's transfers own."""
        for transfer in self.transfers:
            transfer.released.set()
            if transfer.thread is not None:
                transfer.thread.join(1)


class _IdleQueue:
    """Collect `GLib.idle_add` payloads for the test to run in order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queued = []
        self._next_id = 0

    def __call__(self, callback, *args):
        with self._lock:
            self._queued.append((callback, args))
            self._next_id += 1
            return self._next_id

    @property
    def pending(self):
        with self._lock:
            return tuple(self._queued)

    def drain(self):
        while True:
            with self._lock:
                if not self._queued:
                    return
                callback, args = self._queued.pop(0)
            callback(*args)


class _FakeGeolocation:
    """Stand in for geoip.Geolocation: the enabled flag and the load only."""

    def __init__(self, events):
        self.enabled = False
        self.events = events

    def load(self, path):
        self.events.append(("load", path))
        self.enabled = True


class GeoIPDownloadCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.geolocation = _FakeGeolocation(self.events)
        self.dialogs = []
        self.idle = _IdleQueue()
        self.parent = object()
        self.enterContext(mock.patch.object(
            gtk_module.Gtk, "Dialog", side_effect=self.make_dialog))
        self.enterContext(mock.patch.object(
            gtk_module.Gtk, "Label", side_effect=_FakeWidget))
        self.enterContext(mock.patch.object(
            gtk_module.Gtk, "ProgressBar", side_effect=_FakeWidget))
        self.enterContext(mock.patch.object(
            gtk_module.GLib, "idle_add", side_effect=self.idle))
        self.coordinator = gtk_geoip.GeoIPDownloadCoordinator(self.geolocation)

    def make_dialog(self, **kwargs):
        dialog = _FakeDialog(self.events, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def install_transfers(self, **behaviour):
        factory = _TransferFactory(**behaviour)
        self.enterContext(mock.patch.object(
            gtk_geoip.geoip, "GeoIPDatabaseDownload", factory))
        self.addCleanup(factory.release_all)
        return factory

    def finished(self):
        self.events.append(("finished",))

    def refused(self):
        raise AssertionError("a refused offer must not report completion")

    def offer(self, finished_callback=None):
        return self.coordinator.offer(
            self.parent,
            self.finished if finished_callback is None else finished_callback,
        )

    def accept(self, factory):
        """Accept the open offer and wait for its worker to start."""
        self.dialogs[-1].respond(gtk_module.Gtk.ResponseType.ACCEPT)
        self.assertEqual(len(factory.transfers), 1)
        transfer = factory.transfers[0]
        self.assertTrue(transfer.started.wait(1))
        return transfer

    def test_offer_is_single_flight_until_decline(self):
        factory = self.install_transfers()

        self.assertIs(self.offer(), True)
        self.assertEqual(len(self.dialogs), 1)
        dialog = self.dialogs[0]
        self.assertEqual(dialog.shown, 1)
        self.assertIs(dialog.kwargs["transient_for"], self.parent)
        self.assertIs(dialog.kwargs["modal"], True)
        self.assertEqual(
            [button.response for button in dialog.buttons],
            [
                gtk_module.Gtk.ResponseType.CANCEL,
                gtk_module.Gtk.ResponseType.ACCEPT,
            ],
        )
        # The offer builds its own content, so it shows it and then hides the
        # progress bar no transfer is driving yet.
        self.assertEqual(dialog.content.show_all_calls, 1)
        self.assertEqual(len(dialog.content.children), 2)
        self.assertEqual(dialog.content.children[-1].calls, [("hide",)])

        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.events, [])

        dialog.respond(gtk_module.Gtk.ResponseType.CANCEL)
        self.assertEqual(self.events, [("destroy",), ("finished",)])
        self.assertEqual(factory.transfers, [])

        # The declined offer released single flight, so the next activation
        # may offer again.
        self.assertIs(self.offer(), True)
        self.assertEqual(len(self.dialogs), 2)

        self.dialogs[1].respond(gtk_module.Gtk.ResponseType.CANCEL)
        self.geolocation.enabled = True
        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs), 2)

    def test_failed_presentation_destroys_its_dialog_and_releases_the_offer(
        self,
    ):
        self.install_transfers()
        error = RuntimeError("dialog presentation failed")

        def failing_show():
            raise error

        def present_failing_dialog(**kwargs):
            dialog = self.make_dialog(**kwargs)
            dialog.show = failing_show
            return dialog

        with mock.patch.object(
            gtk_module.Gtk, "Dialog", side_effect=present_failing_dialog
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.offer(self.refused)

        # An offer that never reached the screen still owns its dialog, so it
        # is destroyed rather than dropped, and the failure keeps its identity.
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.dialogs[0].destroyed, 1)
        self.assertEqual(self.events, [("destroy",)])

        # The released offer leaves this coordinator free to offer again.
        self.assertIs(self.offer(), True)
        self.assertEqual(len(self.dialogs), 2)
        self.assertEqual(self.dialogs[1].shown, 1)

    def test_accepted_offer_remains_single_flight_until_transfer_completion(self):
        factory = self.install_transfers(result="/cache/db.mmdb", blocking=True)

        self.assertIs(self.offer(), True)
        started = time.monotonic()
        transfer = self.accept(factory)

        dialog = self.dialogs[0]
        self.assertEqual(dialog.buttons[1].sensitivity, [False])
        self.assertEqual(
            dialog.response_sensitivity,
            [(gtk_module.Gtk.ResponseType.CANCEL, True)],
        )
        self.assertEqual(
            dialog.content.children[-1].calls,
            [("hide",), ("show",)],
        )
        self.assertEqual(transfer.destination, gtk_geoip.geoip.CACHE_DB_PATH)
        self.assertEqual(transfer.keywords, {})
        self.assertGreaterEqual(
            transfer.deadline,
            started + gtk_geoip.geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )
        self.assertLessEqual(
            transfer.deadline,
            time.monotonic()
            + gtk_geoip.geoip.DOWNLOAD_TIMEOUT_SECONDS,
        )

        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.events, [])

        transfer.released.set()
        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertIs(self.offer(self.refused), False)

        self.idle.drain()
        self.assertEqual(
            self.events,
            [("destroy",), ("load", "/cache/db.mmdb"), ("finished",)],
        )
        self.assertEqual(dialog.destroyed, 1)
        self.assertEqual(len(factory.transfers), 1)

    def test_begin_close_cancels_transfer_and_suppresses_late_dispatch(self):
        factory = self.install_transfers(
            result="/cache/db.mmdb",
            blocking=True,
            progress_after=((1, 2),),
        )

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        self.coordinator.begin_close()
        self.assertTrue(transfer.cancelled.is_set())

        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertEqual(self.idle.pending, ())
        self.assertEqual(self.events, [])
        self.assertEqual(self.dialogs[0].destroyed, 0)
        self.assertIs(self.offer(self.refused), False)
        self.assertEqual(len(self.dialogs), 1)

    def test_queued_completion_after_close_does_not_load_database_or_touch_gtk(self):
        factory = self.install_transfers(
            result="/cache/db.mmdb",
            blocking=True,
            progress_after=((3, 4),),
        )

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)
        progress = self.dialogs[0].content.children[-1]

        transfer.released.set()
        transfer.thread.join(1)
        self.assertFalse(transfer.thread.is_alive())
        self.assertEqual(len(self.idle.pending), 2)

        self.coordinator.begin_close()
        self.idle.drain()

        self.assertEqual(self.events, [])
        self.assertEqual(self.dialogs[0].destroyed, 0)
        self.assertFalse(self.geolocation.enabled)
        self.assertNotIn(("set_fraction", 0.75), progress.calls)
        self.assertNotIn(("pulse",), progress.calls)

    def test_join_until_uses_only_the_remaining_application_deadline(self):
        factory = self.install_transfers(blocking=True)

        self.assertIs(self.offer(), True)
        transfer = self.accept(factory)

        recorded = []
        real_join = transfer.thread.join

        def recording_join(timeout=None):
            recorded.append(timeout)
            return real_join(timeout)

        with mock.patch.object(transfer.thread, "join", recording_join):
            # A worker that outlasts the application deadline is waited for
            # only as long as that deadline still has left.
            self.coordinator.join_until(time.monotonic() + 0.2)
            self.assertEqual(len(recorded), 1)
            self.assertGreater(recorded[0], 0)
            self.assertLessEqual(recorded[0], 0.2)
            self.assertTrue(transfer.thread.is_alive())

            # An exhausted deadline leaves nothing to wait for.
            self.coordinator.join_until(time.monotonic() - 0.2)
            self.assertEqual(len(recorded), 1)

            with self.assertRaises(TypeError):
                self.coordinator.join_until(1)
            self.assertEqual(len(recorded), 1)

            transfer.released.set()
            self.coordinator.join_until(time.monotonic() + 1.0)
            self.assertEqual(len(recorded), 2)

        self.assertFalse(transfer.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
