#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epson_maintenance_gui.py — graphical front end for the Epson maintenance tools

Builds on epson_maintenance.py (protocol logic) and provides:

    - status view with ink levels and waste ink pad
    - nozzle check printing
    - regular print head cleaning (nozzle group 0 = all)
    - Power Cleaning (with a confirmation dialog because of the ink usage)
    - log window and automatic status refresh

Requires: PySide6 (on CachyOS/Arch:  sudo pacman -S python-pyside6)

Start:  python3 epson_maintenance_gui.py
"""

import json
import os
import queue
import sys
import threading
import time

import epson_maintenance as core

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "epson_maintenance",
    "gui.json",
)
# Predecessor of this tool stored its config under a German app name;
# migrate it silently if the new path does not exist yet.
CONFIG_PATH_LEGACY = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "epson_wartung",
    "gui.json",
)

COLORS = {
    "ok": "#3fb950",       # ready / finished
    "working": "#d29922",   # cleaning, printing, self test
    "error": "#f85149",    # error state
    "unknown": "#8b949e",
}

TIME_FMT = time.strftime


def ink_color(name: str) -> str:
    """Bar color per ink type (fallback: black/gray inks)."""
    low = name.lower()
    if "cyan" in low:
        return "#39a7c9"
    if "magenta" in low:
        return "#c45fd0"
    if "yellow" in low or "gelb" in low:
        return "#e0a832"
    return "#aeb6c2"


# ---------------------------------------------------------------------------
# Business logic — no Qt, therefore testable without a window
# ---------------------------------------------------------------------------

class MaintenanceCore:
    """Runs actions against the printer; reports everything through callbacks."""

    def __init__(self, log_callback, status_callback):
        self.log = log_callback
        self.report_status = status_callback
        self.ip = None
        self.port = 9100
        self.snmp_port = 161
        self.snmp_timeout = 1.2
        self.power_limit = 360

    # -- status ---------------------------------------------------------------

    def query_status(self):
        """Fetch the SNMP status; callback with the report (dict) or None."""
        if not self.ip:
            self.log("No IP address given.")
            return None
        try:
            b = core.get_status(self.ip, self.snmp_port, self.snmp_timeout)
        except Exception as e:
            self.report_status(None)
            self.log(f"Status query failed: {e}")
            return None
        self.report_status(b)
        return b

    def _check_busy(self, b):
        """False if the printer is busy (action rejected)."""
        if b is None:
            return True  # SNMP unavailable -> do not block
        if b.get("status") in core.BUSY:
            self.log(f"Printer is busy ({b.get('status_text')}), action declined.")
            return False
        return True

    # -- actions ----------------------------------------------------------------

    def nozzle_check(self, mock=False):
        self.log("Nozzle check: printing the built-in test pattern.")
        self._action(core.stream_nozzle_check(), "Nozzle check", 90, mock)

    def head_clean(self, group=0, power=False, mock=False):
        what = "Power Cleaning" if power else "Head cleaning"
        limit = self.power_limit if power else 150
        self.log(
            f"{what} ({'all nozzle groups' if group == 0 else f'nozzle group {group}'}"
            ") starting."
        )
        self._action(core.stream_head_clean(group, power=power), what, limit, mock)

    def _action(self, stream, what, wait_limit, mock):
        """Sequence: precheck -> send -> monitor."""
        if not self._check_busy(self.query_status()):
            return
        if mock:
            self.log(f"[DRY RUN] {what} — would send the following stream:")
            self.log(core.hexdump(core.RESET + stream))
            self.log("[DRY RUN] Nothing was sent.")
            return
        try:
            core.send_tcp(self.ip, self.port, stream)  # send_tcp prepends RESET
        except OSError as e:
            self.log(f"Sending failed: {e}")
            return
        self.log(f"{what} sent.")
        self._wait(wait_limit)

    def _wait(self, limit):
        """Poll for the action to start and to finish."""
        if not self.ip:
            return
        self.log("Waiting for the action to start (up to 60 s) ...")
        started = False
        for _ in range(30):
            time.sleep(2)
            try:
                b = core.get_status(self.ip, self.snmp_port, self.snmp_timeout)
            except Exception:
                self.log("SNMP unreachable — watch the printer.")
                self.report_status(None)
                return
            self.report_status(b)
            if b.get("status") in core.BUSY:
                started = True
                self.log(f"Printer: {b['status_text']}")
                break
        if not started:
            self.log("No state change observed — check the action at the device.")
            return
        last = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            time.sleep(3)
            try:
                b = core.get_status(self.ip, self.snmp_port, self.snmp_timeout)
            except Exception:
                continue
            self.report_status(b)
            if b.get("status") in core.IDLE:
                self.log("Completed.")
                self.query_status()
                return
            if b.get("status_text") != last:
                last = b["status_text"]
                self.log(f"Printer: {last}")
        self.log("Time limit reached — printer not back to 'Ready'. Check the device.")


# ---------------------------------------------------------------------------
# Qt user interface
# ---------------------------------------------------------------------------

QSS = """
QMainWindow, QDialog { background: #15181d; }
#card { background: #1f242b; border: 1px solid #2a3039; border-radius: 10px; }
QLabel { color: #e6e8eb; background: transparent; }
#title { font-size: 20px; font-weight: 600; }
#subtitle { color: #8b949e; }
#state { font-size: 16px; font-weight: 600; }
#dot { border-radius: 7px; background: #8b949e; }
#section { color: #8b949e; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLineEdit, QSpinBox, QComboBox {
    background: #151a20; color: #e6e8eb; border: 1px solid #2a3039;
    border-radius: 6px; padding: 5px 8px; selection-background-color: #26527d;
}
QPushButton {
    background: #2c3542; color: #e6e8eb; border: 1px solid #3a4454;
    border-radius: 7px; padding: 9px 16px; font-weight: 600;
}
QPushButton:hover { background: #35414f; }
QPushButton:disabled { color: #5c6570; background: #23282f; }
#primary { background: #1f4d78; border-color: #2a6ba3; }
#primary:hover { background: #255a8f; }
#power { background: #66232e; border-color: #8f3040; }
#power:hover { background: #7c2b39; }
QProgressBar {
    background: #151a20; border: 1px solid #2a3039; border-radius: 6px;
    text-align: center; color: #e6e8eb; max-height: 18px; font-size: 11px;
}
QProgressBar::chunk { background: #6ea8d8; border-radius: 5px; }
QPlainTextEdit {
    background: #14171c; color: #b9c2cc; border: 1px solid #2a3039;
    border-radius: 8px; font-family: 'DejaVu Sans Mono', monospace; font-size: 11px;
}
QCheckBox { color: #b9c2cc; }
QStatusBar { color: #8b949e; }
QToolTip { background: #23282f; color: #e6e8eb; border: 1px solid #3a4454; }
"""


def build_window():
    """Creates the window class (separated so it stays testable headless)."""
    from PySide6 import QtCore, QtWidgets

    class Window(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Epson Printer Maintenance")
            self.setMinimumSize(660, 700)
            self.resize(700, 780)
            self.busy_ui = False
            self.dot_color = COLORS["unknown"]
            self.msgs = queue.Queue()
            self.ink_rows = []
            self.core = MaintenanceCore(self.log, self._push_status)
            self._build_ui()
            self._load_config()
            self.timer_queue = QtCore.QTimer(self, interval=120, timeout=self._drain_queue)
            self.timer_queue.start()
            self.timer_auto = QtCore.QTimer(self, interval=15000, timeout=self._auto_refresh)
            self.timer_auto.start()
            if self.core.ip:
                self.fetch_status()

        # ---- construction ----------------------------------------------------

        def _card(self):
            frame = QtWidgets.QFrame()
            frame.setObjectName("card")
            box = QtWidgets.QVBoxLayout(frame)
            box.setContentsMargins(14, 12, 14, 12)
            box.setSpacing(8)
            return frame, box

        def _build_ui(self):
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            page = QtWidgets.QVBoxLayout(central)
            page.setContentsMargins(16, 14, 16, 12)
            page.setSpacing(10)

            title = QtWidgets.QLabel("Epson Printer Maintenance")
            title.setObjectName("title")
            subtitle = QtWidgets.QLabel(
                "Local maintenance via ESC/P2 and SNMP — no Epson software required\n"
                "Command line: epson_maintenance.py"
            )
            subtitle.setObjectName("subtitle")
            page.addWidget(title)
            page.addWidget(subtitle)

            # connection
            frame, box = self._card()
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(QtWidgets.QLabel("Printer IP"))
            self.ip_field = QtWidgets.QLineEdit()
            self.ip_field.setPlaceholderText("192.168.178.175")
            self.ip_field.setMinimumWidth(170)
            self.ip_field.editingFinished.connect(self._save_config)
            row.addWidget(self.ip_field, stretch=1)
            row.addWidget(QtWidgets.QLabel("Port"))
            self.port_field = QtWidgets.QSpinBox()
            self.port_field.setRange(1, 65535)
            self.port_field.setValue(9100)
            row.addWidget(self.port_field)
            self.status_btn = QtWidgets.QPushButton("Fetch status")
            self.status_btn.setObjectName("primary")
            self.status_btn.clicked.connect(self.fetch_status)
            row.addWidget(self.status_btn)
            box.addLayout(row)
            page.addWidget(frame)

            # status
            frame, box = self._card()
            section = QtWidgets.QLabel("STATUS")
            section.setObjectName("section")
            box.addWidget(section)
            head = QtWidgets.QHBoxLayout()
            self.dot = QtWidgets.QLabel()
            self.dot.setObjectName("dot")
            self.dot.setFixedSize(14, 14)
            head.addWidget(self.dot)
            self.state_label = QtWidgets.QLabel("Unknown — enter the IP and fetch the status")
            self.state_label.setObjectName("state")
            head.addWidget(self.state_label, stretch=1)
            self.time_label = QtWidgets.QLabel("")
            self.time_label.setObjectName("subtitle")
            head.addWidget(self.time_label)
            box.addLayout(head)
            self.ink_layout = QtWidgets.QVBoxLayout()
            self.ink_layout.setSpacing(6)
            box.addLayout(self.ink_layout)
            page.addWidget(frame)

            # actions
            frame, box = self._card()
            section = QtWidgets.QLabel("ACTIONS")
            section.setObjectName("section")
            box.addWidget(section)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(10)
            self.nozzle_btn = QtWidgets.QPushButton("Print nozzle check")
            self.nozzle_btn.setToolTip("Prints the built-in nozzle test pattern — have paper ready")
            self.nozzle_btn.clicked.connect(self.run_nozzle_check)
            row.addWidget(self.nozzle_btn, stretch=1)
            self.clean_btn = QtWidgets.QPushButton("Clean print head")
            self.clean_btn.setToolTip("Regular head cleaning (moderate ink usage)")
            self.clean_btn.clicked.connect(lambda: self.run_clean(False))
            row.addWidget(self.clean_btn, stretch=1)
            self.power_btn = QtWidgets.QPushButton("Power Cleaning …")
            self.power_btn.setObjectName("power")
            self.power_btn.setToolTip("Intensive flush — significantly higher ink usage")
            self.power_btn.clicked.connect(lambda: self.run_clean(True))
            row.addWidget(self.power_btn, stretch=1)
            box.addLayout(row)
            extra = QtWidgets.QHBoxLayout()
            extra.addWidget(QtWidgets.QLabel("Nozzle group"))
            self.group_box = QtWidgets.QComboBox()
            self.group_box.addItems(["0 — all", "1", "2", "3", "4", "5"])
            extra.addWidget(self.group_box)
            extra.addStretch(1)
            self.mock_box = QtWidgets.QCheckBox("Dry run (log only, send nothing)")
            self.mock_box.toggled.connect(self._mock_note)
            extra.addWidget(self.mock_box)
            box.addLayout(extra)
            page.addWidget(frame)

            # log
            frame, box = self._card()
            section = QtWidgets.QLabel("LOG")
            section.setObjectName("section")
            box.addWidget(section)
            self.log_view = QtWidgets.QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(3000)
            self.log_view.setMinimumHeight(140)
            box.addWidget(self.log_view)
            page.addWidget(frame, stretch=1)

            self.statusBar().showMessage("Ready")

        # ---- queue / threads ---------------------------------------------------

        def log(self, text):
            self.msgs.put(("log", text))

        def _push_status(self, b):
            self.msgs.put(("status", b))

        def _drain_queue(self):
            try:
                while True:
                    kind, content = self.msgs.get_nowait()
                    if kind == "log":
                        self.log_view.appendPlainText(f"[{TIME_FMT('%H:%M:%S')}] {content}")
                    elif kind == "status":
                        self._apply_status(content)
                    elif kind == "done":
                        self.busy_ui = False
                        self._update_buttons()
                        self.statusBar().showMessage("Ready")
            except queue.Empty:
                pass

        def _work(self, fn):
            if self.busy_ui:
                return
            self.busy_ui = True
            self._update_buttons()
            self.statusBar().showMessage("Working …")
            self._save_config()

            def runner():
                try:
                    fn()
                finally:
                    self.msgs.put(("done", None))

            threading.Thread(target=runner, daemon=True).start()

        # -- actions -----------------------------------------------------------

        def _take_target(self):
            self.core.ip = self.ip_field.text().strip() or None
            self.core.port = self.port_field.value()

        def fetch_status(self):
            self._take_target()

            def fn():
                self.core.query_status()

            self._work(fn)

        def run_nozzle_check(self):
            self._take_target()
            mock = self.mock_box.isChecked()

            def fn():
                self.core.nozzle_check(mock=mock)

            self._work(fn)

        def run_clean(self, power):
            self._take_target()
            if power:
                box = QtWidgets.QMessageBox(self)
                box.setIcon(QtWidgets.QMessageBox.Warning)
                box.setWindowTitle("Confirm Power Cleaning")
                box.setText("Run Power Cleaning now?")
                box.setInformativeText(
                    "Power Cleaning is an intensive flush:\n\n"
                    "- significantly increased ink usage\n"
                    "- the waste ink pad fills up faster\n"
                    "- takes one to a few minutes; do not turn the printer off\n"
                    "- Epson recommends running it only after 2-3 regular\n"
                    "  cleanings with nozzle checks showed no improvement"
                )
                box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
                box.setDefaultButton(QtWidgets.QMessageBox.No)
                if box.exec() != QtWidgets.QMessageBox.Yes:
                    return
            mock = self.mock_box.isChecked()
            group = self.group_box.currentIndex()

            def fn():
                self.core.head_clean(group=group, power=power, mock=mock)

            self._work(fn)

        def _mock_note(self, on):
            if on:
                self.log("Dry run mode enabled: nothing will be sent to the printer.")

        # -- status handling ------------------------------------------------------

        def _apply_status(self, b):
            self.time_label.setText(TIME_FMT("queried %H:%M:%S"))
            if b is None:
                self.dot_color = COLORS["unknown"]
                self.state_label.setText("Not reachable — check IP and connection")
            else:
                st = b.get("status")
                if st in core.IDLE:
                    self.dot_color = COLORS["ok"]
                elif st in core.BUSY:
                    self.dot_color = COLORS["working"]
                elif st == 0:
                    self.dot_color = COLORS["error"]
                else:
                    self.dot_color = COLORS["unknown"]
                self.state_label.setText(b.get("status_text", "Unknown"))
            self.dot.setStyleSheet(f"background: {self.dot_color}; border-radius: 7px;")
            self._apply_inks(b)

        def _apply_inks(self, b):
            for row in self.ink_rows:
                row.deleteLater()
                self.ink_layout.removeWidget(row)
            self.ink_rows = []
            if b is None:
                return

            def bar(name, value, color):
                widget = QtWidgets.QWidget()
                h = QtWidgets.QHBoxLayout(widget)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                name_label = QtWidgets.QLabel(name)
                name_label.setMinimumWidth(130)
                pbar = QtWidgets.QProgressBar()
                pbar.setRange(0, 100)
                if value is None:
                    pbar.setRange(0, 1)
                    pbar.setValue(1)
                    pbar.setFormat("?")
                else:
                    pbar.setValue(value)
                    pbar.setFormat(f"{value}%")
                pbar.setStyleSheet(
                    f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}"
                )
                h.addWidget(name_label)
                h.addWidget(pbar, stretch=1)
                return widget

            entries = [(name, value, ink_color(name)) for name, value in b.get("ink", [])]
            if b.get("tanks"):
                level = max(b["tanks"])
                entries.append(
                    ("Waste ink pad", level, "#f0883e" if level >= 80 else "#8b949e")
                )
            for name, value, color in entries:
                widget = bar(name, value, color)
                self.ink_layout.addWidget(widget)
                self.ink_rows.append(widget)

        def _update_buttons(self):
            allowed = not self.busy_ui and self.dot_color in (COLORS["ok"], COLORS["unknown"])
            for btn in (self.nozzle_btn, self.clean_btn, self.power_btn):
                btn.setEnabled(allowed)
            self.status_btn.setEnabled(not self.busy_ui)

        def _auto_refresh(self):
            if not self.busy_ui and self.ip_field.text().strip():
                self.fetch_status()

        # -- configuration -----------------------------------------------------------

        def _load_config(self):
            path = CONFIG_PATH
            data = {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                if not os.path.exists(path) and os.path.exists(CONFIG_PATH_LEGACY):
                    try:
                        with open(CONFIG_PATH_LEGACY, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = {}
            self.ip_field.setText(data.get("ip", ""))
            self.port_field.setValue(int(data.get("port", 9100)))
            self.group_box.setCurrentIndex(int(data.get("group", 0)))
            self._take_target()

        def _save_config(self):
            self._take_target()
            data = {
                "ip": self.ip_field.text().strip(),
                "port": self.port_field.value(),
                "group": self.group_box.currentIndex(),
            }
            try:
                os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            except Exception:
                pass

        def closeEvent(self, event):
            self._save_config()
            super().closeEvent(event)

    return Window


def main():
    try:
        from PySide6 import QtWidgets
    except ImportError:
        print("PySide6 is missing. Install with:  sudo pacman -S python-pyside6", file=sys.stderr)
        sys.exit(1)
    window_class = build_window()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    window = window_class()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
