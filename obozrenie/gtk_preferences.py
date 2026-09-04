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

"""The per-game preferences dialog, built from the presenter's fields."""

from collections.abc import Callable, Mapping, Sequence

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from obozrenie import i18n
from obozrenie.presenter import OptionKind, PreferenceField


class PreferencesDialog(Adw.Dialog):

    """Edit one game's options: Save writes them back, anything else discards.

    `rows` maps each option to the widget editing it — an `Adw.EntryRow`, an
    `Adw.SwitchRow`, or the `Gtk.TextView` of a line list.
    """

    def __init__(
        self,
        title: str,
        fields: Sequence[PreferenceField],
        on_save: Callable[[Mapping[str, object]], None],
    ) -> None:
        super().__init__(title=title, content_width=520, content_height=560)
        self._on_save = on_save
        self._readers: dict[str, Callable[[], object]] = {}
        self.rows: dict[str, Gtk.Widget] = {}

        header = Adw.HeaderBar()
        self.save_button = Gtk.Button(label=i18n._("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save_clicked)
        header.pack_end(self.save_button)

        page = Adw.PreferencesPage()
        row_fields = [field for field in fields if field.schema.kind is not OptionKind.LINES]
        if row_fields:
            group = Adw.PreferencesGroup()
            for field in row_fields:
                group.add(self._row(field))
            page.add(group)
        for field in fields:
            if field.schema.kind is OptionKind.LINES:
                page.add(self._lines_group(field))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _row(self, field: PreferenceField) -> Adw.PreferencesRow:
        schema = field.schema
        if schema.kind is OptionKind.FLAG:
            row = Adw.SwitchRow(
                title=schema.label,
                subtitle=schema.description,
                active=bool(field.value),
            )
            self._readers[field.option] = row.get_active
        else:
            row = Adw.EntryRow(title=schema.label, tooltip_text=schema.description)
            row.set_text(field.value)
            self._readers[field.option] = row.get_text
        self.rows[field.option] = row
        return row

    def _lines_group(self, field: PreferenceField) -> Adw.PreferencesGroup:
        schema = field.schema
        group = Adw.PreferencesGroup(
            title=schema.label,
            description=i18n._("Single entry per line"),
        )
        view = Gtk.TextView(
            tooltip_text=schema.description,
            accepts_tab=False,
            left_margin=8,
            right_margin=8,
            top_margin=8,
            bottom_margin=8,
        )
        buffer = view.get_buffer()
        buffer.set_text(field.value)
        group.add(Gtk.ScrolledWindow(
            child=view,
            has_frame=True,
            min_content_height=120,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        ))
        self._readers[field.option] = lambda: buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.rows[field.option] = view
        return group

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        values = {option: read() for option, read in self._readers.items()}
        self._on_save(values)
        self.close()
