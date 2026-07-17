"""
archive_card.py — One search result, displayed as a collapsible card.

Mirrors the web UI's "peel open" card exactly in spirit: click the
header to expand/collapse contents, signature, metadata editor, and
actions. Calls ace.transformer/ace.search functions directly -- no HTTP,
since this is an in-process Qt app, not a browser talking to a server.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtCore import QThreadPool

from .metadata_editor import MetadataEditor
from .workers import Worker


class ArchiveCard(QFrame):
    changed = pyqtSignal()   # emitted after a save/unwrap/delete that should refresh the results list

    def __init__(self, summary: dict, parent=None):
        super().__init__(parent)
        self.summary = summary
        self.path = summary["path"]
        self.expanded = False
        self._delete_armed = False
        self._delete_timer = None

        self.setProperty("class", "card")
        self.outer = QVBoxLayout(self)

        self._build_header()
        self._build_meta_line()
        self._build_description()
        self._build_expandable_section()

        self.outer.addStretch(0)
        self.hint = QLabel("click to peel open \u2193")
        self.hint.setProperty("class", "muted")
        self.outer.addWidget(self.hint)

        self._set_expanded(False)

    # ── Header row: path + badges ────────────────────────────────────────
    def _build_header(self):
        row = QHBoxLayout()
        path_label = QLabel(self.path)
        path_label.setProperty("class", "mono")
        path_label.setWordWrap(True)
        row.addWidget(path_label, 1)

        if self.summary.get("encrypted"):
            enc = QLabel("\U0001F512 encrypted")
            enc.setProperty("class", "badge-enc")
            row.addWidget(enc)

        tags = (self.summary.get("meta") or {}).get("tags")
        if tags:
            tag_list = tags if isinstance(tags, list) else [tags]
            for t in tag_list:
                badge = QLabel(str(t))
                badge.setProperty("class", "badge")
                row.addWidget(badge)

        self.outer.addLayout(row)
        self.header_row = row

    def _build_meta_line(self):
        contents = self.summary.get("contents")
        parts = [f"{self.summary['original_size']:,} bytes original",
                  f"{self.summary['layer_count']} layer(s)"]
        if contents:
            parts.append(f"{len(contents)} file(s)")
        line = QLabel(" \u00b7 ".join(parts))
        line.setProperty("class", "muted")
        self.outer.addWidget(line)

    def _build_description(self):
        desc = (self.summary.get("meta") or {}).get("description")
        if desc:
            text = ", ".join(str(d) for d in desc) if isinstance(desc, list) else str(desc)
            label = QLabel(text)
            label.setWordWrap(True)
            self.outer.addWidget(label)

    # ── Expandable section: contents, signature, metadata editor, actions ─
    def _build_expandable_section(self):
        self.expandable = QWidget()
        layout = QVBoxLayout(self.expandable)
        layout.setContentsMargins(0, 8, 0, 0)

        contents = self.summary.get("contents")
        if contents:
            tag = QLabel(f"contents ({len(contents)} file(s), from TOC, no decompression)")
            tag.setProperty("class", "muted")
            layout.addWidget(tag)
            table = QTableWidget(len(contents), 2)
            table.setHorizontalHeaderLabels(["path", "size"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setMaximumHeight(140)
            for i, entry in enumerate(contents):
                table.setItem(i, 0, QTableWidgetItem(entry.get("path", "?")))
                size_item = QTableWidgetItem(f"{entry.get('size', 0):,} B")
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
                table.setItem(i, 1, size_item)
            layout.addWidget(table)

        self._build_signature_block(layout)
        self._build_metadata_editor(layout)
        self._build_actions(layout)

        self.outer.addWidget(self.expandable)

    def _build_signature_block(self, layout):
        hmac = (self.summary.get("meta") or {}).get("hmac_sha256")
        if not hmac:
            return
        tag = QLabel("signature (read-only)")
        tag.setProperty("class", "muted")
        layout.addWidget(tag)

        row = QHBoxLayout()
        short = hmac[:10] + "\u2026" + hmac[-8:] if len(hmac) > 20 else hmac
        hash_label = QLabel(short)
        hash_label.setProperty("class", "mono")
        hash_label.setToolTip(hmac)
        row.addWidget(hash_label)

        self.sig_key_input = QLineEdit()
        self.sig_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.sig_key_input.setPlaceholderText("signing key to verify")
        row.addWidget(self.sig_key_input, 1)

        verify_btn = QPushButton("Verify")
        verify_btn.setProperty("class", "ghost")
        verify_btn.clicked.connect(self._on_verify)
        row.addWidget(verify_btn)

        self.sig_status = QLabel("present, unverified")
        self.sig_status.setProperty("class", "muted")
        row.addWidget(self.sig_status)

        layout.addLayout(row)

    def _build_metadata_editor(self, layout):
        tag = QLabel("metadata (created/source host preserved automatically)")
        tag.setProperty("class", "muted")
        layout.addWidget(tag)

        self.meta_editor = MetadataEditor()
        self.meta_editor.load(self.summary.get("meta") or {})
        layout.addWidget(self.meta_editor)

        save_row = QHBoxLayout()
        save_btn = QPushButton("Save changes")
        save_btn.setProperty("class", "primary")
        save_btn.clicked.connect(self._on_save_meta)
        save_row.addWidget(save_btn)
        self.meta_status = QLabel("")
        self.meta_status.setProperty("class", "muted")
        save_row.addWidget(self.meta_status)
        save_row.addStretch()
        layout.addLayout(save_row)

    def _build_actions(self, layout):
        row = QHBoxLayout()

        if self.summary.get("encrypted"):
            self.unwrap_password = QLineEdit()
            self.unwrap_password.setEchoMode(QLineEdit.EchoMode.Password)
            self.unwrap_password.setPlaceholderText("password to unwrap")
            row.addWidget(self.unwrap_password)
        else:
            self.unwrap_password = None

        unwrap_btn = QPushButton("Remove wrapper (restore file)")
        unwrap_btn.setProperty("class", "ghost")
        unwrap_btn.clicked.connect(self._on_unwrap)
        row.addWidget(unwrap_btn)

        self.delete_btn = QPushButton("Delete archive")
        self.delete_btn.setProperty("class", "danger")
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        row.addWidget(self.delete_btn)

        self.action_status = QLabel("")
        self.action_status.setProperty("class", "muted")
        row.addWidget(self.action_status)
        row.addStretch()
        layout.addLayout(row)

    # ── Expand/collapse ───────────────────────────────────────────────────
    def mousePressEvent(self, event):
        # Ignore clicks that originated inside the expandable section's
        # interactive widgets -- only the card's own header/background
        # toggles the peel state, matching the web UI's stopPropagation.
        child = self.childAt(event.pos())
        if child is not None and self.expandable.isAncestorOf(child):
            return
        self._set_expanded(not self.expanded)
        super().mousePressEvent(event)

    def _set_expanded(self, value: bool):
        self.expanded = value
        self.expandable.setVisible(value)
        self.hint.setText("click to collapse \u2191" if value else "click to peel open \u2193")

    # ── Actions ────────────────────────────────────────────────────────────
    def _on_verify(self):
        key = self.sig_key_input.text()
        if not key:
            self.sig_status.setText("Enter a key first")
            return
        from ace.transformer import verify as verify_archive
        self.sig_status.setText("Verifying...")

        def run():
            return verify_archive(self.path, key)

        worker = Worker(run)
        worker.signals.finished.connect(self._on_verify_done)
        worker.signals.error.connect(lambda msg: self.sig_status.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_verify_done(self, valid):
        self.sig_status.setText("\u2713 present and confirmed" if valid else "\u2717 invalid signature")

    def _on_save_meta(self):
        new_meta = self.meta_editor.get_metadata()
        self.meta_status.setText("Saving...")
        from ace.transformer import save_metadata_replacing_editable_fields

        def run():
            save_metadata_replacing_editable_fields(self.path, new_meta)

        worker = Worker(run)
        worker.signals.finished.connect(lambda _: self._on_save_meta_done())
        worker.signals.error.connect(lambda msg: self.meta_status.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_save_meta_done(self):
        self.meta_status.setText("Saved.")
        self.changed.emit()

    def _on_unwrap(self):
        password = self.unwrap_password.text() if self.unwrap_password else ""
        from ace.transformer import unwrap
        self.action_status.setText("Removing wrapper...")

        def run():
            return unwrap(self.path, password=password)

        worker = Worker(run)
        worker.signals.finished.connect(lambda _: self._on_unwrap_done())
        worker.signals.error.connect(lambda msg: self.action_status.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_unwrap_done(self):
        self.action_status.setText("Restored.")
        self.changed.emit()

    def _on_delete_clicked(self):
        if not self._delete_armed:
            self._delete_armed = True
            self.delete_btn.setText("Really delete? Click again (6s)")
            self.delete_btn.setProperty("class", "danger-armed")
            self.delete_btn.style().unpolish(self.delete_btn)
            self.delete_btn.style().polish(self.delete_btn)
            self._delete_timer = QTimer(self)
            self._delete_timer.setSingleShot(True)
            self._delete_timer.timeout.connect(self._disarm_delete)
            self._delete_timer.start(6000)
            return
        self._disarm_delete()
        self._do_delete()

    def _disarm_delete(self):
        self._delete_armed = False
        self.delete_btn.setText("Delete archive")
        self.delete_btn.setProperty("class", "danger")
        self.delete_btn.style().unpolish(self.delete_btn)
        self.delete_btn.style().polish(self.delete_btn)

    def _do_delete(self):
        import os
        self.action_status.setText("Deleting...")

        def run():
            os.remove(self.path)

        worker = Worker(run)
        worker.signals.finished.connect(lambda _: self._on_delete_done())
        worker.signals.error.connect(lambda msg: self.action_status.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_delete_done(self):
        self.action_status.setText("Deleted.")
        self.changed.emit()
