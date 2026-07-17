"""
main_window.py — Onion Qt UI main window.

Mirrors the web UI's layout: a search panel up top, results below as
"peel open" cards, a theme toggle, and a "+ New Archive" button. Calls
ace.search.search() directly (no HTTP) via a background worker so the
UI never freezes while scanning a directory of archives.
"""

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QFrame, QFileDialog, QApplication
)
from PyQt6.QtCore import Qt, QThreadPool

from .workers import Worker
from .archive_card import ArchiveCard
from .metadata_editor import MetaFieldRow
from . import theme as theme_mod


class MainWindow(QMainWindow):
    def __init__(self, default_paths=None):
        super().__init__()
        self.setWindowTitle("Onion \U0001F9C5 — Archive Search")
        self.resize(820, 700)
        self.default_paths = default_paths or []
        self.dark = False
        self.filter_rows = []

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 20, 20, 20)

        self._build_top_bar(outer)
        self._build_search_panel(outer)
        self._build_status_line(outer)
        self._build_results_area(outer)

        if self.default_paths:
            self.path_input.setText(self.default_paths[0])
        self.do_search()

    # ── Top bar ────────────────────────────────────────────────────────────
    def _build_top_bar(self, outer):
        row = QHBoxLayout()
        heading = QLabel("\u25ce Onion \u2014 Archive Search")
        heading.setProperty("class", "heading")
        row.addWidget(heading)
        row.addStretch()

        self.new_archive_btn = QPushButton("+ New Archive")
        self.new_archive_btn.setProperty("class", "ghost")
        self.new_archive_btn.clicked.connect(self.open_new_archive_dialog)
        row.addWidget(self.new_archive_btn)

        self.theme_btn = QPushButton("\u2600 Light")
        self.theme_btn.clicked.connect(self.toggle_theme)
        row.addWidget(self.theme_btn)

        outer.addLayout(row)

    def toggle_theme(self):
        self.dark = not self.dark
        theme_mod.apply_theme(QApplication.instance(), dark=self.dark)
        self.theme_btn.setText("\u263e Dark" if self.dark else "\u2600 Light")

    # ── Search panel ─────────────────────────────────────────────────────
    def _build_search_panel(self, outer):
        panel = QFrame()
        panel.setProperty("class", "panel")
        layout = QVBoxLayout(panel)

        path_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("e.g. /home/alan/archives")
        self.path_input.returnPressed.connect(self.do_search)
        path_row.addWidget(self.path_input, 1)
        browse_btn = QPushButton("Browse\u2026")
        browse_btn.setProperty("class", "ghost")
        browse_btn.clicked.connect(self.browse_path)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        filters_label = QLabel("Metadata filters")
        filters_label.setProperty("class", "muted")
        layout.addWidget(filters_label)
        self.filters_layout = QVBoxLayout()
        layout.addLayout(self.filters_layout)
        self.add_filter_row()

        add_filter_btn = QPushButton("+ Add filter")
        add_filter_btn.setProperty("class", "ghost")
        add_filter_btn.clicked.connect(lambda: self.add_filter_row())
        layout.addWidget(add_filter_btn)

        self.any_input = QLineEdit()
        self.any_input.setPlaceholderText("Free text (filename or any metadata value)")
        self.any_input.returnPressed.connect(self.do_search)
        layout.addWidget(self.any_input)

        actions_row = QHBoxLayout()
        search_btn = QPushButton("Search")
        search_btn.setProperty("class", "primary")
        search_btn.clicked.connect(self.do_search)
        actions_row.addWidget(search_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.setProperty("class", "ghost")
        clear_btn.clicked.connect(self.clear_search)
        actions_row.addWidget(clear_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        outer.addWidget(panel)

    def add_filter_row(self, key="", value=""):
        row = MetaFieldRow(key, value)
        row.remove_btn.clicked.connect(lambda: self._remove_filter_row(row))
        self.filters_layout.addWidget(row)
        self.filter_rows.append(row)

    def _remove_filter_row(self, row):
        self.filters_layout.removeWidget(row)
        self.filter_rows.remove(row)
        row.deleteLater()

    def browse_path(self):
        directory = QFileDialog.getExistingDirectory(self, "Choose a folder", self.path_input.text() or "")
        if directory:
            self.path_input.setText(directory)
            self.do_search()

    def clear_search(self):
        self.path_input.clear()
        self.any_input.clear()
        for row in list(self.filter_rows):
            self._remove_filter_row(row)
        self.add_filter_row()
        self.status_label.setText("Enter a path above and search, or search with no filters to list everything.")
        self._clear_results()

    # ── Status + results ─────────────────────────────────────────────────
    def _build_status_line(self, outer):
        self.status_label = QLabel("Enter a path above and search, or search with no filters to list everything.")
        self.status_label.setProperty("class", "muted")
        outer.addWidget(self.status_label)

    def _build_results_area(self, outer):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_container.setObjectName("resultsContainer")
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.addStretch()
        scroll.setWidget(self.results_container)
        outer.addWidget(scroll, 1)

    def _clear_results(self):
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── Search execution ─────────────────────────────────────────────────
    def do_search(self):
        from ace.search import search as run_search

        path = self.path_input.text().strip()
        paths = [path] if path else self.default_paths
        if not paths:
            self.status_label.setText("Enter a path to search.")
            return

        meta_filters = {}
        for row in self.filter_rows:
            pair = row.get_pair()
            if pair:
                k, v = pair
                meta_filters[k] = v if isinstance(v, str) else ",".join(v)
        any_text = self.any_input.text().strip() or None

        self.status_label.setText("Searching...")

        def run():
            return list(run_search(paths, meta_filters=meta_filters, any_text=any_text, recursive=True))

        worker = Worker(run)
        worker.signals.finished.connect(self._on_search_done)
        worker.signals.error.connect(lambda msg: self.status_label.setText(f"Error: {msg.splitlines()[0]}"))
        QThreadPool.globalInstance().start(worker)

    def _on_search_done(self, results):
        self._clear_results()
        if not results:
            self.status_label.setText("0 match(es).")
            empty = QLabel("No matching archives found. Try a broader path or remove a filter.")
            empty.setProperty("class", "muted")
            self.results_layout.insertWidget(0, empty)
            return

        self.status_label.setText(f"{len(results)} match(es). Click an archive to peel it open.")
        for summary in results:
            card = ArchiveCard(summary)
            card.changed.connect(self.do_search)
            self.results_layout.insertWidget(self.results_layout.count() - 1, card)

    def open_new_archive_dialog(self):
        from .new_archive_dialog import NewArchiveDialog
        dialog = NewArchiveDialog(self, default_path=self.path_input.text().strip() or None)
        dialog.created.connect(self.do_search)
        dialog.exec()
