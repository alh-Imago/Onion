"""
new_archive_dialog.py — "+ New Archive" dialog.

Uses native QFileDialog pickers for file/folder selection rather than
rebuilding a custom breadcrumb navigator from scratch -- Qt's native
picker is the idiomatic choice here and covers the same capability.
Selection accumulates in a simple list (add file(s), add a folder,
remove an item) rather than a persistent-across-navigation Map, since
native dialogs don't have a "current folder" concept to persist across.
"""

import os

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QCheckBox, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal

from .metadata_editor import MetadataEditor
from .workers import Worker


class NewArchiveDialog(QDialog):
    created = pyqtSignal()

    def __init__(self, parent=None, default_path=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Archive")
        self.resize(560, 640)
        self.default_path = default_path or os.path.expanduser("~")
        self.selected_paths = []

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # Password
        layout.addWidget(QLabel("Password (optional — leave blank for no encryption)"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password_input)

        # No-compress
        self.no_compress_cb = QCheckBox("No compression (store raw)")
        layout.addWidget(self.no_compress_cb)
        no_compress_note = QLabel(
            "Keeps the file fully searchable via metadata and the TOC block "
            "without running any compression algorithm."
        )
        no_compress_note.setProperty("class", "muted")
        no_compress_note.setWordWrap(True)
        layout.addWidget(no_compress_note)

        # Split-huffman (experimental) + time warning
        self.split_huffman_cb = QCheckBox("Experimental: split-stream Huffman")
        self.split_huffman_cb.toggled.connect(self._toggle_split_warning)
        layout.addWidget(self.split_huffman_cb)
        split_note = QLabel(
            "Separate Huffman trees for literal vs match data. Not a universal win: "
            "smaller on random/repetitive data, larger on typical source code, small "
            "files, and general text."
        )
        split_note.setProperty("class", "muted")
        split_note.setWordWrap(True)
        layout.addWidget(split_note)

        self.split_warning = QLabel(
            "\u26a0 Pure Python, no hardware acceleration \u2014 noticeably slower than the "
            "default, especially on larger files. May take several seconds to tens of "
            "seconds depending on size and content."
        )
        self.split_warning.setProperty("class", "warning")
        self.split_warning.setWordWrap(True)
        self.split_warning.setVisible(False)
        layout.addWidget(self.split_warning)

        # File/folder selection
        layout.addWidget(QLabel("Files and folders to include"))
        pick_row = QHBoxLayout()
        add_files_btn = QPushButton("Add file(s)\u2026")
        add_files_btn.setProperty("class", "ghost")
        add_files_btn.clicked.connect(self._add_files)
        pick_row.addWidget(add_files_btn)
        add_folder_btn = QPushButton("Add folder\u2026")
        add_folder_btn.setProperty("class", "ghost")
        add_folder_btn.clicked.connect(self._add_folder)
        pick_row.addWidget(add_folder_btn)
        pick_row.addStretch()
        layout.addLayout(pick_row)

        self.selected_list = QListWidget()
        self.selected_list.setMaximumHeight(140)
        layout.addWidget(self.selected_list)

        remove_btn = QPushButton("Remove selected item")
        remove_btn.setProperty("class", "ghost")
        remove_btn.clicked.connect(self._remove_selected)
        layout.addWidget(remove_btn)

        # Destination
        layout.addWidget(QLabel("Save as"))
        self.dest_input = QLineEdit()
        self.dest_input.setText(os.path.join(self.default_path, "archive.onion"))
        layout.addWidget(self.dest_input)

        # Metadata
        layout.addWidget(QLabel("Metadata (optional)"))
        self.meta_editor = MetadataEditor()
        self.meta_editor.clear()
        layout.addWidget(self.meta_editor)

        # Actions
        actions_row = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setProperty("class", "muted")
        actions_row.addWidget(self.status_label, 1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setProperty("class", "ghost")
        cancel_btn.clicked.connect(self.reject)
        actions_row.addWidget(cancel_btn)
        create_btn = QPushButton("Create Archive")
        create_btn.setProperty("class", "primary")
        create_btn.clicked.connect(self._on_create)
        actions_row.addWidget(create_btn)
        outer.addLayout(actions_row)

    def _toggle_split_warning(self, checked):
        self.split_warning.setVisible(checked)

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select file(s)", self.default_path)
        for f in files:
            if f not in self.selected_paths:
                self.selected_paths.append(f)
                self.selected_list.addItem(QListWidgetItem("\U0001F4C4 " + f))

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select a folder", self.default_path)
        if folder and folder not in self.selected_paths:
            self.selected_paths.append(folder)
            self.selected_list.addItem(QListWidgetItem("\U0001F4C1 " + folder))

    def _remove_selected(self):
        row = self.selected_list.currentRow()
        if row >= 0:
            self.selected_list.takeItem(row)
            del self.selected_paths[row]

    def _on_create(self):
        if not self.selected_paths:
            self.status_label.setText("Select at least one file or folder first.")
            return
        dest = self.dest_input.text().strip()
        if not dest:
            self.status_label.setText("Enter a destination filename first.")
            return
        if not dest.lower().endswith(".onion"):
            dest += ".onion"
        if os.path.exists(dest):
            self.status_label.setText(f"Destination already exists: {dest}")
            return

        password = self.password_input.text()
        no_compress = self.no_compress_cb.isChecked()
        split_huffman = self.split_huffman_cb.isChecked()
        meta = self.meta_editor.get_metadata()
        sources = list(self.selected_paths)

        self.status_label.setText("Creating archive...")

        def run():
            from ace.analyser import analyse
            from ace.transformer import compress_files
            from ace.manifest import collect
            from ace.ignore import build_matcher

            base_dir = sources[0] if (len(sources) == 1 and os.path.isdir(sources[0])) else ""
            matcher = build_matcher(extra_patterns=[], base_dir=base_dir, use_default_ignores=True)
            files, _label = collect(sources, matcher=matcher)
            if not files:
                raise ValueError("No files to compress (everything matched an ignore pattern).")

            total_data = b"".join(d for _, d in files)
            iset = analyse(total_data, encrypt=bool(password),
                            no_compress=no_compress, split_huffman=split_huffman)
            compress_files(files, iset, dest, password=password,
                            audit=True, meta_pairs=meta or None, sign_key=None)
            return dest, len(files)

        worker = Worker(run)
        worker.signals.finished.connect(self._on_create_done)
        worker.signals.error.connect(lambda msg: self.status_label.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_create_done(self, result):
        dest, file_count = result
        self.status_label.setText(f"Created: {dest} ({file_count} file(s))")
        self.created.emit()
        self.accept()
