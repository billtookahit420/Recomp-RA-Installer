#!/usr/bin/env python3
"""Recomp RA Installer - GUI (PySide6).

Point it at your master retroachievements.toml and at a folder of
extracted recomp packages. It grades every package against the
master (installed / stale / missing), installs into the missing
ones, updates stale ones only when you say so, and can strip the
toml back out of a whole library before a server upload. See
rainstaller.py for the rules and README.md for the full story.

The api_token value is never shown in this window or written to the
log - only field names and match/mismatch states appear.

Worker discipline (family rule, learned the hard way): every QThread
is held in a set until it finishes - a worker reference is never
overwritten while running, and closing the window asks the core to
cancel, then drains the workers before exiting. A failed worker
always re-enables the buttons it disabled.
"""

import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import rainstaller

DARK = """
QWidget { background: #1d1f26; color: #e8e9ec; font-size: 13px; }
QLineEdit, QListWidget, QTableWidget, QPlainTextEdit {
    background: #262933; border: 1px solid #3a3f4d; border-radius: 4px; }
QPushButton { background: #343947; border: 1px solid #4a5064;
    border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background: #41465c; }
QPushButton:disabled { color: #7a7f8c; }
QHeaderView::section { background: #262933; color: #e8e9ec;
    border: none; padding: 4px; }
QProgressBar { background: #262933; border: 1px solid #3a3f4d;
    border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #7c5cd6; }
"""


class FuncWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class Bridge(QObject):
    log = Signal(str)
    progress = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recomp RA Installer")
        self.setStyleSheet(DARK)
        self.resize(1120, 740)
        self._workers = set()
        self._cancel = threading.Event()
        self.units = []
        self.skipped = 0
        self.scan_notes = []
        self._scanned_root = None
        self.master = None
        self.last_actions = []
        self.bridge = Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._on_progress)

        master_row = QHBoxLayout()
        self.master_edit = QLineEdit()
        self.master_edit.setPlaceholderText(
            "Your master retroachievements.toml…")
        browse_master = QPushButton("Browse…")
        browse_master.clicked.connect(self._browse_master)
        self.master_status = QLabel("no master loaded")
        master_row.addWidget(QLabel("Master toml:"))
        master_row.addWidget(self.master_edit)
        master_row.addWidget(browse_master)
        master_row.addWidget(self.master_status)

        lib_row = QHBoxLayout()
        self.lib_edit = QLineEdit()
        self.lib_edit.setPlaceholderText(
            "Folder of extracted recomp packages…")
        browse_lib = QPushButton("Browse…")
        browse_lib.clicked.connect(self._browse_lib)
        self.scan_btn = QPushButton("Scan library")
        self.scan_btn.clicked.connect(self._scan)
        lib_row.addWidget(QLabel("Library:"))
        lib_row.addWidget(self.lib_edit)
        lib_row.addWidget(browse_lib)
        lib_row.addWidget(self.scan_btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Package", "System", "State",
             "Differs in", "Update stale"])
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        mode = header.ResizeMode
        header.setSectionResizeMode(0, mode.Interactive)
        header.setSectionResizeMode(1, mode.ResizeToContents)
        header.setSectionResizeMode(2, mode.Stretch)
        header.setSectionResizeMode(3, mode.ResizeToContents)
        header.setSectionResizeMode(4, mode.ResizeToContents)
        self.table.setColumnWidth(0, 240)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        action_row = QHBoxLayout()
        self.install_btn = QPushButton("Install missing")
        self.install_btn.clicked.connect(self._install_missing)
        self.update_btn = QPushButton("Update ticked stale")
        self.update_btn.clicked.connect(self._update_ticked)
        self.update_all_btn = QPushButton("Update ALL stale…")
        self.update_all_btn.clicked.connect(self._update_all)
        self.strip_btn = QPushButton("Strip all…")
        self.strip_btn.clicked.connect(self._strip)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(lambda: self._cancel.set())
        self.export_btn = QPushButton("Export report…")
        self.export_btn.clicked.connect(self._export)
        for b in (self.install_btn, self.update_btn, self.update_all_btn,
                  self.strip_btn, self.cancel_btn):
            action_row.addWidget(b)
        action_row.addStretch()
        action_row.addWidget(self.export_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("idle")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        lay = QVBoxLayout()
        lay.addLayout(master_row)
        lay.addLayout(lib_row)
        lay.addWidget(self.table)
        lay.addLayout(action_row)
        lay.addWidget(self.progress_bar)
        lay.addWidget(QLabel("Log"))
        lay.addWidget(self.log_view)
        w = QWidget()
        w.setLayout(lay)
        self.setCentralWidget(w)

        self._restore_settings()

    # ------------------------------------------------------------ util
    def _log(self, msg):
        self.log_view.appendPlainText(msg)

    def _on_progress(self, msg):
        self.progress_bar.setFormat(msg[:90])

    def _run_worker(self, fn, handler, buttons, on_fail=None):
        for b in buttons:
            b.setEnabled(False)
        worker = FuncWorker(fn)
        self._workers.add(worker)
        worker.done.connect(handler)

        def _reenable():
            for b in buttons:
                b.setEnabled(True)

        def _failed(msg):
            self._log(f"error: {msg}")
            _reenable()
            if on_fail is not None:
                on_fail()

        worker.failed.connect(_failed)
        worker.finished.connect(
            lambda w=worker: (self._workers.discard(w), _reenable()))
        worker.start()
        return worker

    def _cancelled(self):
        return self._cancel.is_set()

    def _save_settings(self):
        rainstaller.save_settings({
            "library_root": self.lib_edit.text().strip(),
            "master_path": self.master_edit.text().strip(),
        })

    def _restore_settings(self):
        s = rainstaller.load_settings()
        if s.get("library_root"):
            self.lib_edit.setText(s["library_root"])
        if s.get("master_path"):
            self.master_edit.setText(s["master_path"])
            self._load_master(quiet=False)

    # ---------------------------------------------------------- master
    def _browse_master(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Choose master retroachievements.toml", "",
            "TOML files (*.toml);;All files (*)")
        if f:
            self.master_edit.setText(f)
            self._load_master(quiet=False)
            self._save_settings()

    def _load_master(self, quiet):
        path = self.master_edit.text().strip()
        self.master = None
        if not path:
            self.master_status.setText("no master loaded")
            return False
        try:
            self.master = rainstaller.load_master(path)
        except ValueError as e:
            self.master_status.setText("master invalid")
            self._log(f"master: {e}")
            return False
        self.master_status.setText(
            "master OK (token hidden, "
            f"hardcore={self.master.fields.get('hardcore')})")
        if not quiet:
            self._log(f"Master loaded: {self.master.describe()}")
        return True

    def _ready(self):
        """A pass needs a valid master and a completed scan."""
        if self.master is None:
            if not self._load_master(quiet=False):
                self._log("Load a valid master toml first.")
                return False
        if not self.units and self._scanned_root is None:
            self._log("Scan a library first.")
            return False
        return True

    # ------------------------------------------------------------ scan
    def _browse_lib(self):
        d = QFileDialog.getExistingDirectory(self, "Choose library folder")
        if d:
            self.lib_edit.setText(d)
            self._save_settings()
            self._scan()

    def _scan(self):
        root = self.lib_edit.text().strip()
        if not root:
            self._log("Pick a library folder first.")
            return
        if self.master is None and not self._load_master(quiet=False):
            self._log("Scan needs a valid master to grade against.")
            return
        self._cancel.clear()
        master = self.master

        def work():
            return rainstaller.scan_library(
                root, master, cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m))

        def done(result):
            self._scanned_root = root
            self.units, self.skipped, self.scan_notes = result
            self._fill_table()
            for n in self.scan_notes:
                self._log(f"note: {n}")
            c = rainstaller.count_states(self.units)
            self._log(
                f"Scanned {root}: {len(self.units)} package(s) - "
                f"good {c['good']}, stale {c['stale']}, "
                f"missing {c['missing']}, error {c['error']}; "
                f"{self.skipped} folder(s) skipped (no marker).")
            self._save_settings()

        self._run_worker(work, done, [self.scan_btn])

    def _fill_table(self):
        self.table.setRowCount(0)
        for u in self.units:
            self._append_row(u)
        self.table.resizeRowsToContents()

    def _append_row(self, u):
        r = self.table.rowCount()
        self.table.insertRow(r)
        title_item = QTableWidgetItem(u.title)
        title_item.setToolTip(str(u.folder))
        self.table.setItem(r, 0, title_item)
        self.table.setItem(r, 1, QTableWidgetItem(u.system))
        self.table.setItem(r, 2, QTableWidgetItem(u.state_text()))
        self.table.setItem(r, 3, QTableWidgetItem(
            ", ".join(u.differ) if u.differ else ""))
        box = QCheckBox()
        box.setEnabled(u.state == "stale")
        self.table.setCellWidget(r, 4, box)

    def _refresh_row(self, row, unit):
        state = self.table.item(row, 2)
        if state:
            state.setText(unit.state_text())
        diff = self.table.item(row, 3)
        if diff:
            diff.setText(", ".join(unit.differ) if unit.differ else "")
        box = self.table.cellWidget(row, 4)
        if box is not None:
            box.setEnabled(unit.state == "stale")
            if unit.state != "stale":
                box.setChecked(False)

    def _ticked_stale_keys(self):
        keys = set()
        for r, u in enumerate(self.units):
            box = self.table.cellWidget(r, 4)
            if box is not None and box.isChecked() \
                    and u.state == "stale":
                keys.add(u.key)
        return keys

    # ---------------------------------------------------------- passes
    def _run_pass(self, fn, buttons, verb):
        self._cancel.clear()
        units = self.units

        def done(results):
            self.last_actions = results
            for r, u in enumerate(units):
                self._refresh_row(r, u)
            tally = {}
            for a in results:
                tally[a["action"]] = tally.get(a["action"], 0) + 1
                if a["action"] in ("installed", "updated", "removed",
                                   "error"):
                    self.bridge.log.emit(
                        f"{a['unit'].title}: {a['action']} - "
                        f"{a['detail']}")
            self._log(f"{verb} done: "
                      + ", ".join(f"{k} {v}" for k, v in
                                  sorted(tally.items())) + ".")

        self._run_worker(fn, done, buttons)

    def _install_missing(self):
        if not self._ready():
            return
        n = sum(1 for u in self.units if u.state == "missing")
        if n == 0:
            self._log("Nothing missing - every package already has a "
                      "toml (stale ones need an explicit update).")
            return
        master = self.master
        units = self.units
        self._run_pass(
            lambda: rainstaller.run_install(
                units, master, cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m)),
            [self.install_btn], "Install")

    def _update_ticked(self):
        if not self._ready():
            return
        keys = self._ticked_stale_keys()
        if not keys:
            self._log("No stale rows ticked - tick the packages whose "
                      "toml you want replaced with the master.")
            return
        master = self.master
        units = self.units
        self._run_pass(
            lambda: rainstaller.run_install(
                units, master, update_keys=keys,
                cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m)),
            [self.update_btn], "Update")

    def _update_all(self):
        if not self._ready():
            return
        stale = [u for u in self.units if u.state == "stale"]
        if not stale:
            self._log("No stale packages to update.")
            return
        answer = QMessageBox.question(
            self, "Update all stale?",
            f"Replace the retroachievements.toml in all "
            f"{len(stale)} stale package(s) with the master copy?\n\n"
            "The differing fields are listed in the table. The "
            "replaced files are not backed up.")
        if answer != QMessageBox.Yes:
            return
        keys = {u.key for u in stale}
        master = self.master
        units = self.units
        self._run_pass(
            lambda: rainstaller.run_install(
                units, master, update_keys=keys,
                cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m)),
            [self.update_all_btn], "Update all")

    def _strip(self):
        if not self.units and self._scanned_root is None:
            self._log("Scan a library first.")
            return
        present = [u for u in self.units
                   if u.toml_path.is_file()]
        if not present:
            self._log("Nothing to strip - no package under this root "
                      "has a retroachievements.toml.")
            return
        answer = QMessageBox.question(
            self, "Strip all?",
            f"This removes your RetroAchievements login "
            f"(retroachievements.toml) from {len(present)} game(s) "
            f"under {self._scanned_root}.\n\n"
            "The games still run; achievements will be off until you "
            "install the toml again (this tool can, from your "
            "master). Strip anyway?")
        if answer != QMessageBox.Yes:
            return
        self._cancel.clear()
        units = self.units
        self._run_pass(
            lambda: rainstaller.run_strip(
                units, cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m)),
            [self.strip_btn], "Strip")

    # ---------------------------------------------------------- export
    def _export(self):
        if not self.units and self._scanned_root is None:
            self._log("Nothing to export yet - scan first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export report", "ra-installer-report.md",
            "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        Path(path).write_text(rainstaller.make_report(
            self.units, self.skipped, self.scan_notes, self.master,
            self._scanned_root or "", actions=self.last_actions))
        self._log(f"Report written to {path}")

    # ----------------------------------------------------------- close
    def closeEvent(self, event):
        self._cancel.set()
        self._save_settings()
        for w in list(self._workers):
            w.wait(15000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
