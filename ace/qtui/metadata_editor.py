"""
metadata_editor.py — Reusable key/value metadata editor widget.

Same convention as the web UI and CLI: comma-separated values become a
list on save (matching --meta's parsing), single values stay plain
strings. Used both for editing an existing archive's metadata and for
setting initial metadata when creating a new one.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel
)
from PyQt6.QtCore import Qt


class MetaFieldRow(QWidget):
    def __init__(self, key: str = "", value: str = "", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.key_edit = QLineEdit(key)
        self.key_edit.setPlaceholderText("field name")
        self.val_edit = QLineEdit(value)
        self.val_edit.setPlaceholderText("value (comma-separate for a list)")
        self.remove_btn = QPushButton("\u00d7")
        self.remove_btn.setFixedWidth(32)
        layout.addWidget(self.key_edit, 1)
        layout.addWidget(self.val_edit, 2)
        layout.addWidget(self.remove_btn)

    def get_pair(self):
        k = self.key_edit.text().strip()
        v = self.val_edit.text().strip()
        if not k:
            return None
        if "," in v:
            return k, [s.strip() for s in v.split(",")]
        return k, v


class MetadataEditor(QWidget):
    """A stack of MetaFieldRow widgets plus an 'Add field' button.
    Call load(dict) to populate, get_metadata() to read the current
    complete set back out (used both to save edits and to seed a new
    archive's initial metadata)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(6)
        outer.addLayout(self.rows_layout)

        self.add_btn = QPushButton("+ Add field")
        self.add_btn.setProperty("class", "ghost")
        self.add_btn.clicked.connect(lambda: self.add_row())
        outer.addWidget(self.add_btn)

        note = QLabel("Removing a row and saving deletes that field. No undo.")
        note.setProperty("class", "muted")
        outer.addWidget(note)

    def add_row(self, key="", value=""):
        row = MetaFieldRow(key, str(value) if not isinstance(value, list) else ", ".join(value))
        row.remove_btn.clicked.connect(lambda: self._remove_row(row))
        self.rows_layout.addWidget(row)
        self.rows.append(row)
        return row

    def _remove_row(self, row):
        self.rows_layout.removeWidget(row)
        self.rows.remove(row)
        row.deleteLater()

    def clear(self):
        for row in list(self.rows):
            self._remove_row(row)

    def load(self, meta: dict, skip_keys=("created", "source_host", "hmac_sha256")):
        self.clear()
        for k, v in (meta or {}).items():
            if k in skip_keys:
                continue
            self.add_row(k, v)
        if not self.rows:
            self.add_row()

    def get_metadata(self) -> dict:
        out = {}
        for row in self.rows:
            pair = row.get_pair()
            if pair:
                out[pair[0]] = pair[1]
        return out
