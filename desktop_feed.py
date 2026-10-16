#!/usr/bin/env python3
"""Native desktop Local Dispatch application."""

from __future__ import annotations

import argparse
import sys
import webbrowser

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from local_feed import FeedItem, load_feeds


class FeedWorker(QObject):
    finished = Signal(object, object)

    def __init__(self, location: str, distance: int) -> None:
        super().__init__()
        self.location = location
        self.distance = distance

    def run(self) -> None:
        news, events = load_feeds(self.location, self.distance)
        self.finished.emit(news, events)


class FeedPane(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[FeedItem] = []
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.show_item)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setPlaceholderText("Select a story to read its summary.")
        self.open_button = QPushButton("Open article in browser")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_article)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 3)
        layout.addWidget(self.details, 2)
        layout.addWidget(self.open_button)

    def set_items(self, items: list[FeedItem]) -> None:
        self.items = items
        self.list.clear()
        self.details.clear()
        self.open_button.setEnabled(False)
        for item in items:
            row = QListWidgetItem(item.title)
            row.setToolTip(f"{item.source} | {item.published}")
            self.list.addItem(row)
        if items:
            self.list.setCurrentRow(0)

    def show_item(self, row: int) -> None:
        if row < 0 or row >= len(self.items):
            self.details.clear()
            self.open_button.setEnabled(False)
            return
        item = self.items[row]
        self.details.setPlainText(
            f"{item.title}\n\n{item.source}\n{item.published}\n\n{item.summary or 'No summary was provided.'}"
        )
        self.open_button.setEnabled(True)

    def open_article(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.items):
            webbrowser.open(self.items[row].link)


class LocalDispatchWindow(QMainWindow):
    def __init__(self, location: str = "", distance: int = 25) -> None:
        super().__init__()
        self.worker_thread: QThread | None = None
        self.worker: FeedWorker | None = None
        self.setWindowTitle("Local Dispatch")
        self.resize(900, 700)

        title = QLabel("LOCAL DISPATCH")
        title.setFont(QFont("Georgia", 24, QFont.Weight.Bold))
        subtitle = QLabel("News and events from around your location")
        subtitle.setObjectName("subtitle")

        from PySide6.QtWidgets import QLineEdit

        self.location = QLineEdit(location)
        self.location.setPlaceholderText("City, ZIP code, or state")
        self.distance = QSpinBox()
        self.distance.setRange(1, 500)
        self.distance.setValue(distance)
        self.distance.setSuffix(" miles")
        self.refresh = QPushButton("Refresh feeds")
        self.refresh.clicked.connect(self.refresh_feeds)
        self.status = QLabel("Enter a location and refresh to load the desk.")
        self.status.setObjectName("status")

        form = QFormLayout()
        form.addRow("Location", self.location)
        form.addRow("Radius", self.distance)
        controls = QHBoxLayout()
        controls.addLayout(form, 1)
        controls.addWidget(self.refresh)

        self.tabs = QTabWidget()
        self.news = FeedPane()
        self.events = FeedPane()
        self.tabs.addTab(self.news, "News")
        self.tabs.addTab(self.events, "Events")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(controls)
        layout.addWidget(self.status)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f1e8; color: #18231f; }
            QLabel#subtitle, QLabel#status { color: #68736d; }
            QLineEdit, QSpinBox, QListWidget, QTextBrowser { background: #fffdf8; border: 1px solid #d8d4c9; padding: 7px; }
            QPushButton { background: #d65b3d; color: white; border: 0; padding: 9px 15px; font-weight: bold; }
            QPushButton:disabled { background: #b9b3a9; }
            QTabBar::tab { padding: 10px 18px; }
            QTabBar::tab:selected { color: #d65b3d; border-bottom: 2px solid #d65b3d; }
            """
        )

    def refresh_feeds(self) -> None:
        location = self.location.text().strip()
        if not location:
            QMessageBox.warning(self, "Location required", "Enter a city, ZIP code, or state first.")
            self.location.setFocus()
            return
        self.refresh.setEnabled(False)
        self.status.setText(f"Loading news and events for {location}...")
        self.worker_thread = QThread(self)
        self.worker = FeedWorker(location, self.distance.value())
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.feeds_loaded)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def feeds_loaded(self, news: list[FeedItem], events: list[FeedItem]) -> None:
        self.news.set_items(news)
        self.events.set_items(events)
        self.refresh.setEnabled(True)
        total = len(news) + len(events)
        self.status.setText(f"Loaded {total} results for {self.location.text().strip()}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the native Local Dispatch desktop app")
    parser.add_argument("--location", default="", help="Pre-fill a city, ZIP code, or state")
    parser.add_argument("--distance", type=int, default=25, help="Event search radius in miles")
    args = parser.parse_args()
    if args.distance < 1:
        parser.error("--distance must be at least 1 mile")
    app = QApplication(sys.argv)
    window = LocalDispatchWindow(args.location, args.distance)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()